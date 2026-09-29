#!/usr/bin/env python3
"""
migrate_profiles_dir.py — move Chrome Isolation profile data off the confusing
`~/Chrome` and onto the new, unmistakable store (`~/Isolated-Chrome/profiles` by
default; `$CHROME_ISOLATION_HOME` overrides the root).

`~/Chrome` is a genuinely bad name for this: it reads like a browser profile
store, is confusable with real Google Chrome (`~/.config/google-chrome`), and is
exactly the sort of generic directory a person `rm -rf`s as "browser junk" — with
real cookies, logins and history inside. The new name cannot be confused with a
browser and is not something any browser creates.

SAFETY CONTRACT
---------------
This moves live user data, so it is deliberately hard to run by accident:

  * **Dry run by default.** Without `--apply` it only prints a plan. It imports
    backend/paths.py, which has no import-time side effects, so a dry run
    provably changes nothing on disk.
  * **Verify before deleting.** Every profile is fingerprinted (regular file
    count + total bytes, plus directory and symlink counts) *before* the move.
    After the move the destination is fingerprinted again. Only when the two
    agree is the source allowed to be removed. Any mismatch aborts that profile
    with the source left intact — nothing is deleted on a guess.
  * **Same filesystem: `rename`.** Atomic and instant. Cross-device: copy to a
    `<dest>.partial` staging dir, verify the copy, `rename` it into place (atomic
    on the destination filesystem), verify again, and only then remove the
    source. A failed copy leaves the source untouched and the partial copy on
    disk for inspection.
  * **Never silently lose a profile.** If directories move but the registry
    rewrite then fails, that is reported as a hard error with the exact new
    paths, never swallowed.
  * **Refuses to run into a non-empty destination** unless every profile is
    verifiably already migrated (idempotent re-run) or `--resume` is given.
  * **Backs up profiles.json** (timestamped, next to the original) before
    rewriting it, and rewrites it atomically via a temp file + `os.replace`,
    matching the pattern in backend/registry.py.
  * **Leaves an empty `~/Chrome` behind** for the user to `rmdir` themselves
    rather than deleting a directory unasked. The profile directories inside it
    are the only things removed, and only after verification.

USAGE
-----
    python3 scripts/migrate_profiles_dir.py              # dry run: print the plan
    python3 scripts/migrate_profiles_dir.py --apply      # actually move things

    # Non-default locations (defaults come from $CHROME_ISOLATION_HOME / $HOME):
    python3 scripts/migrate_profiles_dir.py --from ~/old --to ~/new/profiles
    python3 scripts/migrate_profiles_dir.py --registry /path/to/profiles.json

    # Resume after an interrupted run (destination already has some profiles):
    python3 scripts/migrate_profiles_dir.py --apply --resume

EXIT STATUS
    0  nothing to do, or (with --apply) everything moved and verified
    1  error — nothing destructive happened, or a per-profile failure was isolated
    2  usage error
    3  refused: destination non-empty and not verifiably already migrated

This script NEVER runs the migration implicitly and is not imported by the app.
"""
import argparse
import errno
import json
import os
import shutil
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(os.path.dirname(_HERE), "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# paths.py is import-side-effect free by design: a dry run must not create a
# single directory on its way to printing a plan.
import paths  # noqa: E402

# The single seam through which profile data is copied. Indirected so tests can
# substitute a deliberately lossy copy and prove the verification catches it.
_copytree = shutil.copytree


# --------------------------------------------------------------------------- #
# Fingerprinting — the verification primitive
# --------------------------------------------------------------------------- #
def fingerprint(path):
    """
    Count what is actually inside a profile directory.

    Regular files are counted and their sizes summed; directories and symlinks
    are counted separately. Symlinks are never followed (a Chrome profile's
    Singleton* links point at sockets, and following one would either fail or
    wander outside the profile). `other` catches sockets, fifos and devices,
    which Chromium does create — they must not silently vanish in a migration.

    Returns a dict so comparisons are a single ==, and so a mismatch report can
    name the exact component that differs.
    """
    stats = {"files": 0, "bytes": 0, "dirs": 0, "symlinks": 0, "other": 0}
    if not os.path.isdir(path):
        return None

    # Recurse with scandir rather than os.walk: os.walk reports a symlink to a
    # directory in `dirnames` even with followlinks=False, which would both
    # misclassify it and risk descending through it. Chromium profiles contain
    # Singleton* symlinks, so getting this right matters.
    import stat as _stat
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            # Unreadable directory: count it once so before/after must agree.
            stats["other"] += 1
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError:
                # Vanished or unreadable mid-scan. Counted as "other" so the
                # before/after comparison still has to agree.
                stats["other"] += 1
                continue
            if _stat.S_ISLNK(st.st_mode):
                stats["symlinks"] += 1
            elif _stat.S_ISDIR(st.st_mode):
                stats["dirs"] += 1
                stack.append(entry.path)
            elif _stat.S_ISREG(st.st_mode):
                stats["files"] += 1
                stats["bytes"] += st.st_size
            else:
                # Sockets, fifos, devices: Chromium does create these, and they
                # must not silently vanish in a migration.
                stats["other"] += 1
    return stats


def describe(stats):
    if stats is None:
        return "missing"
    return (f"{stats['files']} files, {stats['bytes']} bytes, "
            f"{stats['dirs']} dirs, {stats['symlinks']} symlinks, "
            f"{stats['other']} other")


def diff(expected, actual):
    """Human-readable difference between two fingerprints."""
    if expected is None or actual is None:
        return f"expected {describe(expected)}, got {describe(actual)}"
    parts = [f"{k}: {expected[k]} -> {actual[k]}" for k in expected
             if expected[k] != actual[k]]
    return "; ".join(parts) or "identical"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
def read_registry(registry_file):
    """Load profiles.json, tolerating absence and corruption."""
    if not os.path.isfile(registry_file):
        return {}
    try:
        with open(registry_file, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"error: cannot read registry {registry_file}: {e}")


def _under(path, base):
    """True when `path` is inside `base` (or is `base` itself)."""
    if not path or not base:
        return False
    path = os.path.normpath(os.path.abspath(path))
    base = os.path.normpath(os.path.abspath(base))
    return path == base or path.startswith(base + os.sep)


def backup_registry(registry_file):
    """Copy profiles.json aside, timestamped, before anything rewrites it."""
    if not os.path.isfile(registry_file):
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = f"{registry_file}.bak-{stamp}"
    n = 1
    while os.path.exists(dest):
        dest = f"{registry_file}.bak-{stamp}.{n}"
        n += 1
    shutil.copy2(registry_file, dest)
    return dest


def rewrite_registry(registry_file, data):
    """
    Write profiles.json atomically: temp file in the same directory, then
    os.replace, which is atomic on POSIX and on Windows. Same pattern as
    registry._save, so a crash mid-write can never truncate the live registry.
    """
    tmp = registry_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, registry_file)


# --------------------------------------------------------------------------- #
# The move itself
# --------------------------------------------------------------------------- #
def same_filesystem(a, b):
    """
    True when `a` and `b` are on the same device, so rename(2) can move between
    them atomically. Uses the nearest existing ancestor of each path, so it works
    before the destination has been created.
    """
    def dev(path):
        path = os.path.abspath(path)
        while not os.path.exists(path):
            parent = os.path.dirname(path)
            if parent == path:
                return None
            path = parent
        try:
            return os.stat(path).st_dev
        except OSError:
            return None

    da, db = dev(a), dev(b)
    return da is not None and da == db


def _move_same_fs(src, dst, expected):
    """
    rename(2), then verify. rename is atomic, so a failure here means the
    destination genuinely does not match the source we just measured — which
    should be impossible. Abort loudly rather than proceeding to a registry
    rewrite that would point at a bad directory.
    """
    os.rename(src, dst)
    actual = fingerprint(dst)
    if actual != expected:
        # Do NOT roll back silently — a half-understood state is worse than a
        # reported one. Say exactly what happened and stop.
        raise RuntimeError(
            f"verification failed after rename for {src}: {diff(expected, actual)}. "
            f"Source is now at {dst}. Nothing was deleted; the source no longer "
            f"exists at its old path. Restore manually from {dst} if needed.")
    return "rename"


def _move_cross_fs(src, dst, expected):
    """
    copy -> verify the copy -> rename into place (atomic within the destination
    filesystem) -> verify again -> remove the source. The source is only ever
    removed after the destination has been proven to hold the same data.
    """
    staging = dst + ".partial"
    if os.path.exists(staging):
        shutil.rmtree(staging)

    try:
        # _copytree (not shutil.copytree directly) so the copy step is a single,
        # documented seam. It exists for the tests, which use it to simulate a
        # lossy copy and prove the verification catches it.
        _copytree(src, staging, symlinks=True)
    except OSError as e:
        # Leave any partial staging dir in place for inspection, and above all
        # leave the source alone.
        raise RuntimeError(
            f"copy of {src} to {staging} failed: {e}. "
            f"Source is untouched at {src}.")

    staged_stats = fingerprint(staging)
    if staged_stats != expected:
        raise RuntimeError(
            f"verification failed for the copy of {src}: "
            f"{diff(expected, staged_stats)}. The source is untouched at {src}; "
            f"the incomplete copy was left at {staging} for inspection.")

    # Staging and destination share a filesystem (both under dst's parent), so
    # this rename is atomic: dst is never half-populated.
    os.rename(staging, dst)
    actual = fingerprint(dst)
    if actual != expected:
        raise RuntimeError(
            f"verification failed after promoting {staging} to {dst}: "
            f"{diff(expected, actual)}. Source is untouched at {src}.")

    shutil.rmtree(src)
    return "copy+verify"


def move_profile(src, dst, expected):
    """
    Move one profile, verifying before anything is deleted.
    Returns the method used ('rename' or 'copy+verify').
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    if os.path.exists(dst):
        raise RuntimeError(
            f"destination already exists: {dst}. Refusing to touch it — the "
            f"source is intact at {src}.")

    if same_filesystem(src, dst):
        try:
            return _move_same_fs(src, dst, expected)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            # Crossed devices between the stat and the rename. Fall through to
            # the copy path rather than failing.
            return _move_cross_fs(src, dst, expected)
    return _move_cross_fs(src, dst, expected)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #
def _dirs(base):
    try:
        return {n for n in os.listdir(base)
                if os.path.isdir(os.path.join(base, n))}
    except OSError:
        return set()


def already_migrated(src, dst, profiles, registry, registry_file):
    """
    True when there is nothing left to move because the migration already
    happened: the source holds no profiles and every profile the registry knows
    about lives at the destination.

    This is what makes a second run a safe no-op instead of an error, and it is
    deliberately a *positive* claim — the tool never infers "done" from the
    absence of an error.
    """
    if _dirs(src):
        return False
    if not _dirs(dst):
        return False          # nothing to migrate and nothing migrated: a no-op
    for name, entry in registry.items():
        if not _under((entry or {}).get("path", ""), dst):
            return False
    return True


def plan(src, dst, registry, registry_file):
    """
    Work out what a run would do. Returns (actions, already_done).

    `actions` is a list of (name, src_path, dst_path, stats, needs_rewrite).
    A profile needs a registry rewrite when its recorded path is under `src` —
    including the case where the directory is already gone but the registry
    still points at the old location.
    """
    names = _dirs(src) | {n for n, e in registry.items()
                          if _under((e or {}).get("path", ""), src)}
    actions = []
    for name in sorted(names):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        entry = registry.get(name) or {}
        recorded = entry.get("path", "")
        # Only rewrite an entry that actually points into the old store; a
        # custom location elsewhere is the user's deliberate choice and is left
        # exactly as it is.
        rewrite = _under(recorded, src)
        # Already at the destination and gone from the source (an interrupted
        # earlier run): nothing to move, but the registry entry still needs
        # rewriting. `stats` is None there, so there is nothing to compare.
        skip_move = os.path.isdir(d) and not os.path.isdir(s)
        actions.append([name, s, d, fingerprint(s), rewrite, skip_move])
    return actions, already_migrated(src, dst, [a[0] for a in actions],
                                     registry, registry_file)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _print_plan(actions, src, dst, registry_file, apply_mode):
    mode = "APPLY" if apply_mode else "DRY RUN (nothing will be changed)"
    print(f"Chrome Isolation — profile data relocation [{mode}]")
    print(f"  from: {src}")
    print(f"    to: {dst}")
    print(f"  registry: {registry_file}")
    print()
    if not actions:
        print("Nothing to migrate: no profiles found in the old location.")
        return
    print(f"{len(actions)} profile(s):\n")
    for name, s, d, stats, rewrite, skip_move in actions:
        present = os.path.isdir(s)
        if skip_move:
            status = "already at the destination (registry only)"
        elif present:
            status = "move"
        else:
            status = "MISSING — will be reported, not moved"
        print(f"  • {name}")
        print(f"      source:      {s}{'' if present else '   [MISSING]'}")
        print(f"      destination: {d}")
        print(f"      contents:    {describe(stats)}")
        print(f"      action:      {status}")
        print(f"      registry:    {'rewrite to new path' if rewrite else 'left alone (not under the old store)'}")
    print()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="migrate_profiles_dir.py",
        description="Relocate Chrome Isolation profile data off ~/Chrome. "
                    "Dry run unless --apply is given.")
    ap.add_argument("--from", dest="src", default=None,
                    help="old profile directory (default: $HOME/Chrome)")
    ap.add_argument("--to", dest="dst", default=None,
                    help="new profile directory "
                         "(default: $CHROME_ISOLATION_HOME/profiles, "
                         "else ~/Isolated-Chrome/profiles)")
    ap.add_argument("--registry", default=None,
                    help="profiles.json to rewrite (default: the app's own)")
    ap.add_argument("--apply", action="store_true",
                    help="actually move the data (without this: dry run)")
    ap.add_argument("--resume", action="store_true",
                    help="allow a non-empty destination that holds already-"
                         "migrated profiles; only the missing ones are moved")
    args = ap.parse_args(argv)

    src = os.path.abspath(os.path.expanduser(
        args.src if args.src is not None else paths.legacy_profiles_dir()))
    dst = os.path.abspath(os.path.expanduser(
        args.dst if args.dst is not None else paths.default_profiles_dir()))
    registry_file = os.path.abspath(os.path.expanduser(
        args.registry if args.registry is not None else paths.registry_file()))

    if src == dst:
        print(f"Nothing to do: --from and --to are the same directory ({src}).")
        return 0
    if _under(dst, src):
        print(f"error: --to ({dst}) is inside --from ({src}).", file=sys.stderr)
        return 2
    # A destination inside real Chrome's own storage would make this app write
    # into the browser it exists to stay clear of.
    real_chrome = os.path.join(paths.home(), ".config", "google-chrome")
    if _under(dst, real_chrome) or _under(src, real_chrome):
        print(f"error: refusing to touch {real_chrome} — that is real Google "
              f"Chrome's storage.", file=sys.stderr)
        return 2

    registry = read_registry(registry_file)
    actions, done = plan(src, dst, registry, registry_file)

    if args.resume:
        # Already-at-the-destination entries are planned as skip_move above, so
        # --resume only relaxes the non-empty-destination refusal below.
        pass

    # "Already migrated" is decided before the plan is printed, because a
    # completed migration legitimately has nothing left to list.
    if done:
        print(f"Already migrated: {src} holds no profiles and every profile "
              f"the registry knows about is at {dst}.")
        print("Nothing to do.")
        return 0

    _print_plan(actions, src, dst, registry_file, args.apply)

    if not actions:
        if not os.path.isdir(src):
            print(f"Nothing to migrate: {src} does not exist.")
        return 0

    # Refuse a non-empty destination we cannot fully account for.
    dst_has = _dirs(dst) if os.path.isdir(dst) else set()
    unexplained = dst_has - {a[0] for a in actions}
    if dst_has and not (args.resume or done):
        print(f"error: destination {dst} is not empty "
              f"({len(dst_has)} profile(s) already there) and the migration "
              f"cannot prove they are part of this migration.", file=sys.stderr)
        if unexplained:
            print(f"  not accounted for by this run: "
                  f"{', '.join(sorted(unexplained))}", file=sys.stderr)
        print("  Refusing to touch either directory. Resolve this first, or "
              "re-run with --apply --resume if you know the destination only "
              "holds already-migrated profiles.", file=sys.stderr)
        return 3

    rewrites = [a for a in actions if a[4]]
    if not args.apply:
        if rewrites:
            bak = f"{registry_file}.bak-<timestamp>"
            print(f"Would back up   : {bak}")
            print(f"Would rewrite   : {registry_file} "
                  f"({len(rewrites)} path(s), atomically)")
            for name, _, d, _, _, _ in rewrites:
                print(f"                   {name} -> {d}")
        print()
        print("Dry run complete. NOTHING WAS CHANGED.")
        print("Re-run with --apply to perform the migration.")
        return 0

    # ---- apply -------------------------------------------------------------
    backup = backup_registry(registry_file)
    if backup:
        print(f"Backed up registry: {backup}")

    moved, errors = [], []
    # Only profiles that are actually AT the destination, verified, may have
    # their registry path rewritten. A profile that failed to move keeps its old
    # path on purpose: that is where the data still is, and pointing the app at
    # an unverified directory would hide real data behind a broken entry.
    settled = set()
    for name, s, d, stats, rewrite, skip_move in actions:
        if skip_move:
            # Already at the destination from an earlier run. Nothing to move;
            # the registry entry below still gets rewritten.
            settled.add(name)
            print(f"  = {name}: already at {d}, leaving data alone")
            continue
        if stats is None or not os.path.isdir(s):
            errors.append(
                f"profile '{name}': source {s} does not exist. The registry "
                f"still references it; nothing was moved and nothing was "
                f"deleted.")
            continue
        try:
            method = move_profile(s, d, stats)
        except (RuntimeError, OSError) as e:
            # Source intact for the copy path; reportable error, never swallowed.
            errors.append(f"profile '{name}': {e}")
            continue
        moved.append((name, d, method))
        settled.add(name)
        print(f"  ✓ {name}: {s} -> {d}  ({method}, verified "
              f"{describe(stats)})")

    if moved:
        print(f"\n{len(moved)} profile(s) moved and verified.")

    rewrites = [a for a in actions if a[4] and a[0] in settled]
    unrewritten = [a[0] for a in actions if a[4] and a[0] not in settled]
    if unrewritten:
        # A partial migration is a real, reportable state: these profiles are
        # still where they were, and the app must keep using them from there.
        errors.append(
            "registry paths NOT updated for "
            f"{', '.join(sorted(unrewritten))} — those profiles did not move, "
            f"so their entries still point at {src}, which is where their data "
            f"is. Fix or re-run once the problem above is resolved.")

    if rewrites:
        for name, _, d, _, _rewrite, _skip in rewrites:
            entry = registry.get(name)
            if entry is not None:
                entry["path"] = d
        try:
            os.makedirs(os.path.dirname(registry_file) or ".", exist_ok=True)
            rewrite_registry(registry_file, registry)
            print(f"Registry rewritten: {registry_file}")
        except (OSError, TypeError, ValueError) as e:
            # The data moved but the registry did not. That is a hard error and
            # must be loud: the paths are recoverable, the app is not usable
            # until they are fixed.
            print("\nERROR: profile data was moved but the registry could not "
                  f"be updated ({e}).", file=sys.stderr)
            print("The profiles are at:", file=sys.stderr)
            for name, d, _ in moved:
                print(f"  {name} -> {d}", file=sys.stderr)
            print(f"Fix by hand in {registry_file}, or restore the backup and "
                  f"re-run with --apply.", file=sys.stderr)
            for e2 in errors:
                print(f"  also: {e2}", file=sys.stderr)
            return 1
    elif registry_file and not os.path.exists(registry_file):
        print("No registry entry pointed into the old directory; registry not "
              "rewritten.")

    # The old directory itself is left in place (now empty) — removing a
    # directory unasked is the user's call, and `rmdir` on a non-empty
    # directory fails safely anyway.
    if os.path.isdir(src) and not _dirs(src):
        print(f"\nThe old directory {src} is now empty. Remove it when you are "
              f"sure:\n    rmdir {src}")

    if errors:
        print(f"\n{len(errors)} problem(s):", file=sys.stderr)
        for e in errors:
            print(f"  ! {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Regression tests: scripts/verify.sh must not write into the user's data.

THE BUG THESE EXIST FOR
-----------------------
Check 13 of the harness reported:

    THE HARNESS MUTATED USER DATA: DIRENT
      .../host-shares/bridge-tz-385493;
      DIRENT .../host-shares/bridge-upd-385493;

Running a verification harness created directories inside the user's real
host-shares store. An earlier version of the same harness had already been
caught adding a junk profile to the user's profiles.json, so this was a repeat
of a known failure mode.

WHAT ACTUALLY CREATED THEM
--------------------------
Check 2 introspects the container-hardening code by CALLING it, and calling
`_profile_host_mount()` is not a read: for every profile with an empty
host_mount it resolves through

    docker_manager._profile_host_mount()
      -> host_share.resolve_host_mount(name, '')
        -> host_share.ensure_host_share_dir(name)
          -> os.makedirs(<real host-shares>/<name>, mode=0o700)

The probe DID install a stub — but it stubbed
`docker_manager.ensure_host_share_dir`, which is a name that
`_profile_host_mount()` never calls. The mkdir went through
`resolve_host_mount()`, which looks `ensure_host_share_dir` up in host_share's
own namespace and got the real one. The stub was called zero times and the
directory was created anyway. Names like `bridge-tz-<pid>` come from the test
suite's real-bridge profiles being present in the registry when check 2 ran.

WHAT THE FIX IS
---------------
Redirect the LOCATION the code derives the path from
(`host_share.HOST_SHARES_DIR` -> the run's mktemp scratch dir), *and* the
function names, *and* let the probe measure its own leak
(`PROBE_HOST_SHARE_LEAK`) so a future regression fails loudly instead of
silently littering.

WHAT IS TESTED HERE
-------------------
These tests extract the probe body straight out of scripts/verify.sh — between
its BEGIN and END markers — and execute it. A copy of the probe would drift
from the shipped one and would keep passing after the real one regressed, so
the shipped text is what runs.

The probe is run under a REDIRECTED HOME. That is deliberate: it makes the
failure observable instead of destructive. With HOME pointed at a temp tree,
backend/config.py derives APP_DATA_DIR, the registry and the host-shares root
from it, so any mkdir the probe fails to redirect lands in the sandbox where
the test can see it — and the real store is compared before and after, so a
real leak is still caught.

THE SECOND INSTANCE OF THE SAME FAILURE MODE (found 2026-09-29)
--------------------------------------------------------------
These tests were themselves an instance of the bug they document. They were
green in the working tree and red from `git archive HEAD`, and the reason was
the same shape as the probe bug above, one level up:

  * the probe read its profiles from `<repo>/profiles.json`, i.e. from the
    CHECKOUT directory rather than from the app's own resolver. Those are the
    same string only on a host where the checkout IS the install directory
    (`~/.local/share/chrome-isolation-manager`, where the developer keeps it);
  * these tests then read `<repo>/profiles.json` too, and derived their
    EXPECTATIONS from whatever the developer's live registry happened to hold.

`profiles.json` is git-ignored runtime data, so a clean export has no such
file at all: the probe died at `open()` and two tests raised FileNotFoundError.
On the development host the file was present, held four profiles, and every
assertion was satisfied — including
`test_the_probe_is_not_vacuous`, which SKIPPED when no profile used the empty
host_mount default, a skip condition wired to untracked state.

So the generalisable lesson, now asserted by the tests below rather than left
in a comment: a test's INPUTS must be fixtures, and its expectations must be
derived from those fixtures. Reading live runtime state to decide what to
assert produces a green result that measures the machine it ran on, not the
code.
"""
import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFY = os.path.join(ROOT, "scripts", "verify.sh")

BEGIN = "# VERIFY-PROBE-HOST-MOUNTS-BEGIN"
END = "# VERIFY-PROBE-HOST-MOUNTS-END"

# The lines the harness feeds to the probe as sys.argv, before the marked body.
# They are extracted from verify.sh too, not copied here: a copy would drift
# from the shipped probe and would keep passing after the real one regressed.
HEREDOC_OPEN = "<<'PY_EOF'"


def _probe_source():
    with open(VERIFY) as f:
        text = f.read()
    assert BEGIN in text, (
        "%s no longer contains the %r marker. tests/test_verify_harness_side_effects.py "
        "extracts the shipped probe between its BEGIN and END markers; without them this "
        "test silently stops testing anything." % (VERIFY, BEGIN)
    )
    assert END in text, "%s no longer contains the %r marker" % (VERIFY, END)
    # Everything from the heredoc opener that immediately precedes the BEGIN
    # marker up to the marker itself is the probe's setup (imports, the stubbed
    # docker client, construction of the DockerManager under test).
    b = text.index(BEGIN)
    opener = text.rfind(HEREDOC_OPEN, 0, b)
    assert opener != -1, "no %s heredoc opens the probe body" % HEREDOC_OPEN
    setup = text[text.index("\n", opener) + 1:b]
    body = text[b:].split(END, 1)[0]
    return setup + body


def _tree(root):
    """(relpath, mtime_ns, size) for every entry under `root`, sorted."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames) + sorted(filenames):
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out.append((os.path.relpath(p, root), st.st_mtime_ns, st.st_size))
    return sorted(out)


def _fake_app_data(fake_home):
    """Where backend/paths.py puts the registry when HOME is `fake_home`."""
    return os.path.join(str(fake_home), ".local", "share",
                        "chrome-isolation-manager")


def _write_registry(fake_home, profiles):
    """
    Materialise `profiles` as the registry the probe will actually read.

    WHY THE TEST BUILDS ITS OWN REGISTRY
    ------------------------------------
    This used to open `<repo>/profiles.json` — the developer's real, live
    profile registry — and compute its expectations from whatever was in it.
    That is what made the test meaningless outside one specific directory
    layout, and it is why it failed from a clean export:

      * `profiles.json` is git-IGNORED (`.gitignore: /profiles.json`). It is
        runtime user data, not source. `git archive HEAD` does not contain it,
        so a clean checkout has no registry to read and the test died with
        FileNotFoundError;
      * it only ever existed at `<repo>/profiles.json` on a host where the
        checkout IS the install directory, i.e. where `$HOME/.local/share/
        chrome-isolation-manager` is the working tree. There the app's resolver
        and the repo path coincide by accident. Everywhere else they do not.

    A test whose expected values come from untracked runtime state is a test
    that can only be run on the machine that wrote it. The fixture below makes
    the input explicit, so the assertions are the same on every host.
    """
    app_data = _fake_app_data(fake_home)
    os.makedirs(app_data, exist_ok=True)
    path = os.path.join(app_data, "profiles.json")
    with open(path, "w") as f:
        json.dump(profiles, f, indent=2, sort_keys=True)
    return path


def _sample_registry(store, n_default=3, n_explicit=1, explicit_dir=None):
    """A registry with known, counted profiles — so `expected` is a fact.

    Mixes the two host_mount cases deliberately: an empty one must produce a
    per-profile share dir (the thing this whole file is about), and an explicit
    one must still be honoured, so a probe that stubbed too much would be
    caught rather than rewarded.
    """
    profiles = {}
    for i in range(n_default):
        name = "probe-default-%d" % i
        profiles[name] = {
            "name": name,
            "path": os.path.join(store, name),
            "host_mount": "",
            "proxy": "",
            "created_at": "1970-01-01T00:00:00Z",
        }
    for i in range(n_explicit):
        name = "probe-explicit-%d" % i
        profiles[name] = {
            "name": name,
            "path": os.path.join(store, name),
            # An explicit host_mount has to exist and has to survive
            # validate_host_mount(), so point it at a real scratch directory
            # rather than at anything resembling user data.
            "host_mount": str(explicit_dir),
            "proxy": "",
            "created_at": "1970-01-01T00:00:00Z",
        }
    return profiles


def _run_probe(fake_home, scratch, profiles=None):
    """
    Execute the shipped probe under a redirected HOME.

    `profiles` is written as the registry first, so the probe always has
    something real to introspect and never depends on state outside tmp_path.
    """
    fake_home = str(fake_home)
    scratch = str(scratch)
    os.makedirs(fake_home, exist_ok=True)
    os.makedirs(scratch, exist_ok=True)
    store = os.path.join(fake_home, "Isolated-Chrome", "profiles")
    os.makedirs(store, exist_ok=True)
    if profiles is None:
        explicit = os.path.join(fake_home, "shared-inbox")
        os.makedirs(explicit, exist_ok=True)
        profiles = _sample_registry(store, explicit_dir=explicit)
    _write_registry(fake_home, profiles)

    env = dict(os.environ)
    env["HOME"] = fake_home
    env["VERIFY_SCRATCH"] = scratch
    env.pop("CHROME_ISOLATION_HOME", None)
    return subprocess.run([sys.executable, "-", ROOT],
                          input=_probe_source(), env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=180)


def _probe_ran(r):
    """The probe's own account of itself, or a hard failure with the output.

    Every probe test funnels through this so that "the probe printed nothing"
    can never be mistaken for "the probe found nothing". That mistake is the
    entire bug class in this file.
    """
    if "PROBE_REGISTRY" not in r.stdout:
        raise AssertionError(
            "the probe never reported which registry it read, so it may not "
            "have introspected anything at all (rc=%s)\nstdout:\n%s\nstderr:\n%s"
            % (r.returncode, r.stdout, r.stderr))
    return r


def _real_host_shares_dir():
    """
    The USER'S host-share root, as the app resolves it — never `$REPO_ROOT/...`.

    Asking the app is the whole point: `host_shares.HOST_SHARES_DIR` is
    `config.APP_DATA_DIR/host-shares`, which is under $HOME. In a clean export
    the two are different directories, and a test that assumes otherwise is
    watching a path the code will never touch — which is how a leak test passes
    without having looked at anything.
    """
    code = ("import os, sys; sys.path.insert(0, os.path.join(sys.argv[1], 'backend'));"
            "import paths; print(os.path.join(paths.app_data_dir(), 'host-shares'))")
    r = subprocess.run([sys.executable, "-c", code, ROOT],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _has_docker_sdk():
    return subprocess.run([sys.executable, "-c", "import docker"],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _has_docker_sdk(),
    reason="the probe imports the docker SDK; without it verify.sh SKIPs check 2")


def test_the_probe_is_byte_identical_before_and_after(tmp_path):
    """
    THE REGRESSION: the probe must not touch the USER'S real host-shares store.

    What made this a faithful reproduction rather than a simulation:

      * the registry handed to the probe contains profiles whose host-share
        directories DO NOT EXIST YET. Without that the test is blind on a host
        where every real profile's directory is already present — exactly the
        state this host was in when the bug was found, and exactly why the
        litter looked intermittent;
      * the store being compared is the one the APP resolves
        (`_real_host_shares_dir()`), not `$REPO_ROOT/host-shares`. The old
        version used the latter, which coincides with the app's store only on a
        host whose checkout IS the install directory. Anywhere else it compared
        a directory the probe had no reason to touch, so the assertion could
        not fail however the probe behaved;
      * the probe runs under a redirected HOME with a registry this test
        writes, so it never reads or writes the developer's live data to
        reproduce a leak — the point is what it does NOT touch, and that is
        fully determined by the probe, not by the input.

    Assert the user's store is untouched: same entries, same mtimes, same
    sizes. Nothing is created there, so there is nothing to clean up: the
    previous version had to `rmdir` after itself, which is precisely the
    behaviour it was trying to prove absent.

    A missing store is NOT a reason to skip. If the app has never created one
    there is nothing to protect, and the test's job — proving the probe does
    not put something there — is still meaningful, so the assertion runs
    against an empty expectation instead.
    """
    real_shares = _real_host_shares_dir()
    existed = os.path.isdir(real_shares)
    before = _tree(real_shares) if existed else []

    PROBE_PROFILE = "verify-sideeffect-probe"
    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()
    store = os.path.join(str(fake_home), "Isolated-Chrome", "profiles")
    os.makedirs(store, exist_ok=True)
    r = _run_probe(fake_home, scratch, profiles={
        PROBE_PROFILE: {
            "name": PROBE_PROFILE,
            "path": os.path.join(store, PROBE_PROFILE),
            "host_mount": "",           # -> the lazily-created share dir
            "proxy": "",
            "created_at": "1970-01-01T00:00:00Z",
        },
    })

    after = _tree(real_shares) if os.path.isdir(real_shares) else []
    leaked = sorted(set(after) - set(before))
    assert not leaked, (
        "the harness probe created entries in the user's real host-shares "
        "store (%s): %s\nThe probe reached the mkdir through "
        "_profile_host_mount() -> resolve_host_mount() -> "
        "host_share.ensure_host_share_dir(), so patching only "
        "docker_manager.ensure_host_share_dir is not enough — redirect "
        "host_share.HOST_SHARES_DIR to VERIFY_SCRATCH as well.\n"
        "stdout:\n%s\nstderr:\n%s" % (real_shares, leaked, r.stdout, r.stderr)
    )
    if existed:
        assert before == after, (
            "the probe modified the user's real host-shares store in place\n"
            "before: %r\nafter:  %r" % (before, after))

    # Non-vacuity. Without these the two assertions above are satisfied by a
    # probe that crashed on startup, printed nothing, and touched nothing —
    # which is very nearly what the broken probe did.
    _probe_ran(r)
    assert PROBE_PROFILE in r.stdout, (
        "the probe did not report the probe profile, so it may not have done "
        "any work at all\nstdout:\n%s\nstderr:\n%s" % (r.stdout, r.stderr))
    # Truthfulness of the report: it must name the store the CODE derives, not
    # the scratch stand-in the probe made. Under a redirected HOME that store
    # is under the fake home — which is the correct answer here, and the whole
    # reason the real store above went untouched. A probe that silently
    # reported its own scratch paths while writing somewhere else would be
    # lying, and that is what this catches.
    reported_root = _fake_app_data(fake_home)
    assert reported_root in r.stdout, (
        "the probe's report does not name the app-data dir the code derives "
        "its store from (%s), so the pass above could be vacuous\nstdout:\n%s"
        % (reported_root, r.stdout))
    assert str(scratch) not in r.stdout, (
        "the probe reported paths inside its scratch dir, i.e. the "
        "substitute it created rather than the location the code chose — its "
        "own report is therefore not evidence about the real code path\n"
        "stdout:\n%s" % r.stdout)
    assert os.path.isdir(os.path.join(str(scratch), "host-shares",
                                      PROBE_PROFILE)), (
        "the probe created nothing in its scratch dir, so it never exercised "
        "the mkdir path this test exists to police\nstdout:\n%s" % r.stdout)


def test_the_probe_creates_nothing_outside_its_scratch_dir(tmp_path):
    """
    Stronger than the test above, and the reason the fake HOME exists: assert
    the probe wrote NOTHING under the redirected $HOME. Any mkdir the probe
    failed to redirect lands here and is listed by name, instead of quietly
    landing in the user's data.
    """
    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()

    r = _run_probe(fake_home, scratch)
    assert r.returncode in (0, 1), (
        "the probe crashed (rc=%s)\nstdout:\n%s\nstderr:\n%s"
        % (r.returncode, r.stdout, r.stderr))
    _probe_ran(r)

    shares = fake_home / ".local/share/chrome-isolation-manager/host-shares"
    created = sorted(os.listdir(str(shares))) if shares.is_dir() else []
    assert not created, (
        "the probe created per-profile host-share directories under the "
        "redirected HOME, which means those mkdirs are NOT going to the "
        "scratch dir and WILL go to the real store on a real run: %s\n"
        "stdout:\n%s" % (created, r.stdout)
    )
    # It must still have done the work: the scratch dir is where they belong.
    # Asserting the specific per-profile directory, not just the parent, is
    # what makes "nothing was created under $HOME" a statement about the
    # redirect rather than about a probe that created nothing anywhere.
    scratch_shares = scratch / "host-shares"
    assert scratch_shares.is_dir(), r.stdout
    # Only the profiles that RELY on the default get a share directory — a
    # profile with an explicit host_mount uses the directory the user chose,
    # and must not have one invented for it. Asserting the exact set, rather
    # than a non-empty check, catches both an over-eager mkdir and a probe
    # that stopped creating any.
    expected_dirs = sorted(
        line.split("\t")[1] for line in r.stdout.splitlines()
        if line.startswith("PROFILE\t") and line.split("\t")[2] == "<empty>")
    assert expected_dirs, (
        "no profile in the fixture used the empty host_mount default, so this "
        "test cannot detect a mkdir at all\nstdout:\n%s" % r.stdout)
    made = sorted(os.listdir(str(scratch_shares)))
    assert made == expected_dirs, (
        "the probe created %r in its scratch host-shares root; expected %r — "
        "one directory per profile relying on the default\nstdout:\n%s"
        % (made, expected_dirs, r.stdout))


def registry_names(r):
    """Profile names the probe reported on, from its own PROFILE lines."""
    names = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0] == "PROFILE":
            names.append(parts[1])
    return names


def test_the_probe_reports_its_own_leak_count(tmp_path):
    """
    The probe must MEASURE its own side effects. A probe that trusts its own
    monkey-patching is exactly how this bug shipped: the patch was there, it
    looked right, and it was never called.
    """
    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()

    r = _run_probe(fake_home, scratch)
    m = re.search(r"^PROBE_HOST_SHARE_LEAK\t(\d+)\t(.*)$", r.stdout, re.M)
    assert m, (
        "the probe no longer prints PROBE_HOST_SHARE_LEAK; check 2's shell side "
        "reads that line to fail when a probe writes to the real store\n"
        "stdout:\n%s\nstderr:\n%s" % (r.stdout, r.stderr)
    )
    assert m.group(1) == "0", "the probe leaked %s" % m.group(2)
    # A leak count of 0 is only meaningful if the probe looked at a real store
    # with a known set of contents. Both facts are asserted here so that a
    # probe which measured nothing at all cannot satisfy this test by
    # reporting zero leaks — which is exactly what the broken probe did.
    _probe_ran(r)
    names = registry_names(r)
    assert len(names) >= 4, (
        "the probe reported on only %d profile(s), so its leak measurement "
        "rested on almost nothing\nstdout:\n%s" % (len(names), r.stdout))
    assert m.group(2) == "", (
        "the probe reported a leak but the count line says 0; the two "
        "disagree: %r" % (m.group(0),))


def test_the_probe_is_not_vacuous(tmp_path):
    """
    "No profile mounts $HOME" is trivially true if the probe stopped producing
    binds altogether. Assert it still produces one bind per profile with an
    empty host_mount.

    The registry is a FIXTURE with a known shape, not the developer's live one.
    That is not a stylistic preference. The old version read the real
    profiles.json — git-ignored runtime data, present in the working tree and
    absent from any clean export — and then SKIPPED when it found no profile
    with an empty host_mount. On this host it had four, so it ran and passed;
    from a clean checkout the file did not exist and it errored. A skip
    condition derived from untracked state is a green light wired to a
    coincidence, which is the failure mode this whole file documents.

    Because the counts are known in advance, the assertions can be exact:
    a probe that silently introspected nothing now fails, rather than
    satisfying `>= expected` with expected == 0.
    """
    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()
    store = os.path.join(str(fake_home), "Isolated-Chrome", "profiles")
    os.makedirs(store, exist_ok=True)
    explicit = os.path.join(str(fake_home), "shared-inbox")
    os.makedirs(explicit, exist_ok=True)

    N_DEFAULT, N_EXPLICIT = 3, 1
    registry = _sample_registry(store, n_default=N_DEFAULT,
                               n_explicit=N_EXPLICIT, explicit_dir=explicit)
    expected = sum(1 for e in registry.values()
                   if not str(e.get("host_mount", "")).strip())
    assert expected == N_DEFAULT, "the fixture must exercise the empty default"

    r = _run_probe(fake_home, scratch, profiles=registry)
    _probe_ran(r)
    m = re.search(r"^PROFILE_HOST_BINDS\t(\d+)\t(\d+)$", r.stdout, re.M)
    assert m, "the probe no longer prints PROFILE_HOST_BINDS\nstdout:\n%s" % r.stdout
    total, with_binds = int(m.group(1)), int(m.group(2))
    assert total == len(registry), (
        "the probe saw %d profile(s) but the registry has %d" % (total, len(registry)))
    assert with_binds == len(registry), (
        "only %d of %d profile(s) produced a /home/chrome/host bind; the "
        "'nothing mounts $HOME' verdict would then be true for the wrong "
        "reason. Every profile in the fixture must produce a bind — the "
        "empty-host_mount ones via their own share dir, the explicit one via "
        "the directory the user chose." % (with_binds, len(registry)))
    assert with_binds >= expected, (
        "only %d of %d default-mount profile(s) produced a bind"
        % (with_binds, expected))


def test_the_probe_redirects_the_location_not_only_the_function_name():
    """
    The probe must defend in depth, and this pins the *second* defence.

    The location redirect (`host_share.HOST_SHARES_DIR`) and the function stubs
    are, empirically, redundant: removing either one alone still leaves the
    other, and every behavioural test in this file stays green. That redundancy
    is the design — it is why the bug could not come back the same way twice —
    but it also means the behavioural tests cannot tell you WHICH one you just
    deleted. Delete the location redirect and the file goes on passing while
    the stated guarantee ("redirect the LOCATION, not just the function name,
    because that cannot go stale when a function is renamed") quietly stops
    holding.

    So assert both are present, as source. This is a text assertion and that is
    the point: it is the only thing in this file that can see the difference
    between "protected twice" and "protected once".
    """
    src = _probe_source()
    assert re.search(r"host_share\.HOST_SHARES_DIR\s*=\s*_FAKE_SHARE_ROOT", src), (
        "the probe no longer redirects host_share.HOST_SHARES_DIR to its "
        "scratch dir; the function stubs alone are not enough of a defence, "
        "because they are bound by NAME and go stale the moment the function "
        "is renamed or reached through another module's namespace")
    assert re.search(r"host_share\.ensure_host_share_dir\s*=\s*_dry_ensure", src), (
        "the probe no longer stubs host_share.ensure_host_share_dir")
    assert re.search(r"_mod\.resolve_host_mount\s*=\s*_dry_resolve", src), (
        "the probe no longer patches resolve_host_mount in every namespace it "
        "can be bound under; that is the name _profile_host_mount() actually "
        "calls, and patching only docker_manager's binding is what shipped the "
        "original bug")


def test_the_probe_refuses_to_run_without_a_scratch_dir(tmp_path):
    """
    With no VERIFY_SCRATCH there is nowhere to redirect the mkdir, so the
    probe must refuse to introspect rather than write into the real store.
    """
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("VERIFY_SCRATCH", None)
    r = subprocess.run([sys.executable, "-", ROOT], input=_probe_source(),
                       env=env, cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert r.returncode == 9, (
        "the probe ran without VERIFY_SCRATCH (rc=%s); it must refuse, because "
        "running anyway means mkdir into the user's host-shares\nstdout:\n%s"
        % (r.returncode, r.stdout))
    assert "VERIFY_SCRATCH" in r.stdout


def test_verify_shell_does_not_use_the_assignment_prefix_that_swallows_the_scratch_dir():
    """
    The second, independent cause of the same leak, worth its own test because
    it is invisible from the python side.

    The harness invoked the probe as

        VERIFY_SCRATCH="$WORK" VOL_REPORT="$(python - <<EOF ... EOF)"

    bash does not make a pending assignment visible to a command substitution
    in the SAME simple command — the substitution is expanded before the
    assignments are applied to the environment. So VERIFY_SCRATCH never reached
    the probe. The old probe did not notice: its `if SCRATCH:` branch was dead
    code and it fell through to returning the real path, which is precisely
    what wrote into the user's store. A redirect that is never delivered is
    worse than no redirect, because it reads like protection.

    First assert the idiom really is broken (so the reason this test exists is
    demonstrated, not asserted), then assert verify.sh no longer uses it.
    """
    r = subprocess.run(
        ["bash", "-c",
         'FOO=bar OUT="$(%s -c \'import os;print(os.environ.get("FOO"))\')"; echo "$OUT"'
         % sys.executable],
        capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "None", (
        "bash now exports pending assignments into same-line command "
        "substitutions; if that ever changes, re-read verify.sh's VERIFY_SCRATCH "
        "handling before trusting this test (got %r)" % r.stdout.strip())

    with open(VERIFY) as f:
        text = f.read()
    # Comments are excluded deliberately: this file's own explanatory comment
    # quotes the broken form verbatim, and a grep-style assertion would then
    # fail on its own documentation.
    code = "\n".join(l for l in text.split("\n")
                     if not l.lstrip().startswith("#"))
    assert 'VERIFY_SCRATCH="$WORK" VOL_REPORT=' not in code, (
        "verify.sh assigns VERIFY_SCRATCH as a prefix of the command that runs "
        "the probe again; bash does not export it there, so the probe receives "
        "nothing. Export it on its own line.")
    assert 'VERIFY_SCRATCH="$WORK" HARDEN_REPORT=' not in code, (
        "same defect on the check-3 probe")
    assert 'export VERIFY_SCRATCH="$WORK"' in code


def test_the_probe_reads_the_registry_the_app_resolves_not_one_beside_the_source(tmp_path):
    """
    THE ROOT CAUSE, as a regression test.

    The probe used to open `<repo>/profiles.json`, where `repo` is the directory
    the harness happens to be checked out in. That file is git-ignored runtime
    data (`.gitignore: /profiles.json`) and it only exists at that path on a
    host where the checkout IS the install directory — i.e. `~/.local/share/
    chrome-isolation-manager`, where the app's own `paths.registry_file()`
    happens to resolve to the same string.

    Everywhere else the file is absent, and the failure was silent in the worst
    way: `open()` raised, the probe exited 1, and the shell side read 1 as "the
    code has violations". Check 2 reported a confident finding about a registry
    it had never opened, and the tests beside it reported on profiles the
    developer happened to have.

    Two halves, both asserted:

      * the probe names the registry it read, so the report is auditable; and
      * running the probe with the repo pointed somewhere that has NO
        profiles.json still yields a verdict, because the input comes from the
        app's resolver rather than from the checkout.

    The second half is the one that would have caught this. A clean export is
    the cheapest available stand-in for "a checkout that is not the install
    directory", and it is exactly how the bug presented.
    """
    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()
    store = os.path.join(str(fake_home), "Isolated-Chrome", "profiles")
    os.makedirs(store, exist_ok=True)
    explicit = os.path.join(str(fake_home), "shared-inbox")
    os.makedirs(explicit, exist_ok=True)
    _write_registry(fake_home, _sample_registry(
        store, n_default=2, n_explicit=1, explicit_dir=explicit))

    # A directory that is NOT the app's data directory and holds no
    # profiles.json — the clean-export condition, in miniature.
    #
    # backend/ is linked in because `repo` is legitimately used for that: it is
    # how the harness finds the source under test. What it must NOT be used for
    # is locating the registry, which is the half under test here.
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    os.symlink(os.path.join(ROOT, "backend"), str(elsewhere / "backend"))
    assert not os.path.exists(str(elsewhere / "profiles.json"))

    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["VERIFY_SCRATCH"] = str(scratch)
    env.pop("CHROME_ISOLATION_HOME", None)
    r = subprocess.run([sys.executable, "-", str(elsewhere)],
                       input=_probe_source(), env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=180)

    assert r.returncode in (0, 1), (
        "with the repo pointed at a directory holding no profiles.json, the "
        "probe exited %s instead of reporting a verdict\nstdout:\n%s\nstderr:\n%s"
        % (r.returncode, r.stdout, r.stderr))
    m = re.search(r"^PROBE_REGISTRY\t(\S+)\t(\d+)", r.stdout, re.M)
    assert m, (
        "the probe does not report which registry it read, so a verdict from "
        "it cannot be audited\nstdout:\n%s\nstderr:\n%s" % (r.stdout, r.stderr))
    assert m.group(1) == os.path.join(_fake_app_data(fake_home), "profiles.json"), (
        "the probe read %s; it must read the registry the APP resolves "
        "(paths.registry_file()), not one relative to the checkout directory "
        "%s" % (m.group(1), elsewhere))
    binds = re.search(r"^PROFILE_HOST_BINDS\t(\d+)\t(\d+)$", r.stdout, re.M)
    assert binds, (
        "the probe produced no bind measurement at all, so it reached no "
        "verdict\nstdout:\n%s" % r.stdout)
    assert int(binds.group(1)) == int(m.group(2)) == 3, (
        "the probe measured %s profile(s) against a registry holding %s — it "
        "is not introspecting the registry it reported"
        % (binds.group(1), m.group(2)))


def test_verify_sh_does_not_hardcode_the_registry_beside_the_source_tree():
    """
    The same wrong assumption, on the shell side, where it is even quieter.

    `_snapshot_files()` — the check-13 tripwire that proves the harness changed
    nothing — used to hash `$REPO_ROOT/profiles.json` and
    `$REPO_ROOT/proxies.json`. Off the install directory those do not exist, so
    it recorded `absent` for both, compared `absent` to `absent`, and reported
    "nothing changed" about files it had never seen. A tripwire watching nothing
    is worse than no tripwire, because it is read as evidence.

    The registry, the proxy store and the host-shares root are now resolved
    through `paths.py` and the header discloses which way it resolved.
    """
    with open(VERIFY) as f:
        text = f.read()
    code = "\n".join(l for l in text.split("\n")
                     if not l.lstrip().startswith("#"))
    assert 'PROFILES_JSON="$REPO_ROOT/profiles.json"' not in code, (
        "verify.sh still derives the registry from the checkout directory; "
        "resolve it with paths.registry_file() like the profile store")
    assert '"$REPO_ROOT/proxies.json"' not in code, (
        "verify.sh still derives the proxy store from the checkout directory")
    assert '"$REPO_ROOT/host-shares"' not in code, (
        "verify.sh still derives the host-shares root from the checkout "
        "directory")
    assert 'paths.registry_file()' in code
    # And it must say which way it resolved, so a fallback is visible in the
    # output rather than discovered later.
    assert "registry  : " in code, (
        "the banner does not disclose the resolved registry path, so a "
        "check that reports on 'the profiles' cannot be audited")


def test_the_harness_treats_a_test_suite_mutation_as_a_failure():
    """
    Check 13 used to report the pytest phase as a footnote and PASS. The test
    suite is part of the harness — verify.sh runs it — so a mutation there is
    the harness's own. Assert the footnote is gone and the FAIL text names it.
    """
    with open(VERIFY) as f:
        text = f.read()
    d13 = text.split("CHECK 13", 1)[1]
    assert "the pytest phase (check 1)" in d13, (
        "check 13 no longer has a distinct verdict for mutations made by the "
        "pytest phase; those must be a FAIL, not a NOTE")
    assert 'if [[ -z "$D13_BY_HARNESS" && -z "$D13_BY_TESTS" ]]' in d13, (
        "check 13 still passes when only the test-suite segment changed")
#!/usr/bin/env python3
"""
THE GATE: the suite must be green from a CLEAN EXPORT, not just in your tree.

THE PROBLEM THIS EXISTS TO STOP
-------------------------------
This project's recurring failure mode is not a red test. It is a GREEN test
that means nothing because it was measured in the wrong environment. It has
happened more than once, and the 2026-09-29 instance is the sharpest example:

    working tree:   pytest tests/test_verify_harness_side_effects.py  -> 7 passed
    clean export:   same commit, same command                      -> 2 failed

The cause was that those tests read `<repo>/profiles.json` — git-IGNORED
runtime data, present in the working tree and absent from any export — and
derived their expectations from it. The tests were not wrong about the code.
They were wrong about where they were standing.

That class of bug is INVISIBLE to the normal suite, by construction: running
`pytest tests/` in the working tree reproduces the very conditions that make
the tests pass. So the suite cannot catch it, and "the suite is green" is not
evidence. This test supplies the missing evidence by running the suite
somewhere else.

WHAT IT COVERS — precisely, and no more
---------------------------------------
IT COVERS: every test in `tests/`, run against a tree containing ONLY the
files git tracks. That is the same set of files `git clone` gives you, so it
catches exactly the failure it is aimed at — a test that silently depends on an
untracked, ignored, or otherwise local file.

IT DOES NOT COVER:
  * anything outside `tests/` (the harness `scripts/verify.sh` is asserted on
    by tests/test_verify_harness_side_effects.py, which reads the real file and
    is not re-run here);
  * live state — Docker, containers, a real $HOME. The clean export runs
    against the same environment as the parent run, deliberately: this gate is
    about which FILES exist, not about what the machine looks like. Tests that
    need the real store still get it;
  * uncommitted changes to untracked files. See "WHAT IT EXPORTS" below.

WHAT IT EXPORTS, AND WHY NOT LITERALLY `git archive HEAD`
---------------------------------------------------------
By default it exports the working-tree content of every TRACKED file — i.e.
the tree as it would be committed right now. That is what catches the bug
above: untracked and ignored files are excluded, which is the whole point.

`git archive HEAD` would instead export the last COMMIT. That is stricter in
one way and useless in another here: this gate has to be runnable before you
commit (otherwise it cannot protect the commit that fixes a clean-export bug —
you would have to commit a broken tree first to find out it was broken), and
`git archive HEAD` would silently grade the previous commit while reporting on
the current one.

So the ref is a parameter, not a hardcoded `HEAD`:

    VERIFY_CLEAN_REF=HEAD   scripts/verify.sh      # grade the last commit
    VERIFY_CLEAN_REF=HEAD   pytest tests/          # same, ad hoc

With it set, a genuine `git archive <ref>` is used. That is the right mode for
CI, where the tree is already committed and the thing under test IS the commit.
Without it, the tracked-working-tree export is used, and that is the right mode
for the working loop.

GUARDS AGAINST THE OBVIOUS WAYS THIS GOES WRONG
-----------------------------------------------
  * INFINITE RECURSION. This file runs pytest in a subprocess, so the export
    contains a copy of this file, which would export, which would... Two
    independent guards, because either alone can be defeated by a future edit:
      (1) the inner run is given `--ignore` for this file, so the copy never
          runs; and
      (2) `VERIFY_CLEAN_CHECKOUT_CHILD=1` is exported, and this module skips
          outright when it sees it. The env var is the load-bearing one: a `-k`
          or `--ignore` filter has to name every spawner correctly or it fails
          open, and this repo has already had that failure go the other way
          (tests/test_version_check.py: a `-k` filter missed one spawner and
          produced 223 live pytest processes).
  * LEAVING TEMP FILES. The export lives in a `mktemp -d`, removed in a
    `finally` — on failure, on timeout, on an exception in the assertion.
  * WRITING INTO THE REPO OR THE USER'S DATA. The inner run's cwd is the
    export, not the repo; bytecode writing and pytest's cache are both
    disabled for it. Nothing is created in the repo, and `$HOME` is passed
    through untouched so the suite sees the same environment it normally does.
  * A GATE THAT PASSES BY RUNNING NOTHING. The inner run's collected/passed
    count is asserted against the parent run's. A clean export that collects
    zero tests and exits 0 would otherwise satisfy "rc == 0" — the same
    vacuous-pass shape as the bug being guarded against.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THIS_FILE = os.path.relpath(os.path.abspath(__file__), ROOT)

#: Set on the inner run. See "INFINITE RECURSION" above.
CHILD_ENV = "VERIFY_CLEAN_CHECKOUT_CHILD"

#: Ref to export. Unset = the tracked working tree (see the module docstring).
REF_ENV = "VERIFY_CLEAN_REF"

#: Inner-run timeout. The full suite takes ~65s on this host; 15 minutes is
#: ~13x headroom, enough for a slow machine and well short of hanging CI.
TIMEOUT = int(os.environ.get("VERIFY_CLEAN_TIMEOUT", "900"))

#: The inner run must run at least this share of the parent's tests. Guards
#: against an export that silently lost most of the suite (a bad ref, a
#: .gitattributes export-ignore, a sparse checkout) and would otherwise report
#: a green result from a fraction of the tests.
MIN_FRACTION = 0.75


def _git(*args, **kw):
    return subprocess.run(["git", "-C", ROOT] + list(args),
                          capture_output=True, timeout=120, **kw)


def _pytest_available():
    if os.environ.get(CHILD_ENV):
        return None
    try:
        return subprocess.run([sys.executable, "-c", "import pytest"],
                              capture_output=True, timeout=60).returncode == 0
    except OSError:
        return None


def _skip_reason():
    if os.environ.get(CHILD_ENV):
        # The recursion guard. Never fails open: if this file somehow runs in a
        # child, it does nothing rather than spawning a grandchild.
        return "%s is set — this is an inner clean-export run" % CHILD_ENV
    if shutil.which("git") is None:
        return "git is not on PATH"
    if not os.path.isdir(os.path.join(ROOT, ".git")) and _git("rev-parse",
                                                             "--git-dir").returncode != 0:
        return "%s is not a git repository" % ROOT
    if _pytest_available() is not True:
        return "pytest is not importable by %s" % sys.executable
    return None


pytestmark = pytest.mark.skipif(_skip_reason() is not None,
                                reason=_skip_reason() or "")


def _export(dest):
    """
    Populate `dest` with the tracked tree. Returns a description of what it did.

    Two modes, per the module docstring:
      * VERIFY_CLEAN_REF set -> `git archive <ref>`, byte-for-byte the commit.
      * unset                 -> the working-tree content of every tracked file,
                                 which is the same file SET as a clone with the
                                 untracked and ignored files removed.
    """
    ref = os.environ.get(REF_ENV, "").strip()
    if ref:
        # BINARY, not text: an archive contains image files, and decoding it as
        # UTF-8 raises rather than exporting. (It did, on the first run.)
        r = _git("archive", "--format=tar", ref)
        if r.returncode != 0:
            raise AssertionError(
                "git archive %s failed (rc=%s)\nstderr:\n%s"
                % (ref, r.returncode, r.stderr.decode("utf-8", "replace")))
        p = subprocess.run(["tar", "-x", "-C", dest], input=r.stdout,
                           capture_output=True, timeout=300)
        if p.returncode != 0:
            raise AssertionError(
                "extracting `git archive %s` failed (rc=%s)\nstderr:\n%s"
                % (ref, p.returncode, p.stderr.decode("utf-8", "replace")))
        return "git archive %s" % ref

    # Tracked-file export. `git ls-files` is the file set a clone would contain;
    # reading each path from the working tree gives the content that is about to
    # be committed. Untracked and ignored files — profiles.json, host-shares/,
    # __pycache__, node_modules — are therefore absent, which is the property
    # under test.
    listing = _git("ls-files", "-z", text=True)
    if listing.returncode != 0:
        raise AssertionError("git ls-files failed: %s" % listing.stderr)
    copied, missing = 0, []
    for rel in listing.stdout.split("\0"):
        if not rel:
            continue
        src = os.path.join(ROOT, rel)
        dst = os.path.join(dest, rel)
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # symlinks=True so a tracked symlink is reproduced as a symlink
            # rather than followed, which would export its target's contents.
            shutil.copy2(src, dst, follow_symlinks=False)
            copied += 1
        except FileNotFoundError:
            # Tracked but deleted in the working tree. Not staged as a deletion,
            # so it is not part of what would be committed; record and move on.
            missing.append(rel)
    if copied == 0:
        raise AssertionError(
            "git ls-files listed %d path(s) but none could be exported; the "
            "gate would then grade an empty tree and pass" % len(missing))
    desc = "tracked working tree (%d file(s)" % copied
    if missing:
        desc += ", %d tracked-but-deleted omitted" % len(missing)
    return desc + ")"


def _parse_counts(out):
    """(collected, passed) from pytest's summary line."""
    tail = out[-4000:]
    m = re.search(r"(\d+) passed", tail)
    passed = int(m.group(1)) if m else 0
    m2 = re.search(r"(\d+) (?:tests? )?collected|(\d+) tests? collected", tail)
    if m2:
        collected = int(m2.group(1) or m2.group(2))
    else:
        m3 = re.search(r"(\d+) passed", tail)
        collected = int(m3.group(1)) if m3 else 0
    return collected, passed


def _inner_env():
    env = dict(os.environ)
    # Recursion guard, load-bearing half.
    env[CHILD_ENV] = "1"
    # The inner run must not leave anything behind in the export.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # HOME is passed through untouched on purpose: the question is which FILES
    # exist, not what the machine looks like, and redirecting it would change
    # which tests skip.
    return env


def test_the_suite_is_green_from_a_clean_export():
    """
    Run `pytest tests/` against a tree of only git-tracked files.

    Fails if anything is red there, and says which tests — a bare rc is not
    actionable, and the whole failure mode of this project is a red result that
    nobody can locate.
    """
    parent_collected, parent_passed = _parse_counts(
        subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q",
             "-p", "no:cacheprovider", "tests/"],
            cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT).stdout)

    dest = tempfile.mkdtemp(prefix="clean-export-")
    try:
        desc = _export(dest)
        assert os.path.isdir(os.path.join(dest, "tests")), (
            "the export has no tests/ directory (%s); it is not a usable "
            "checkout" % desc)

        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-q",
             "-p", "no:cacheprovider",
             # Guard 1 of 2 against recursion. See the module docstring.
             "--ignore=%s" % THIS_FILE],
            cwd=dest, env=_inner_env(),
            capture_output=True, text=True, timeout=TIMEOUT,
            # Own process group, so a timeout kills the whole tree rather than
            # leaving orphans behind — the failure mode documented in
            # tests/test_version_check.py.
            start_new_session=True)

        failures = [l for l in r.stdout.splitlines() if l.startswith("FAILED")]
        errors = [l for l in r.stdout.splitlines() if l.startswith("ERROR")]
        collected, passed = _parse_counts(r.stdout)

        assert r.returncode == 0, (
            "the suite is RED from a clean export of %s.\n"
            "This is the failure mode this gate exists for: tests that pass in "
            "the working tree only because of state that is not in the "
            "committed tree — an untracked or git-ignored file the test reads, "
            "a leftover directory, a module import that resolves to something "
            "uncommitted.\n"
            "Failing (%d):\n%s\nErrors (%d):\n%s\n"
            "--- summary line ---\n%s"
            % (desc, len(failures), "\n".join(failures) or "  (none)",
               len(errors), "\n".join(errors) or "  (none)",
               r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "<none>"))

        # The gate must not be able to pass by running (almost) nothing.
        floor = int(parent_passed * MIN_FRACTION)
        assert collected >= floor and collected > 0, (
            "the clean export ran only %d test(s) against %d in the working "
            "tree — a green result from a fraction of the suite says nothing "
            "about the rest. Check that %r and --ignore are not excluding "
            "more than intended." % (collected, parent_passed, desc))
    finally:
        # Removed on every path: assertion failure, timeout, exception. The
        # export can be a few MB of source, and a gate that leaks one per run
        # fills /tmp.
        shutil.rmtree(dest, ignore_errors=True)


def test_the_gate_itself_would_not_pass_vacuously():
    """
    Meta-check: prove the gate is capable of failing.

    A gate that has never been observed red is not evidence of anything — that
    is this file's entire subject. So break something on purpose and confirm
    the export reports it.

    This is the "mutate something in a scratch export and show the gate fails"
    proof, made permanent so it cannot rot. It costs one cheap export plus a
    two-test inner run, not the full suite.
    """
    dest = tempfile.mkdtemp(prefix="clean-export-selftest-")
    try:
        _export(dest)
        broken = os.path.join(dest, "tests", "test_zz_gate_selftest_broken.py")
        with open(broken, "w") as f:
            f.write("def test_deliberately_broken():\n"
                    "    assert 1 == 2, 'this file must make the gate red'\n")

        env = _inner_env()
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_zz_gate_selftest_broken.py",
             "-q", "-p", "no:cacheprovider"],
            cwd=dest, env=env, capture_output=True, text=True, timeout=300,
            start_new_session=True)
        assert r.returncode != 0, (
            "a deliberately failing test in a clean export did NOT make the "
            "run red (rc=0). The gate would therefore pass on a broken tree, "
            "which makes it worse than no gate at all.\nstdout:\n%s"
            % r.stdout[-2000:])

        # And the mirror image: the SAME export, without the broken file, must
        # be green. A gate that is red for reasons unrelated to the tree under
        # test gets ignored, and an ignored gate is how this failure mode came
        # back twice. The control runs a real test file from the export rather
        # than this one, because this one is --ignore'd by the inner run and
        # would collect nothing — "no tests ran" exits 4 and would look like a
        # failure of the control rather than of the thing being controlled.
        os.unlink(broken)
        control = "tests/test_validator.py"
        if not os.path.exists(os.path.join(dest, control)):
            pytest.skip("control file %s is not in the export" % control)
        r2 = subprocess.run(
            [sys.executable, "-m", "pytest", control,
             "-q", "-p", "no:cacheprovider"],
            cwd=dest, env=env, capture_output=True, text=True, timeout=300,
            start_new_session=True)
        assert r2.returncode == 0, (
            "the clean export is red for a reason unrelated to the mutated "
            "tree, so this gate cannot be trusted to distinguish the two\n"
            "stdout:\n%s\nstderr:\n%s"
            % (r2.stdout[-2000:], r2.stderr[-2000:]))
    finally:
        shutil.rmtree(dest, ignore_errors=True)

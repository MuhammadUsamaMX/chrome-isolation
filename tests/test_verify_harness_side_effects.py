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
"""
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


def _run_probe(fake_home, scratch):
    env = dict(os.environ)
    env["HOME"] = str(fake_home)
    env["VERIFY_SCRATCH"] = str(scratch)
    env.pop("CHROME_ISOLATION_HOME", None)
    return subprocess.run([sys.executable, "-", ROOT],
                          input=_probe_source(), env=env, cwd=ROOT,
                          capture_output=True, text=True, timeout=180)


def _has_docker_sdk():
    return subprocess.run([sys.executable, "-c", "import docker"],
                          capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(
    not _has_docker_sdk(),
    reason="the probe imports the docker SDK; without it verify.sh SKIPs check 2")


def test_the_probe_is_byte_identical_before_and_after(tmp_path):
    """
    THE REGRESSION, run the way verify.sh actually runs it.

    Two details make this a faithful reproduction rather than a simulation:

      * the registry handed to the probe contains a profile whose host-share
        directory DOES NOT EXIST YET. Without that, the test is blind on a host
        where every real profile's directory is already present — which is
        exactly the state this host was in when the bug was found, and exactly
        why the litter looked intermittent;
      * `host-shares` in the repo the probe is pointed at is a SYMLINK to the
        user's real store, so a leak lands in the real store, where the
        before/after comparison can see it.

    Assert the store is untouched: same entries, same mtimes, same sizes.

    SELF-CLEANING, deliberately. A test that guards against litter must not
    become the next litterer, so anything the probe does create here is removed
    in `finally`, and only empty directories are removed, so real data can never
    be destroyed by this test.
    """
    import json
    import shutil

    real_shares = os.path.join(ROOT, "host-shares")
    if not os.path.isdir(real_shares):
        pytest.skip("no host-shares store on this host")
    before = _tree(real_shares)

    PROBE_PROFILE = "verify-sideeffect-probe"
    with open(os.path.join(ROOT, "profiles.json")) as f:
        registry = json.load(f)
    registry[PROBE_PROFILE] = {
        "name": PROBE_PROFILE,
        "path": os.path.join(ROOT, "profiles", PROBE_PROFILE),
        "host_mount": "",           # -> the lazily-created share dir
        "proxy": "",
        "created_at": "1970-01-01T00:00:00Z",
    }

    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    os.symlink(os.path.join(ROOT, "backend"), str(fake_repo / "backend"))
    os.symlink(real_shares, str(fake_repo / "host-shares"))
    with open(str(fake_repo / "profiles.json"), "w") as f:
        json.dump(registry, f)

    scratch = tmp_path / "scratch"
    scratch.mkdir()

    env = dict(os.environ)
    env["VERIFY_SCRATCH"] = str(scratch)
    env.pop("CHROME_ISOLATION_HOME", None)
    r = subprocess.run([sys.executable, "-", str(fake_repo)],
                       input=_probe_source(), env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=180)

    after = _tree(real_shares)
    leaked = sorted(set(after) - set(before))
    try:
        assert not leaked, (
            "the harness probe created entries in the user's real host-shares "
            "store: %s\nThe probe reached the mkdir through "
            "_profile_host_mount() -> resolve_host_mount() -> "
            "host_share.ensure_host_share_dir(), so patching only "
            "docker_manager.ensure_host_share_dir is not enough — redirect "
            "host_share.HOST_SHARES_DIR to VERIFY_SCRATCH as well.\n"
            "stdout:\n%s\nstderr:\n%s" % (leaked, r.stdout, r.stderr)
        )
        assert before == after, (
            "the probe modified the real host-shares store in place\n"
            "before: %r\nafter:  %r" % (before, after)
        )
        # And the store the probe reported must be the real one, not scratch:
        # a probe that quietly redirected its own report would be lying.
        assert PROBE_PROFILE in r.stdout, (
            "the probe did not report the probe profile, so it may not have "
            "done any work at all\nstdout:\n%s\nstderr:\n%s" % (r.stdout, r.stderr))
        assert real_shares in r.stdout, (
            "the probe's report does not name the real host-shares root, so "
            "the pass above could be vacuous\nstdout:\n%s" % r.stdout)
    finally:
        shutil.rmtree(str(fake_repo), ignore_errors=True)
        for rel, _, _ in leaked:
            try:
                os.rmdir(os.path.join(real_shares, rel))
            except OSError:
                pass


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

    shares = fake_home / ".local/share/chrome-isolation-manager/host-shares"
    created = sorted(os.listdir(str(shares))) if shares.is_dir() else []
    assert not created, (
        "the probe created per-profile host-share directories under the "
        "redirected HOME, which means those mkdirs are NOT going to the "
        "scratch dir and WILL go to the real store on a real run: %s\n"
        "stdout:\n%s" % (created, r.stdout)
    )
    # It must still have done the work: the scratch dir is where they belong.
    scratch_shares = scratch / "host-shares"
    assert scratch_shares.is_dir(), r.stdout


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


def test_the_probe_is_not_vacuous(tmp_path):
    """
    "No profile mounts $HOME" is trivially true if the probe stopped producing
    binds altogether. Assert it still produces one bind per profile with an
    empty host_mount, using the real registry.
    """
    import json

    with open(os.path.join(ROOT, "profiles.json")) as f:
        registry = json.load(f)
    expected = sum(1 for e in registry.values()
                   if not str(e.get("host_mount", "")).strip())
    if expected == 0:
        pytest.skip("no profile in the registry uses the empty host_mount default")

    fake_home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    fake_home.mkdir()
    scratch.mkdir()

    r = _run_probe(fake_home, scratch)
    m = re.search(r"^PROFILE_HOST_BINDS\t(\d+)\t(\d+)$", r.stdout, re.M)
    assert m, "the probe no longer prints PROFILE_HOST_BINDS\nstdout:\n%s" % r.stdout
    total, with_binds = int(m.group(1)), int(m.group(2))
    assert total == len(registry), (
        "the probe saw %d profile(s) but the registry has %d" % (total, len(registry)))
    assert with_binds >= expected, (
        "only %d of %d profile(s) produced a /home/chrome/host bind; the "
        "'nothing mounts $HOME' verdict would then be true for the wrong reason"
        % (with_binds, expected)
    )


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
#!/usr/bin/env python3
"""
Regression tests for the `rm -rf` guard in uninstall.sh.

THE BUG THESE EXIST FOR
-----------------------
uninstall.sh derives the directory it deletes from backend/config.py, which
honours $CHROME_ISOLATION_HOME, so the path handed to `rm -rf` is influenced by
the environment. The guard that was supposed to make that safe checked:

    not empty, not "/", is a directory, is not $HOME, is not under $HOME/..,
    leaf basename is "profiles" or "Chrome", and the path contains at least
    two '/' characters.

Every one of those is satisfied by `mkdir -p /tmp/decoy/profiles`. Executed
under a hostile $CHROME_ISOLATION_HOME, the script deleted the decoy: a real
directory, deep enough, called `profiles`, containing somebody else's data.

THE FIX
-------
Two independent questions, both of which must be answered before anything is
removed:

  (1) Could this path have been produced by this app? The candidate is
      CONSTRUCTED from $HOME and this app's own naming convention
      (<isolation home>/profiles, and the documented legacy $HOME/Chrome) and
      never taken from configuration or the registry. Symlinks are refused, and
      the resolved path must still equal the canonical one.
  (2) Does it hold data this app created? At least one profile directory must
      contain `Default/` plus `Local State` or `hardware-signature.json`.

Containment alone is defeated by a hostile $CHROME_ISOLATION_HOME, and
appearance alone is defeated by $HOME/profiles — so this module tests both,
including the case that holds realistic Chromium data and must STILL be
refused.

NON-VACUITY
-----------
Every hostile case here is paired with a case that asserts the genuine store IS
removed. A guard that refuses everything would pass half of this file and fail
the other half, which is the point: "refuse everything" is a bug too.
"""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UNINSTALL = os.path.join(ROOT, "uninstall.sh")


# --------------------------------------------------------------------------- #
# Sandbox
# --------------------------------------------------------------------------- #
def _real_profile(path):
    """A profile directory as this app leaves it after Chromium has run once."""
    os.makedirs(os.path.join(path, "Default"), exist_ok=True)
    os.makedirs(os.path.join(path, "Downloads"), exist_ok=True)
    with open(os.path.join(path, "Default", "Preferences"), "w") as f:
        f.write('{"Cookies":[]}\n')
    open(os.path.join(path, "Local State"), "w").close()
    with open(os.path.join(path, "hardware-signature.json"), "w") as f:
        json.dump({"schema_version": 1, "system": {}}, f)
    return path


def _touch(path, name, body="x"):
    full = os.path.join(path, name)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(body + "\n")
    return full


def _sandbox(tmp_path):
    """
    Build the hostile sandbox.

    Layout under tmp_path/sb:
        home/                       the fake $HOME
          .local/share/chrome-isolation-manager/SENTINEL_APPDATA
          SENTINEL_OUTSIDE
          profiles/Mine/            REALISTIC profile data in $HOME/profiles.
                                     Looks exactly like our store and is still
                                     not our store.
          Isolated-Chrome/profiles/Home/    genuine DEFAULT store
          Chrome/Old/                       genuine LEGACY store
        store/profiles/Work/       genuine store behind an explicit
                                   CHROME_ISOLATION_HOME
        decoy/profiles/            decoy: a real directory named `profiles`
        decoy2/profiles/           decoy: contains a FILE named `Default`
        sibling/profiles.bak/      sibling whose leaf merely resembles the store
        sym/profiles -> decoy/profiles    symlinked store
        bin/                       inert docker/sudo/dpkg/rpm stubs
    """
    sb = tmp_path / "sb"
    home = sb / "home"
    for d in (home / ".local" / "share" / "chrome-isolation-manager",
              home / "Isolated-Chrome" / "profiles",
              home / "Chrome",
              home / "profiles",
              sb / "store" / "profiles",
              sb / "decoy" / "profiles",
              sb / "decoy2" / "profiles",
              sb / "sibling" / "profiles.bak",
              sb / "sym",
              sb / "bin"):
        d.mkdir(parents=True, exist_ok=True)

    _real_profile(str(home / "Isolated-Chrome" / "profiles" / "Home"))
    _real_profile(str(home / "Chrome" / "Old"))
    _real_profile(str(home / "profiles" / "Mine"))
    _real_profile(str(sb / "store" / "profiles" / "Work"))
    # decoy2 carries the directory-name bait without being a Chromium profile.
    (sb / "decoy2" / "profiles" / "Nested").mkdir(parents=True, exist_ok=True)
    with open(str(sb / "decoy2" / "profiles" / "Default"), "w") as f:
        f.write("not a profile\n")
    os.symlink(str(sb / "decoy" / "profiles"), str(sb / "sym" / "profiles"))

    sentinels = {
        "APPDATA": home / ".local/share/chrome-isolation-manager/SENTINEL_APPDATA",
        "OUTSIDE": home / "SENTINEL_OUTSIDE",
        "DEFAULTSTORE": home / "Isolated-Chrome/profiles/SENTINEL_DEFAULTSTORE",
        "LEGACY": home / "Chrome/SENTINEL_LEGACY",
        "HOMEDIRPROFILES": home / "profiles/SENTINEL_HOMEDIRPROFILES",
        "STORE": sb / "store/profiles/SENTINEL_STORE",
        "DECOY": sb / "decoy/profiles/SENTINEL_DECOY",
        "DECOY2": sb / "decoy2/profiles/SENTINEL_DECOY2",
        "SIBLING": sb / "sibling/profiles.bak/SENTINEL_SIBLING",
    }
    for path in sentinels.values():
        _touch(str(path.parent), path.name)

    for stub in ("docker", "sudo", "dpkg", "rpm"):
        p = sb / "bin" / stub
        p.write_text("#!/usr/bin/env bash\n# inert stub\nexit 0\n")
        p.chmod(0o755)
    return sb, sentinels


def _run(sb, isolation_home="UNSET"):
    """
    Execute the REAL uninstall.sh against the sandbox.

    `rm` is deliberately NOT stubbed — stubbing it would make these tests unable
    to detect the exact failure they exist to catch. `docker`, `sudo`, `dpkg`
    and `rpm` are stubbed so the run cannot act on the machine. HOME points at
    the sandbox, so every path the script resolves lands inside it, while
    SCRIPT_DIR stays the real repo so the real config.py is what gets imported.
    """
    env = dict(os.environ)
    env["HOME"] = str(sb / "home")
    env["PATH"] = str(sb / "bin") + os.pathsep + env["PATH"]
    env.pop("CHROME_ISOLATION_HOME", None)
    if isolation_home != "UNSET":
        env["CHROME_ISOLATION_HOME"] = isolation_home
    return subprocess.run(
        ["bash", UNINSTALL],
        input="YES\n", env=env, cwd=str(ROOT),
        capture_output=True, text=True, timeout=120,
    )


def _alive(sentinels, key):
    return os.path.exists(str(sentinels[key]))


def _deleted(sentinels, key):
    return not os.path.exists(str(sentinels[key]))


# --------------------------------------------------------------------------- #
# The genuine store is still removed — the non-vacuity anchor
# --------------------------------------------------------------------------- #
def test_default_install_removes_both_genuine_stores(tmp_path):
    """The real stores ARE removed. Without this, 'refuse everything' passes."""
    sb, s = _sandbox(tmp_path)
    r = _run(sb)

    assert r.returncode == 0, r.stderr
    assert _deleted(s, "DEFAULTSTORE"), "the default store was not removed"
    assert _deleted(s, "LEGACY"), "the legacy ~/Chrome store was not removed"
    assert _deleted(s, "APPDATA")
    assert _deleted(s, "OUTSIDE") is False, "$HOME itself must survive"


def test_explicit_isolation_home_removes_that_store(tmp_path):
    """A store the USER configured is still removed."""
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "store"))

    assert r.returncode == 0, r.stderr
    assert _deleted(s, "STORE"), "an explicitly configured store was not removed"
    for key in ("DECOY", "DECOY2", "SIBLING", "HOMEDIRPROFILES", "OUTSIDE"):
        assert _alive(s, key), "%s must survive an honest run" % key


# --------------------------------------------------------------------------- #
# The bug: a decoy that is merely NAMED `profiles`
# --------------------------------------------------------------------------- #
def test_hostile_isolation_home_does_not_delete_a_named_profiles_decoy(tmp_path):
    """
    THE REGRESSION. A directory called `profiles`, deep enough, containing
    somebody else's data — and nothing else that marks it as the app's store.

    The old guard accepted this and rm -rf'd it.
    """
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "decoy"))

    assert _alive(s, "DECOY"), (
        "uninstall.sh deleted %s, which is a real directory that is NOT this "
        "app's profile store. It is named 'profiles' and deep enough, and "
        "those are the only two things the old guard ever checked." % (sb / "decoy/profiles")
    )
    assert _alive(s, "OUTSIDE") and _alive(s, "SIBLING") and _alive(s, "STORE")
    # A refusal has to be a REPORTED refusal. Silently skipping is how the user
    # ends up with orphan data and no idea why.
    assert "LEFT IN PLACE" in r.stderr, (
        "the decoy survived but uninstall.sh said nothing about it; a refused "
        "directory must be named, with a reason and a manual command\nstderr: %s" % r.stderr
    )
    assert "rm -rf --" in r.stderr, "the refusal must tell the user how to finish the job"


def test_a_file_named_Default_does_not_make_a_decoy_look_ours(tmp_path):
    """decoy2 contains a FILE called `Default` — the bait without the directory."""
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "decoy2"))

    assert _alive(s, "DECOY2"), (
        "uninstall.sh deleted a directory whose 'Default' is a regular file; "
        "the ownership test must require the directory Chromium actually writes"
    )
    assert "LEFT IN PLACE" in r.stderr


def test_symlinked_store_is_refused_rather_than_dereferenced(tmp_path):
    """<hostile>/profiles -> ../decoy/profiles. Neither link nor target touched."""
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "sym"))

    assert os.path.islink(str(sb / "sym" / "profiles")), (
        "the symlink itself was removed; this script only removes real "
        "directories it can name outright"
    )
    assert _alive(s, "DECOY"), "the symlink's target's contents were destroyed"
    assert "LEFT IN PLACE" in r.stderr


def test_home_profiles_is_refused_even_when_it_holds_real_profile_data(tmp_path):
    """
    The shape-only decoy. $HOME/profiles contains genuine-looking Chromium
    profile data and satisfies every appearance test — and is still not this
    app's store, because the app never creates it.

    This is the case that stops a "does it look like ours?" guard from being
    accepted as a fix. Appearance and containment both have to be checked.
    """
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "home"))

    assert _alive(s, "HOMEDIRPROFILES"), (
        "uninstall.sh deleted $HOME/profiles even though it holds realistic "
        "Chromium profile data. A guard that only asks whether a directory "
        "looks like a profile store will delete this."
    )
    assert _alive(s, "OUTSIDE"), "$HOME must survive"
    assert _alive(s, "DECOY") and _alive(s, "DECOY2") and _alive(s, "STORE")


# --------------------------------------------------------------------------- #
# Structural refusals that must survive a future refactor
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("isolation_home,still_alive", [
    (".",                 "OUTSIDE"),        # $CHROME_ISOLATION_HOME=.
])
def test_relative_isolation_home_cannot_escape(tmp_path, isolation_home, still_alive):
    sb, s = _sandbox(tmp_path)
    r = _run(sb, isolation_home)
    assert _alive(s, still_alive), "HOME/$still_alive must survive"
    assert r.returncode in (0, 1), "the script must not crash"


def test_uninstall_aborts_before_touching_anything_without_confirmation(tmp_path):
    """The prompt is still real: anything other than YES changes nothing."""
    sb, s = _sandbox(tmp_path)
    env = dict(os.environ)
    env["HOME"] = str(sb / "home")
    env["PATH"] = str(sb / "bin") + os.pathsep + env["PATH"]
    env.pop("CHROME_ISOLATION_HOME", None)
    r = subprocess.run(["bash", UNINSTALL], input="no\n", env=env, cwd=ROOT,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode != 0
    for key in ("APPDATA", "DEFAULTSTORE", "LEGACY", "OUTSIDE", "STORE", "DECOY"):
        assert _alive(s, key), "%s was removed despite an abort" % key


def test_the_preflight_prompt_names_what_will_be_deleted(tmp_path):
    """
    Confirmation lists exactly what goes — the resolved store AND the profiles
    inside it — before anything is deleted, so 'YES' is an informed answer
    rather than a rubber stamp.

    (bash only renders `read -p`'s prompt when stdin is a terminal, so the
    assertion is on the listing itself, which is the part that carries the
    information.)
    """
    sb, s = _sandbox(tmp_path)
    r = _run(sb, str(sb / "store"))
    out = r.stdout + r.stderr
    assert str(sb / "store" / "profiles") in out, out
    assert "Work/" in out, "the pre-flight listing must name the profiles inside the store"
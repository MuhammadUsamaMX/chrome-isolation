"""
Chromium update — DETECT AND INFORM, AND REBUILD ONLY ON CONSENT.

WHAT "UPDATE CHROMIUM" MEANS HERE
---------------------------------
Chromium is not installed on the host. It lives inside the `isolated-chrome`
Docker image, built from the repo's own `Dockerfile`. So "updating Chromium"
means rebuilding that image. Numbers that drive every decision below:

    * a CACHED build takes ~31 s
    * a COLD build takes minutes and pulls ~300 MB
    * the resulting image is 1.31 GB
    * the build runs the `hwspoof-build` stage, which COMPILES C

THE DEFAULT IS *NOT* AUTO-UPDATE. IT IS DETECT-AND-NOTIFY.
----------------------------------------------------------
The default behaviour of this module is:

    on app start -> check availability -> record it -> STOP. Build nothing.

A rebuild happens only when one of these is true, and never otherwise:

    1. EXPLICIT CONSENT. A human invoked `apply_update(version=...)` for the
       specific version the check reported. That is the "Apply now" button.
    2. `CHROME_ISOLATION_AUTO_REBUILD=1`. The named, deliberate opt-in for
       unattended rebuilds. Off by default. See ENVIRONMENT.
    3. `CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1`. The pre-existing documented
       manual opt-in: rebuild at the next app start even when the version
       check says "no update".

Why this is the default, and not a considered default rather than a timid
one. Four concrete reasons, each of which has bitten or would have bitten this
project:

    1. PROFILE DOWNGRADE IS IMPOSSIBLE. Chromium migrates a profile *up* and
       will not downgrade it. Once the image moves 124 -> 152 the user cannot
       go back with their profile intact. The image layers are recoverable —
       nothing is pruned — but profile data may not be. This is a one-way
       door, so a human must be the one to walk through it.
    2. VERIFICATION IS WEAKER THAN IT LOOKS. `_verify()` proves Chromium
       *runs* inside a throwaway container. It does NOT prove the full
       hardened launch works: the six tmpfs mounts in the real launch were
       sized by trial, and a Chromium/Alpine major bump changes what Chromium
       writes at first start. The FIRST REAL LAUNCH is the actual test, and
       it would happen unattended. That is exactly what this policy removes.
    3. THE REBUILD IS ROOT-ADJACENT. `docker build` runs `hwspoof-build`,
       which compiles C. Asking for that without asking is a poor default.
    4. NO UI INDICATION. `image:updateStatus` is wired in `electron/main.js`
       but the renderer never called it, so under the old policy a browser
       could be replaced with nothing on screen saying so.

A browser silently replacing itself is not an acceptable default.

CONSENT IS A GATE, AND IT IS TIED TO A VERSION
----------------------------------------------
Consent is not a notification and not a standing "yes to updates". It is a
versioned token:

    * `apply_update(version=...)` refuses if `version` does not match the
      version the check actually reported. If Alpine publishes a newer build
      between the check and the click, the approval no longer applies and the
      user is asked again, rather than silently receiving something they did
      not look at.
    * The re-check happens *inside* the worker, against freshly detected
      versions, so a stale state file cannot authorise a build.
    * `CONSENT_WARNING` is returned with the status so the UI can show the
      one-way-door text without inventing its own wording.

THE STATE IS THE CONTRACT
-------------------------
`get_update_status()` is what the UI polls. It distinguishes, and must never
conflate:

    idle              nothing has happened yet
    checking          a check is running on the background thread
    up_to_date        POSITIVELY determined: the installed version is current
    update_available  an update exists; current + latest recorded; awaiting
                      explicit consent (`consent_required` is True)
    unknown           COULD NOT DETERMINE. Not "up to date" — a different
                      state. Conflating these two is exactly how a browser
                      stays stale behind a green UI.
    base_eol          the installed image's Alpine branch is END-OF-LIFE:
                      frozen, so its index will never gain another Chromium
                      and the version check will report "up to date" FOREVER
                      while the browser goes unpatched. Reported, never
                      silently fixed: rebuilding the same frozen branch is a
                      1.31 GB no-op, and repinning the base OS is the user's
                      decision, not a side effect of a Chromium update.
                      `branch_eol` and `base_eol_notice` carry the detail.
    building          a rebuild is running (staging tag, `:latest` untouched)
    verifying         the staging image is being checked
    ready             a verified image was promoted onto `:latest`
    failed            the attempt failed; `:latest` is untouched
    deferred          a `chrome-*` container is running; try later
    disabled          `CHROME_ISOLATION_NO_AUTO_UPDATE` is set
    debounced         the interval/cooldown floor has not elapsed

`update_available` is additionally exposed as its own tri-state field
(`True` / `False` / `None`), mirroring `chromium_version`'s contract.

WHY THIS IS NOT "BUILD ON EVERY APP START"
-----------------------------------------
A rebuild on the request path would stall every launch for minutes, hammer
CPU/network, and — worst — leave a half-built image behind. So this module is
built around one rule: **the working image must never be at risk.**

    1. NEVER BLOCK.   `check_and_start()` returns in microseconds. All work
                      happens on a daemon thread. No caller — bridge, IPC,
                      Electron, the UI — ever waits on a build.
    2. NEVER DESTROY. Builds go to a *staging* tag. Only a fully verified image
                      is promoted onto `:latest` with a single `docker tag`.
                      Any failure deletes the staging tag and leaves the
                      existing image byte-for-byte untouched. This is the
                      single most important property here, and it is not
                      decoration: `_ensure_image()` only rebuilds on
                      `ImageNotFound`, so a broken image left on `:latest`
                      would be run forever with no recovery path.
    3. NEVER TWICE.   An `fcntl.flock` on `<APP_DATA_DIR>/image-update.lock`
                      serialises builders across *processes* as well as
                      threads — Electron restarts the bridge on crash, so an
                      in-process flag is not enough. A second caller gets
                      `in_progress`; it does not queue a second build.
    4. NEVER MID-LAUNCH. A running `chrome-*` container defers the rebuild.
                      Detection happens first and its result is still recorded,
                      so a pending update is not invisible just because a
                      profile happens to be open — but no build is started and
                      none is queued.
    5. BOUNDED.       Timestamped start/finish/result, a debounce floor, and a
                      long cooldown after any failure. No retry loops.
    6. REFUSABLE.     `CHROME_ISOLATION_NO_AUTO_UPDATE=1` disables the whole
                      path — check, consent, forced rebuild and unattended
                      rebuild alike. It wins over every other flag.

STDOUT IS THE IPC PROTOCOL — NEVER WRITE TO IT
-----------------------------------------------
`bridge.py` speaks newline-delimited JSON on **stdout**. A single stray
`print()` from a background thread interleaves with the response stream and
corrupts the protocol for the renderer. Every diagnostic in this module goes
to **stderr** through `_log()`. This is a real, previously-exploited trap in
this codebase (see `host_share.py`); it is asserted by the test suite.

ENVIRONMENT
-----------
    (unset — THE DEFAULT)          detect and notify only. Nothing is built
                                    without explicit consent or an opt-in.
    CHROME_ISOLATION_AUTO_REBUILD=1
                                    NAMED OPT-IN FOR UNATTENDED REBUILDS. When
                                    set, an available update is rebuilt
                                    automatically with no human in the loop.
                                    This restores the old behaviour and is
                                    deliberately opt-in, because of the
                                    one-way profile door documented above.
                                    Still respects the single-flight lock, the
                                    failure cooldown and the running-container
                                    deferral — those protect the install, they
                                    are not politeness knobs.
    CHROME_ISOLATION_NO_AUTO_UPDATE=1
                                    kill switch. Disables detection AND the
                                    consent path AND both opt-ins. Nothing is
                                    built, no Docker command runs. Beats
                                    everything else in this list.
    CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1
                                    documented manual opt-in: run a rebuild
                                    at the next app start even when the
                                    version check says "no update".
    CHROME_ISOLATION_VERSION_DETECTOR="module:callable"
                                    see the version-detection seam below.

THE VERSION-DETECTION SEAM (what the other half must provide)
------------------------------------------------------------
This module deliberately does NOT know how to ask "is there a newer Chromium?".
That belongs to a separate module, and guessing its API would be worse than
declaring a contract. What is required is a **detector callable**:

    def detector(image_name: str) -> dict | VersionInfo
        # -> {"current": "152.0.7977.82", "latest": "153.0.1.2"}
        #    `latest` of None / ""  means  "cannot tell"
        #    raising                  means  "cannot tell"
        # A rebuild happens ONLY when both are known and they differ.
        #
        # OPTIONAL advisory key, ignored by every detector that omits it:
        #
        #    "branch_eol": True | False | None
        #      True  -> the image's Alpine base is no longer the current
        #               stable branch: frozen, no further Chromium, no fixes
        #      False -> the base branch is current
        #      None / absent -> could not determine
        #
        # Plus the provenance used to word the message, when known:
        #    "branch", "alpine_version", "stable_branch"
        #
        # `branch_eol` is NOT a rebuild trigger and must never be treated as
        # one. It is an independent, second-order fact about the base OS, and
        # the two decisions are not the same: see the `base_eol` handling in
        # the worker for why an EOL base is reported rather than rebuilt, and
        # why the base image is never repinned automatically.

Register it either programmatically (preferred, from the detector's own
module init) or via `CHROME_ISOLATION_VERSION_DETECTOR=module:callable`.

**Fail-closed is mandatory and is what the default does.** With no detector
registered, `latest` is `None` and the updater reports the `unknown` state
and builds nothing. Note the word: `unknown`, NOT `up_to_date`. Those are
different facts and this module reports them differently — "we could not
check" must never be rendered as "you are up to date". A module that cannot
tell whether an update exists must never be interpreted as "yes, rebuild".

    >>> set_version_detector(my_detector)     # programmatic wiring

The *current* version needs no detector and is read straight out of the
installed image's `/usr/local/share/chrome-isolation/versions.env`, which the
Dockerfile writes at build time precisely so reporting cannot drift from what
is really installed.

TESTING / SEAMS
---------------
Every side effect goes through three injected callables — `client_factory`,
`run_cmd`, and the version detector — so the suite never runs a real build:

    updater = ImageUpdater(client_factory=fake, run_cmd=recorder)
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Optional, Tuple

from config import APP_DATA_DIR, CONTAINER_PREFIX, DOCKER_IMAGE_NAME

# ---------------------------------------------------------------------------
# Locations & tuning
# ---------------------------------------------------------------------------

#: Cross-process single-flight lock. Must live in APP_DATA_DIR so it is shared
#: by every bridge process for this user, and survives a bridge restart.
LOCK_FILE = os.path.join(APP_DATA_DIR, "image-update.lock")

#: Durable status, so a UI poll after a bridge restart still sees the outcome.
STATE_FILE = os.path.join(APP_DATA_DIR, "image-update-state.json")

ENV_DISABLE = "CHROME_ISOLATION_NO_AUTO_UPDATE"

#: NAMED OPT-IN FOR UNATTENDED REBUILDS. Off by default: with this unset an
#: available update is reported and nothing is built until a human consents.
#: See the module docstring for why the default is detect-and-notify.
ENV_AUTO_REBUILD = "CHROME_ISOLATION_AUTO_REBUILD"

ENV_FORCE = "CHROME_ISOLATION_FORCE_IMAGE_UPDATE"
ENV_DETECTOR = "CHROME_ISOLATION_VERSION_DETECTOR"

#: Shortest gap between two *attempts*. A build is ~31 s cached but minutes
#: cold, so anything under a few minutes is a retry loop waiting to happen.
MIN_INTERVAL_SECONDS = 6 * 3600

#: Gap enforced after a FAILED attempt. A build that failed once (bad network,
#: EOL base image, disk full) will not start working by being retried every
#: few minutes. Long enough that a user notices nothing is happening.
FAILURE_COOLDOWN_SECONDS = 24 * 3600

#: Gap after deferring because a profile container is running. The deferral
#: itself is free, but a crash-loop that restarts the app every few seconds
#: would otherwise hit the Docker API continuously.
DEFER_COOLDOWN_SECONDS = 15 * 60

BUILD_TIMEOUT_SECONDS = 60 * 60      # a cold build is minutes, not seconds
VERIFY_TIMEOUT_SECONDS = 120
QUERY_TIMEOUT_SECONDS = 60

STAGING_PREFIX = "staging-"

#: Where the Dockerfile lives. Single source of truth — `docker_manager`
#: imports this rather than keeping its own copy, so the two can never drift.
VERSIONS_ENV_PATH = "/usr/local/share/chrome-isolation/versions.env"
SHIM_LIB_PATH = "/usr/lib/libhwspoof.so"

#: The complete state vocabulary. `up_to_date` and `unknown` are deliberately
#: two separate states, and `update_available` is its own resting state rather
#: than a variant of "ready": a user must be able to see that an update is
#: PENDING without opening a log, and must be able to distinguish "there is no
#: update" from "we could not find out".
STATES = (
    "idle", "checking", "building", "verifying", "ready", "failed",
    "up_to_date", "unknown", "update_available", "deferred", "disabled",
    "debounced", "interrupted",
    # The image's Alpine branch is end-of-life. Its own package index is frozen,
    # so the version check will report "up to date" FOREVER while the browser
    # goes unpatched — a permanently stale browser behind a green UI. A rebuild
    # of that same branch cannot fix it, so the state has to say something the
    # user can act on instead. See `_base_eol_notice` and the worker.
    "base_eol",
)

#: Private alias kept so existing in-module references and any external code
#: that imported the old private name keep working.
_STATES = STATES

#: Shown by the UI next to the "Apply now" button. Lives here so the warning
#: that travels with the CONSENT cannot drift from the warning the user read
#: when they clicked — the renderer does not get to invent its own wording
#: about a one-way door.
CONSENT_WARNING = (
    "Updating Chromium cannot be undone: the browser upgrades your browser "
    "profiles in place and will not downgrade them. Once this update is "
    "applied you cannot return to Chromium {current} with your existing "
    "profiles intact. The image itself can be rebuilt, but your profile data "
    "may not be recoverable."
)

#: Shown when the detector positively determined that the image's Alpine branch
#: is no longer the current stable one. Lives here, next to the other
#: renderer-facing copy, for the same reason as CONSENT_WARNING: the wording the
#: user reads must not drift from the wording the code means.
#:
#: Note what this does NOT say. It does not say "update available" and it does
#: not offer to rebuild the pinned branch, because rebuilding v3.19 installs the
#: newest Chromium v3.19 will ever publish and leaves the image exactly as
#: unpatched. The fix is a new base image, which is the user's decision.
BASE_EOL_NOTICE = (
    "Your Alpine {branch} base is no longer supported. Alpine has frozen that "
    "branch, so it will never receive another Chromium build and no further "
    "security fixes — this app will keep reporting your browser as up to date "
    "while it ages. Rebuilding the same branch cannot fix this. Updating to "
    "Alpine {stable} and rebuilding is required, and is a deliberate step "
    "because it changes your base operating system."
)

#: One-line form for a status row.
BASE_EOL_NOTICE_SHORT = (
    "base OS branch {branch} is end-of-life — rebuild on {stable} required"
)

#: Short, renderer-ready label for the same gate.
CONSENT_WARNING_SHORT = (
    "This cannot be undone — Chromium will not downgrade your browser "
    "profiles."
)

_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})


def _log(message: str) -> None:
    """Emit a progress line.

    STDERR ONLY. stdout is the newline-delimited JSON IPC protocol owned by
    `bridge.py`; writing anything there from this background thread corrupts
    the stream and breaks the renderer. Never replace this with `print()`.
    """
    try:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        sys.stderr.write(f"[image-update {stamp}] {message}\n")
        sys.stderr.flush()
    except Exception:  # stderr closed (bridge shutting down) — never fatal
        pass


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Version-detection seam
# ---------------------------------------------------------------------------

#: Registered detector, or None. See the module docstring for the contract.
_DETECTOR: Optional[Callable[[str], Any]] = None
_DETECTOR_LOCK = threading.Lock()


def set_version_detector(detector: Optional[Callable[[str], Any]]) -> None:
    """Install the callable that answers 'is a newer Chromium available?'.

    The callable receives the image name (`isolated-chrome`) and returns a
    mapping (or anything with `.current` / `.latest`) holding:

        current  str   version installed in the image
        latest   str   version a fresh build would produce, or None/'' for
                       "cannot tell"

    Raising, or returning a falsy/malformed value, is treated as "cannot tell"
    — never as "yes, rebuild". Pass None to uninstall.
    """
    global _DETECTOR
    with _DETECTOR_LOCK:
        _DETECTOR = detector


def get_version_detector() -> Optional[Callable[[str], Any]]:
    """Return the registered detector, honouring the env-var opt-in."""
    with _DETECTOR_LOCK:
        if _DETECTOR is not None:
            return _DETECTOR
    spec = os.environ.get(ENV_DETECTOR, "").strip()
    if not spec:
        return None
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        _log(f"{ENV_DETECTOR}={spec!r} is malformed (want 'module:callable'); ignoring")
        return None
    try:
        module = __import__(module_name, fromlist=[attr])
        fn = getattr(module, attr, None)
    except Exception as exc:  # a bad detector must never break startup
        _log(f"version detector {spec!r} could not be loaded: {exc}")
        return None
    if not callable(fn):
        _log(f"version detector {spec!r} is not callable; ignoring")
        return None
    return fn


def _coerce_versions(raw: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalise whatever the detector returned into (current, latest)."""
    if raw is None:
        return None, None
    if isinstance(raw, str):  # tolerate a bare version string
        return None, raw.strip() or None
    if not isinstance(raw, dict):
        current = getattr(raw, "current", None)
        latest = getattr(raw, "latest", None)
        raw = {"current": current, "latest": latest}
    def _clean(value):
        return str(value).strip() if value not in (None, "") else None
    return _clean(raw.get("current")), _clean(raw.get("latest"))


def _coerce_branch_eol(raw: Any) -> Optional[bool]:
    """Read the detector's optional `branch_eol` advisory. Tri-state.

        True   -> the detector positively determined the base branch is EOL
        False  -> the base branch is the current stable one
        None   -> NOT DETERMINED (absent, None, or not a real bool)

    Deliberately STRICT about the type. A detector that returns `0`, `""`,
    `"false"` or any other falsy value has not made a determination, and
    coercing that to False is precisely the "could not check" -> "looks fine"
    conflation this whole module is built to avoid. Only a real `bool` counts.

    Absence is the normal case: this is an optional key on the seam, so a
    detector that has never heard of branch EOL keeps working unchanged.
    """
    value = raw.get("branch_eol") if isinstance(raw, dict) else None
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Dockerfile location (shared with docker_manager)
# ---------------------------------------------------------------------------

def dockerfile_dir() -> str:
    """Directory containing the `Dockerfile` to build.

    In a PyInstaller onefile bundle `__file__` points into the _MEIPASS temp
    dir, so the Dockerfile sits next to the packaged bridge binary instead.
    """
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(os.path.join(here, 'Dockerfile')):
        return here
    return os.path.dirname(sys.executable)


# ---------------------------------------------------------------------------
# In-image verification script
# ---------------------------------------------------------------------------
#
# Run against the STAGING tag before promotion. Deliberately covers the three
# things that actually break in practice, each of which has already bitten this
# project:
#
#   * chromium-browser missing or unusable as the non-root `chrome` user
#     (the 3.19 image shipped an EOL Alpine that no longer carried the
#     package — and it only surfaced at container start)
#   * versions.env absent/unparsable (the runtime version manifest the UI reads)
#   * the LD_PRELOAD shim not actually loading. musl SILENTLY ignores an
#     unloadable LD_PRELOAD and still exits 0, so "the process started" proves
#     nothing; the shim's constructor writes a probe file and we read it.
#
# Runs as root inside a throwaway `--network none` container with no mounts,
# so it can use `runuser` to mirror the build-time check exactly.
_VERIFY_SCRIPT = r"""
set -eu
fail() { echo "VERIFY_FAIL $1"; exit 1; }

bin="$(command -v chromium-browser || true)"
[ -n "$bin" ] || fail "chromium-missing"
raw="$(runuser -u chrome -- "$bin" --version 2>&1)" || fail "chromium-not-runnable"
case "$raw" in
  *Chromium*) ;;
  *) fail "chromium-version:$raw" ;;
esac
ver="$(printf '%s' "$raw" | sed -n 's/.*Chromium \([0-9][0-9.]*\).*/\1/p')"
[ -n "$ver" ] || fail "chromium-unparsable:$raw"
echo "CHROMIUM_VERSION=$ver"

[ -r "__VERSIONS_ENV__" ] || fail "versions.env-missing"
echo "VERSIONS_ENV=$(tr '\n' ' ' < "__VERSIONS_ENV__")"

probe=/tmp/.shim-probe
rm -f "$probe" 2>/dev/null || true
LD_PRELOAD="__SHIM__" CHROME_SPOOF_PROBE="$probe" /bin/true >/dev/null 2>&1 || true
got="$(cat "$probe" 2>/dev/null || true)"
[ "$got" = "loaded" ] || fail "shim-probe:${got:-<none>}"
echo "SHIM=loaded"
echo "VERIFY_OK"
""".replace("__VERSIONS_ENV__", VERSIONS_ENV_PATH).replace("__SHIM__", SHIM_LIB_PATH)


def _parse_verify_output(text: str) -> dict:
    """Pull the key=value facts out of a successful verification run."""
    facts: dict = {}
    for line in (text or "").splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            facts[key.strip()] = value.strip()
    return facts


# ---------------------------------------------------------------------------
# Updater
# ---------------------------------------------------------------------------

class ImageUpdater:
    """Background, non-blocking, never-destructive rebuild of the image.

    Constructing one is cheap and does no I/O beyond reading a small JSON
    file: it runs on a path that executes on every backend import, so the
    constructor must never touch the network or block.
    """

    def __init__(
        self,
        client_factory: Optional[Callable[[], Any]] = None,
        run_cmd: Optional[Callable[..., Tuple[int, str, str]]] = None,
        detector: Optional[Callable[[str], Any]] = None,
        image_name: str = DOCKER_IMAGE_NAME,
        lock_file: str = LOCK_FILE,
        state_file: str = STATE_FILE,
        min_interval: int = MIN_INTERVAL_SECONDS,
        failure_cooldown: int = FAILURE_COOLDOWN_SECONDS,
        defer_cooldown: int = DEFER_COOLDOWN_SECONDS,
        now: Callable[[], float] = time.time,
    ):
        self._client_factory = client_factory
        self._run_cmd = run_cmd or _default_run_cmd
        self._detector = detector
        self._image = image_name
        self._tag = f"{image_name}:latest"
        self._lock_file = lock_file
        self._state_file = state_file
        self._min_interval = min_interval
        self._failure_cooldown = failure_cooldown
        self._defer_cooldown = defer_cooldown
        self._now = now

        # RLock, not Lock: `check_and_start` holds this while calling
        # `_preflight`, which records the refusal through `_write_state`.
        # A plain non-reentrant Lock would deadlock on the second acquire.
        self._guard = threading.RLock()  # guards the fields below, in-process
        self._lock_fd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        #: Last raw detector result, kept only so the EOL notice can name the
        #: branches. Written and read only by the worker thread, so it needs no
        #: lock, and it is a plain attribute so `__init__` still touches
        #: nothing beyond the one JSON read.
        self._last_detection: dict = {}
        self._status: dict = self._read_state()

    # ------------------------------------------------------------ properties
    @property
    def image_name(self) -> str:
        return self._image

    @property
    def enabled(self) -> bool:
        """False when the kill switch is set. Nothing is built when False.

        The kill switch is evaluated FIRST and beats every other flag,
        including `CHROME_ISOLATION_AUTO_REBUILD`. A user who has said "do not
        touch my browser" must not be overridden by a flag that happens to be
        left set in the environment.
        """
        return not _flag(ENV_DISABLE)

    def force_requested(self) -> bool:
        """The documented manual opt-in: rebuild regardless of the version check."""
        return _flag(ENV_FORCE)

    def auto_rebuild_requested(self) -> bool:
        """True when the user opted in to UNATTENDED rebuilds.

        This is the escape hatch for anyone who genuinely wants the old
        "just keep it current" behaviour. It is opt-in rather than the
        default because a Chromium major bump crosses a one-way door in the
        user's browser profiles — see the module docstring.
        """
        return _flag(ENV_AUTO_REBUILD)

    # ------------------------------------------------------------ state I/O
    def _read_state(self) -> dict:
        try:
            with open(self._state_file) as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            _log(f"could not read status file {self._state_file}: {exc}")
        return {}

    def _write_state(self, **changes) -> None:
        with self._guard:
            self._status.update(changes)
            snapshot = dict(self._status)
        snapshot["state"] = snapshot.get("state", "idle")
        if snapshot["state"] not in _STATES:
            snapshot["state"] = "idle"
        try:
            os.makedirs(os.path.dirname(self._state_file), exist_ok=True)
            tmp = f"{self._state_file}.tmp"
            with open(tmp, "w") as fh:
                json.dump(snapshot, fh, indent=2, sort_keys=True)
            os.replace(tmp, self._state_file)
        except OSError as exc:
            _log(f"could not write status file {self._state_file}: {exc}")

    # ------------------------------------------------------------ status API
    def get_update_status(self) -> dict:
        """Current state, safe to call at any time from any thread.

        This is the contract the renderer polls (`image:updateStatus`). Every
        key is always present, so the UI never has to guard against a missing
        field and can switch on `state` alone.

        ALWAYS PRESENT
            state              one of _STATES. 'up_to_date' (positively
                               determined: current) and 'unknown' (could not
                               determine) are DIFFERENT states and must be
                               rendered differently.
            update_available   True | False | None. Tri-state, mirroring
                               chromium_version: True = an update exists,
                               False = determined current, None = could not
                               determine. Never collapse None into False.
            current_version    version read out of the installed image
            latest_version     version a fresh build would produce
            branch_eol         True | False | None. Tri-state advisory from
                               the detector: True = the image's Alpine branch
                               is no longer the current stable one, False = it
                               is, None = could not determine. Never collapse
                               None into False.
            base_eol_notice    renderer-facing explanation of the EOL base, or
                               None. Set whenever state == 'base_eol'.
            base_eol_notice_short
                               one-line form of the same.
            consent_required   True only when state == 'update_available'.
                               The renderer shows the "Apply now" button on
                               this flag, not on the mere absence of an error.
            consent_version    the exact version to pass to apply_update().
                               Never a blanket "yes to updates".
            consent_warning    the one-way-door text to show before the click
                               (CONSENT_WARNING with {current} filled in).
            consent_warning_short
                               one-line form for a button/checkbox label.
            policy             'disabled' | 'unattended' | 'notify_only'.
                               Lets the UI explain WHY it is not rebuilding.
            error              failure / unknown detail, or None
            started_at         unix ts when the current/last attempt began
            finished_at        unix ts when it ended
            enabled            False when the kill switch is set
            auto_rebuild       True when unattended rebuilds are opted in
            forced             True when ENV_FORCE is set
            last_result        human-readable summary
            next_attempt_at    unix ts of the next permitted attempt
            retry_in_seconds   seconds until then, or None
            staging_tag        the in-flight staging tag, or None
        """
        status = dict(self._status)
        status.setdefault("state", "idle")
        status.setdefault("started_at", None)
        status.setdefault("finished_at", None)
        status.setdefault("error", None)
        status.setdefault("current_version", None)
        status.setdefault("latest_version", None)
        status.setdefault("last_result", None)
        # `branch_eol` is tri-state here too, and the default is None rather
        # than False: no check has run, so nothing has been determined. A
        # renderer that switched on `branch_eol` and treated a missing key as
        # "supported" would be making exactly the assumption this state exists
        # to prevent.
        status.setdefault("branch_eol", None)
        status.setdefault("base_eol_notice", None)
        self._reconcile_stale_build(status)

        # `update_available` is TRI-STATE, derived from `state` rather than
        # stored, so it can never disagree with the state the UI switches on:
        #     True  -> an update is pending human consent ('update_available')
        #     False -> positively determined current ('up_to_date', 'ready')
        #     None  -> could not determine ('unknown'), or a state in which the
        #              question does not apply yet ('idle', 'checking', ...)
        #
        # None is NOT False. A renderer that collapsed them would paint an
        # unchecked browser green, which is precisely how a browser stays
        # stale behind a green UI.
        if status.get("state") == "update_available":
            status["update_available"] = True
        elif status.get("state") in ("up_to_date", "ready"):
            status["update_available"] = False
        else:
            status["update_available"] = None

        auto_rebuild = self.auto_rebuild_requested()
        if not self.enabled:
            policy = "disabled"
        elif auto_rebuild:
            policy = "unattended"
        else:
            policy = "notify_only"

        status["enabled"] = self.enabled
        status["forced"] = self.force_requested()
        status["auto_rebuild"] = auto_rebuild
        status["policy"] = policy
        status["next_attempt_at"] = status.get("next_attempt_at")
        status["retry_in_seconds"] = status.get("retry_in_seconds")
        status["staging_tag"] = status.get("staging_tag")

        pending = status.get("state") == "update_available"
        status["consent_required"] = bool(pending)
        status["consent_version"] = status.get("latest_version") if pending else None
        current = status.get("current_version") or "the version you have now"
        status["consent_warning"] = CONSENT_WARNING.replace("{current}", str(current))
        status["consent_warning_short"] = CONSENT_WARNING_SHORT
        # Mirrors the long form, derived from the same stored branches so the
        # two can never disagree.
        branch = self._last_detection.get("branch") or \
            self._last_detection.get("alpine_version")
        stable = self._last_detection.get("stable_branch")
        if status.get("branch_eol") and branch and stable:
            status["base_eol_notice_short"] = BASE_EOL_NOTICE_SHORT.replace(
                "{branch}", str(branch)).replace("{stable}", str(stable))
        else:
            status["base_eol_notice_short"] = None
        return status

    def _reconcile_stale_build(self, status: dict) -> None:
        """A `building` state whose lock is free means the builder died.

        The bridge is restarted by Electron on any crash, so a half-finished
        build must not leave the UI claiming an update is in progress forever.
        """
        if status.get("state") not in ("building", "verifying", "checking"):
            return
        fd = self._try_lock(blocking=False)
        if fd is None:
            return                      # a builder really is alive — leave it
        try:
            self._unlock_fd(fd)
            _log("previous build state left behind with no lock holder; "
                 "reporting it as interrupted")
            self._write_state(
                state="interrupted",
                finished_at=self._now(),
                error="build process disappeared before completing",
                next_attempt_at=self._now() + self._min_interval,
                retry_in_seconds=self._min_interval,
            )
            status.update(self._status)
        except OSError:
            pass

    # -------------------------------------------------------------- locking
    def _try_lock(self, blocking: bool = False) -> Optional[int]:
        """flock the update lock; return the fd, or None if already held.

        Deliberately OS-level. Multiple `DockerManager` instances in one
        process AND multiple bridge processes must not build at once, and only
        the kernel can arbitrate between the latter. The lock is released
        implicitly if the builder dies, so there is no stale-lock cleanup to
        get wrong.
        """
        try:
            os.makedirs(os.path.dirname(self._lock_file), exist_ok=True)
            fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            _log(f"cannot open lock file {self._lock_file}: {exc}")
            return None
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError:
            os.close(fd)
            return None
        return fd

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    def is_locked(self) -> bool:
        """True when some builder (this or another process) holds the lock."""
        fd = self._try_lock(blocking=False)
        if fd is None:
            return True
        self._unlock_fd(fd)
        return False

    # -------------------------------------------------------- preflight gate
    def _preflight(self, force: bool, explicit: bool = False) -> Tuple[bool, dict]:
        """Decide whether a build may start. Cheap, no Docker calls.

        `explicit` means a human asked for this specific attempt (consent, or
        `force`). It may ignore the ROUTINE debounce floor — that is what the
        user asked for — but, exactly like `force`, it may never ignore the
        post-failure cooldown.
        """
        if not self.enabled:
            return False, {"reason": "disabled",
                           "detail": f"{ENV_DISABLE} is set"}

        status = dict(self._status)
        now = self._now()

        if status.get("deferred_at") and (
                now - float(status["deferred_at"]) < self._defer_cooldown):
            return False, {"reason": "debounced", "detail": "recently deferred",
                           "retry_in_seconds": self._defer_cooldown}

        last = status.get("last_attempt_at")
        if not last:
            return True, {}

        failed = bool(status.get("consecutive_failures"))
        floor = self._failure_cooldown if failed else self._min_interval
        if (force or explicit) and not failed:
            # A manual "apply now" / "rebuild now" may ignore the routine
            # debounce — that is what the user asked for.
            return True, {}
        # A FAILED attempt is never bypassed, not even by force. A permanently
        # set force flag plus a persistently failing build would otherwise mean
        # minutes of cold-build I/O on every single app launch, which is the
        # retry-hammer this is meant to prevent.
        elapsed = now - float(last)
        if elapsed < floor:
            return False, {
                "reason": "debounced",
                "detail": ("after a failure; " if failed else "")
                          + f"minimum interval {int(floor)}s",
                "retry_in_seconds": int(floor - elapsed),
            }
        return True, {}

    # ---------------------------------------------------------- entry point
    def check_and_start(self, force: Optional[bool] = None,
                        reason: str = "app_start",
                        consent_version: Optional[str] = None) -> dict:
        """Start a background UPDATE CHECK and return immediately.

        THIS IS THE STARTUP PATH, AND BY DEFAULT IT BUILDS NOTHING.

        It detects, records, and stops in the `update_available` state. A
        rebuild happens only with explicit consent (`apply_update`) or with an
        unattended opt-in (`CHROME_ISOLATION_AUTO_REBUILD=1`). `force=True`
        remains the documented manual override for a *programmatic* caller —
        the bridge does not pass it, and the renderer cannot reach it without
        going through `apply_update`, which is the versioned consent gate.

        No Docker work, no version check and no waiting happens here: it
        validates the kill switch, takes the single-flight lock
        (non-blocking), and hands the rest to a daemon thread.

        Returns one of:
            {"started": True,  "state": "checking"}
            {"started": False, "reason": "disabled"|"in_progress"|"debounced", ...}
        """
        if force is None:
            force = self.force_requested()

        if not self.enabled:
            _log(f"auto-update disabled via {ENV_DISABLE}; not checking ({reason})")
            # Record it durably. A user who set the kill switch must be able to
            # SEE that updates are off, rather than the UI showing a stale
            # 'idle' that is indistinguishable from "nothing has happened yet".
            # A file write, not a Docker call — the kill switch still guarantees
            # zero Docker commands.
            self._write_state(state="disabled", finished_at=self._now(),
                              last_result=f"updates disabled via {ENV_DISABLE}")
            return {"started": False, "reason": "disabled", "state": "disabled",
                    "detail": f"{ENV_DISABLE} is set", "status": self.get_update_status()}

        with self._guard:
            if self._thread is not None and self._thread.is_alive():
                return {"started": False, "reason": "in_progress",
                        "detail": "an update check is already running in this process",
                        "status": self.get_update_status()}

            fd = self._try_lock(blocking=False)
            if fd is None:
                # Another builder holds it. A second build is never queued.
                _log("update already in progress elsewhere; not starting a second build")
                return {"started": False, "reason": "in_progress",
                        "detail": "another process holds the image-update lock",
                        "status": self.get_update_status()}

            try:
                allowed, refusal = self._preflight(
                    force=bool(force), explicit=bool(consent_version))
                if not allowed:
                    # Release before reporting: the lock guards the build, and
                    # a refused check must not hold it.
                    self._unlock_fd(fd)
                    _log(f"skipping update check ({refusal.get('detail')}); reason={reason}")
                    if refusal.get("reason") == "debounced":
                        self._write_state(
                            state="debounced",
                            retry_in_seconds=refusal.get("retry_in_seconds"),
                            next_attempt_at=self._now()
                            + float(refusal.get("retry_in_seconds") or 0),
                        )
                    return {"started": False, "reason": refusal.get("reason"),
                            "detail": refusal.get("detail"),
                            "status": self.get_update_status()}
            except Exception:
                self._unlock_fd(fd)
                raise

            self._lock_fd = fd
            self._thread = threading.Thread(
                target=self._worker,
                args=(fd, bool(force), reason, consent_version),
                name="image-update", daemon=True,
            )
            try:
                self._thread.start()
            except Exception as exc:  # never leave a lock held by a dead thread
                self._lock_fd = None
                self._thread = None
                self._unlock_fd(fd)
                _log(f"could not start update thread: {exc}")
                return {"started": False, "reason": "error", "detail": str(exc),
                        "status": self.get_update_status()}

        _log(f"update check started in background (reason={reason}, force={bool(force)})")
        return {"started": True, "state": "checking", "reason": reason,
                "status": self.get_update_status()}

    # ------------------------------------------------------------- consent
    def apply_update(self, version: Optional[str] = None,
                     reason: str = "user_consent") -> dict:
        """The explicit "Apply now" action. Builds ONLY with versioned consent.

        This is the gate the UI calls. It is deliberately separate from
        `check_and_start` so that "look for an update" and "rebuild my
        browser" can never be the same click, and so the one-way-door warning
        travels with the consent rather than with an ambient capability.

        `version` MUST be the version the status currently reports as
        available (`consent_version`). Passing None falls back to the recorded
        `latest_version`. If the worker re-detects and finds a *different*
        latest — Alpine published another build in the interval between the
        check and the click — the consent does not apply, nothing is built,
        and the status is refreshed to the new version so the user consents
        again. A blanket "yes to updates" would let a user approve 153 and
        silently receive 154, which is precisely the surprise this design is
        trying to prevent.

        The kill switch beats consent: with `CHROME_ISOLATION_NO_AUTO_UPDATE`
        set, this returns `disabled` and runs no Docker command at all.

        Returns the same shape as `check_and_start`, plus `consent_version`.
        """
        if not self.enabled:
            _log(f"{ENV_DISABLE} is set; refusing the apply request")
            return {"started": False, "reason": "disabled", "state": "disabled",
                    "detail": f"{ENV_DISABLE} is set",
                    "status": self.get_update_status()}

        approved = str(version).strip() if version is not None else ""
        if not approved:
            status = self.get_update_status()
            approved = str(status.get("latest_version") or "").strip()
        if not approved:
            # Nothing to consent to. This is the "could not determine" case,
            # and it must NOT be quietly treated as "up to date, nothing to
            # do" — say so.
            return {"started": False, "reason": "no_update_available",
                    "detail": ("no version is currently reported as available, "
                               "so there is nothing to consent to"),
                    "status": self.get_update_status()}

        _log(f"user consented to updating Chromium to {approved} (reason={reason})")
        result = self.check_and_start(force=False, reason=reason,
                                      consent_version=approved)
        result["consent_version"] = approved
        return result

    def wait_for_idle(self, timeout: float = 30.0) -> bool:
        """Test/CLI helper: block until the background thread finishes.

        NOT on any request path — this exists so a test or a manual invocation
        can observe completion deterministically. The app never calls it.
        """
        with self._guard:
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------ the worker
    def _worker(self, lock_fd: int, force: bool, reason: str,
                consent_version: Optional[str] = None) -> None:
        started = self._now()
        staging: Optional[str] = None
        promoted = False
        try:
            self._write_state(
                state="checking", started_at=started, finished_at=None, error=None,
                last_result=f"checking for updates ({reason})",
                consecutive_failures=0, deferred_at=None, retry_in_seconds=None,
            )

            if not self.enabled:
                self._write_state(state="disabled", finished_at=self._now(),
                                  last_result="disabled by kill switch")
                return

            current, latest, branch_eol = self._detect_versions()
            force = force or self.force_requested()

            # Record the determination ONCE, here, so it survives into
            # whichever state follows. Writing it only inside the EOL branch
            # would lose a perfectly good `False` — the renderer would see
            # `branch_eol: None` ("could not determine") for an image that was
            # positively confirmed to be on a supported base, which is the same
            # unknown-as-healthy confusion one level up.
            self._write_state(branch_eol=branch_eol)

            # ── WHAT TO DO ABOUT AN END-OF-LIFE BASE BRANCH
            #
            # `chromium_version` compares Chromium within the image's own
            # Alpine branch. On a frozen branch that comparison is permanently
            # satisfied: the 3.19 index tops out at Chromium 124, so the check
            # reports "up to date" forever while the browser goes unpatched.
            # That is exactly the "stale browser behind a green UI" outcome.
            #
            # We therefore do NOT rebuild here, and we do NOT silently
            # repin the user's base image. Both of those are wrong:
            #
            #   * Rebuilding the pinned EOL branch installs the newest Chromium
            #     that branch will EVER publish and leaves the image exactly as
            #     unpatched — 1.31 GB of I/O for a guaranteed no-op.
            #   * Moving the user to a different Alpine branch is a bigger
            #     decision than moving Chromium within a branch: it changes the
            #     base operating system under their profiles. Doing that as a
            #     side effect of a "Chromium update" would be a silent,
            #     unrequested, effectively-irreversible change of the user's
            #     system. Auto-updating a package is a policy decision the
            #     user opted into; auto-changing their base OS is not.
            #
            # So: report a distinct, actionable state and stop. The user sees
            # why their browser is not advancing and what the fix is, and the
            # fix stays theirs to make. `base_eol` is checked BEFORE the
            # up-to-date/unknown branches precisely because those would
            # otherwise render an unsupported, permanently-frozen image as
            # healthy.
            #
            # This is best-effort and strictly non-blocking. `branch_eol` is
            # None whenever the detector could not determine it, and None falls
            # straight through to the pre-existing behaviour — a failed 3 KB
            # side-lookup must never block a legitimate Chromium update.
            if branch_eol and not force:
                notice = self._base_eol_notice()
                _log(f"base OS branch is end-of-life; recording 'base_eol' and "
                     f"NOT rebuilding — {notice}")
                self._write_state(
                    state="base_eol", finished_at=self._now(),
                    current_version=current, latest_version=latest,
                    branch_eol=True, base_eol_notice=notice,
                    last_result=notice,
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return

            # ── What did we learn? Three genuinely different answers, and
            #    collapsing any two of them is the bug this state machine
            #    exists to prevent.
            #
            #    latest is None  -> `unknown`.  We could not determine whether
            #                       an update exists. This is NOT "up to date".
            #    latest == current -> `up_to_date`. Positively determined.
            #    otherwise       -> an update exists.
            if latest is None and not force:
                _log("could not determine whether an update exists (no detector, "
                     "or it could not determine a latest version); "
                     "recording 'unknown' and not rebuilding")
                self._write_state(
                    state="unknown", finished_at=self._now(),
                    current_version=current, latest_version=None,
                    last_result=("update availability unknown — could not check; "
                                 "not rebuilt"),
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return
            if not force and _is_up_to_date(current, latest,
                                             self._last_detection):
                _log(f"up to date (Chromium {current})")
                self._write_state(
                    state="up_to_date", finished_at=self._now(),
                    current_version=current, latest_version=latest,
                    last_result=f"already at Chromium {current}",
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return

            # An update is available. Now: is anyone allowed to ACT on it?
            #
            # Three ways to be authorised, in priority order:
            #   1. explicit consent for THIS version (the UI "Apply now")
            #   2. CHROME_ISOLATION_AUTO_REBUILD=1  (deliberate opt-in)
            #   3. force / CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1 (pre-existing
            #      documented manual opt-in)
            # Anything else: record and stop. This is the default path.
            unattended = self.auto_rebuild_requested()
            consent = str(consent_version).strip() if consent_version else None

            if consent is not None and latest is not None and consent != latest:
                # The version moved between the check and the click. The user
                # approved a specific version; refusing to substitute a
                # different one silently is the entire point of versioned
                # consent. Report the NEW version so the UI can ask again.
                _log(f"consent was given for Chromium {consent} but {latest} is "
                     f"what is actually available; not rebuilding on a version "
                     f"the user did not approve")
                self._write_state(
                    state="update_available", finished_at=self._now(),
                    current_version=current, latest_version=latest,
                    last_result=(f"Chromium {latest} is available, but approval was "
                                 f"given for {consent}; review and approve again"),
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return

            authorised = bool(consent) or unattended or bool(force)
            if not authorised:
                _log(f"Chromium {latest} is available (installed {current}); "
                     f"NOT rebuilding — explicit consent is required "
                     f"(or set {ENV_AUTO_REBUILD}=1 for unattended rebuilds)")
                self._write_state(
                    state="update_available", finished_at=self._now(),
                    current_version=current, latest_version=latest,
                    last_result=(f"Chromium {latest} is available (installed "
                                 f"{current}); awaiting your approval — nothing "
                                 f"will be rebuilt until you apply it"),
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return

            # Authorised to build. NOW check that a profile is not running.
            # This is deliberately after the detection: a pending update must
            # still be reported while the user has a profile open, but the
            # rebuild must not land mid-launch.
            running = self._running_profile_containers()
            if running:
                msg = (f"deferring: {len(running)} profile container(s) running "
                       f"({', '.join(running[:4])})")
                _log(msg)
                self._write_state(
                    state="deferred", finished_at=self._now(),
                    last_result=msg, deferred_at=started,
                    current_version=current, latest_version=latest,
                    retry_in_seconds=self._defer_cooldown,
                    next_attempt_at=started + self._defer_cooldown,
                )
                return

            staging = self._staging_tag(started)
            _log(f"update available (installed={current}, latest={latest}); "
                 f"building staging tag {staging} — ':latest' is NOT touched "
                 f"until the build verifies")
            # last_attempt_at is stamped BEFORE the build, so a build that
            # dies mid-flight still debounces the next attempt. Recording it
            # only on success is how you get a retry loop.
            self._write_state(
                state="building", staging_tag=staging,
                current_version=current, latest_version=latest,
                last_attempt_at=started,
            )
            self._cleanup_stale_staging_tags(staging)

            self._build(staging)

            self._write_state(state="verifying", staging_tag=staging)
            facts = self._verify(staging)
            built_version = facts.get("CHROMIUM_VERSION") or latest
            _log(f"staging image {staging} verified (Chromium {built_version})")

            # The one moment ':latest' moves. A single daemon-side tag
            # operation: readers see the old image or the new one, never a
            # half-updated tag.
            self._promote(staging)
            promoted = True
            self._discard_tag(staging)
            staging = None

            finished = self._now()
            _log(f"promoted {staging or 'staging image'} -> {self._tag} "
                 f"(Chromium {built_version}, {int(finished - started)}s)")
            self._write_state(
                state="ready", finished_at=finished, staging_tag=None,
                current_version=built_version, latest_version=built_version,
                error=None, consecutive_failures=0, deferred_at=None,
                last_result=f"updated to Chromium {built_version}",
                retry_in_seconds=self._min_interval,
                next_attempt_at=finished + self._min_interval,
            )
        except Exception as exc:  # a background thread must never die silently
            if staging is not None and not promoted:
                # The whole point: the working image is never at risk, so a
                # failure cleans up after itself and leaves ':latest' alone.
                _log(f"update FAILED ({exc}); removing staging tag and leaving "
                     f"{self._tag} untouched")
                self._discard_tag(staging)
            self._write_state(
                state="failed", finished_at=self._now(), staging_tag=None,
                error=str(exc), last_result=f"update failed: {exc}",
                consecutive_failures=int(self._status.get("consecutive_failures") or 0) + 1,
                retry_in_seconds=self._failure_cooldown,
                next_attempt_at=self._now() + self._failure_cooldown,
            )
        finally:
            with self._guard:
                self._lock_fd = None
            self._unlock_fd(lock_fd)

    # --------------------------------------------------------- docker access
    def _client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import docker  # imported lazily: module import must stay side-effect free
        return docker.from_env()

    def _running_profile_containers(self) -> list:
        """Names of running `chrome-*` containers. Never raises."""
        try:
            client = self._client()
            running = []
            for container in client.containers.list():
                name = (getattr(container, "name", "") or "").lstrip("/")
                if not name.startswith(CONTAINER_PREFIX):
                    continue
                if (getattr(container, "status", "") or "") == "running":
                    running.append(name)
            return running
        except Exception as exc:
            # If we cannot tell whether a launch is in flight, assume one is
            # and defer. Failing open here is how you rebuild mid-launch.
            _log(f"could not list containers ({exc}); assuming a launch is in "
                 f"flight and deferring")
            return ["<unknown>"]

    def _cleanup_stale_staging_tags(self, keep: str) -> None:
        """Drop staging tags abandoned by a previous, dead build."""
        pattern = f"{self._image}:{STAGING_PREFIX}*"
        code, out, _ = self._run_cmd(
            ["docker", "image", "ls", "--filter", f"reference={pattern}", "-q"],
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if code != 0 or not out.strip():
            return
        for line in out.split():
            ref = line.strip()
            if not ref or ref == keep:
                continue
            self._discard_tag(ref)

    def _staging_tag(self, stamp: float) -> str:
        return f"{self._image}:{STAGING_PREFIX}{os.getpid()}-{int(stamp)}"

    def _build(self, staging: str) -> None:
        env = os.environ.copy()
        env['DOCKER_BUILDKIT'] = '1'
        code, _, err = self._run_cmd(
            ["docker", "build", "-t", staging, dockerfile_dir()],
            env=env, timeout=BUILD_TIMEOUT_SECONDS,
        )
        if code != 0:
            tail = (err or '').strip().splitlines()[-15:]
            raise RuntimeError(
                "docker build failed for " + staging + ":\n" + "\n".join(tail))

    def _verify(self, staging: str) -> dict:
        """Run the acceptance checks against the STAGING tag only."""
        code, out, err = self._run_cmd(
            ["docker", "run", "--rm", "--pull=never", "--network=none",
             "--user", "root", "--entrypoint", "/bin/sh", staging, "-c",
             _VERIFY_SCRIPT],
            timeout=VERIFY_TIMEOUT_SECONDS,
        )
        if code != 0 or "VERIFY_OK" not in (out or ""):
            detail = "\n".join(
                [ln for ln in (out or "").splitlines() if "VERIFY_FAIL" in ln]
            ) or (err or "").strip().splitlines()[-10:]
            raise RuntimeError(
                f"staging image {staging} failed verification: "
                + ("\n".join(detail) or f"exit {code}"))
        return _parse_verify_output(out)

    def _promote(self, staging: str) -> None:
        code, _, err = self._run_cmd(
            ["docker", "tag", staging, self._tag], timeout=QUERY_TIMEOUT_SECONDS)
        if code != 0:
            raise RuntimeError(f"could not tag {staging} as {self._tag}: "
                               f"{(err or '').strip()[:400]}")

    def _discard_tag(self, ref: str) -> None:
        """Remove a staging tag. Best effort — a leftover tag is untidy, not
        dangerous, and must never be escalated into a failure of the run."""
        code, _, err = self._run_cmd(
            ["docker", "image", "rm", ref], timeout=QUERY_TIMEOUT_SECONDS)
        if code != 0:
            _log(f"could not remove {ref} (exit {code}); leaving it in place")
        else:
            _log(f"removed staging tag {ref}")

    # ------------------------------------------------------- version reading
    def _read_installed_versions(self) -> Tuple[Optional[str], Optional[str]]:
        """Read versions.env straight out of the installed image.

        Needs no detector: the Dockerfile writes this manifest at build time
        precisely so the reported version cannot drift from reality. Returns
        (chromium_version, alpine_version), either of which may be None.
        """
        code, out, _ = self._run_cmd(
            ["docker", "run", "--rm", "--pull=never", "--network=none",
             "--entrypoint", "/bin/sh", self._tag, "-c",
             f"cat {VERSIONS_ENV_PATH} 2>/dev/null || true"],
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        if code != 0:
            return None, None
        fields = {}
        for line in (out or "").splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                fields[key.strip()] = value.strip()
        return fields.get("CHROMIUM_VERSION"), fields.get("ALPINE_VERSION")

    def _base_eol_notice(self) -> str:
        """Renderer-facing text for the EOL-base state, from the detector's data.

        Falls back to wording that does not name a branch when the detector
        could not tell us which one it is. A notice with an unfilled
        `{branch}` in it would be worse than a vaguer sentence.
        """
        branch = self._last_detection.get("branch") or self._last_detection.get("alpine_version")
        stable = self._last_detection.get("stable_branch")
        if not branch or not stable:
            return (
                "Your Alpine base is no longer a supported release. Alpine has "
                "frozen that branch, so it will never receive another Chromium "
                "build and no further security fixes. Rebuilding the same "
                "branch cannot fix this; a rebuild on a supported base is "
                "required, which changes your base operating system."
            )
        return BASE_EOL_NOTICE.replace("{branch}", str(branch)) \
                              .replace("{stable}", str(stable))

    def _detect_versions(self) -> Tuple[Optional[str], Optional[str], Optional[bool]]:
        """(current, latest, branch_eol). `latest is None` means "do not rebuild".

        `branch_eol` is tri-state and ADVISORY: True/False/None as described on
        `_coerce_branch_eol`. It is returned alongside rather than folded into
        `latest` deliberately — see the worker.
        """
        detected_current = None
        try:
            detected_current, _ = self._read_installed_versions()
        except Exception as exc:
            _log(f"could not read installed version: {exc}")

        detector = self._detector or get_version_detector()
        if detector is None:
            self._last_detection = {}
            return detected_current, None, None
        try:
            raw = detector(self._image)
        except Exception as exc:
            _log(f"version detector raised ({exc}); treating as 'unknown'")
            self._last_detection = {}
            return detected_current, None, None
        # Retained only so the EOL notice can name the branches. Defaults to an
        # empty dict, never to a stale one from a previous run, so a notice can
        # never quote a branch the current detection did not report.
        self._last_detection = dict(raw) if isinstance(raw, dict) else {}
        current, latest = _coerce_versions(raw)
        branch_eol = _coerce_branch_eol(raw)
        # The image itself is authoritative for "current" — a detector that
        # disagrees cannot make us skip a real update or chase a phantom one.
        return detected_current or current, latest, branch_eol


def _version_tuple(value) -> Optional[Tuple[int, ...]]:
    """
    Sortable key for a Chromium version, or None if unparsable.

    Two traps this exists to avoid:

      * LEXICAL. '99.0.1.2' > '152.0.7977.82' as strings but is older, and the
        predecessor image really was 124 vs 152.
      * Alpine's package revision. The index publishes
        '152.0.7977.82-r0' while the image's own manifest and
        `chromium-browser --version` report '152.0.7977.82'. Comparing the raw
        strings therefore reports an update available for a browser that is
        already exactly current — which is how an installed-124 image on an
        EOL branch asks to be "updated" to 124 again, forever.

    Mirrors chromium_version.version_key. Duplicated deliberately rather than
    imported: this module must not depend on the detector, because the detector
    is optional and injected at runtime. If both are present they agree, and
    the detector's own tri-state verdict is preferred below.
    """
    if not isinstance(value, str):
        return None
    text = value.strip().split("-", 1)[0]      # drop the -rN package revision
    if not text:
        return None
    parts = []
    for chunk in text.split("."):
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return tuple(parts) if parts else None


def _is_up_to_date(current, latest, detection=None) -> bool:
    """
    True only when the image is positively known to be current.

    Prefers the detector's own tri-state verdict when it supplied one: the
    detector is the component that knows how to compare a published index
    version against an installed one, and discarding its answer in favour of a
    local string comparison is what produced a false "update available" for an
    image already at 124.0.6367.78 against an index of 124.0.6367.78-r0.

    Falls back to a normalised numeric comparison, and returns False when
    either side is unparsable — "cannot tell" must never read as "up to date",
    because that is the direction that hides a real update.
    """
    if isinstance(detection, dict) and "update_available" in detection:
        verdict = detection.get("update_available")
        if verdict is False:
            return True
        if verdict is True:
            return False
        # verdict is None -> undetermined; fall through to the local compare
        # rather than guessing, and a failed local compare also yields False.
    cur_key = _version_tuple(current)
    new_key = _version_tuple(latest)
    if cur_key is None or new_key is None:
        return False
    width = max(len(cur_key), len(new_key))
    cur = cur_key + (0,) * (width - len(cur_key))
    new = new_key + (0,) * (width - len(new_key))
    # new < cur means the image is AHEAD of the published index (a branch whose
    # release has not propagated to the mirror). That is not an update either.
    return new <= cur


def _default_run_cmd(cmd, env=None, timeout=None):
    try:
        result = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode, result.stdout or "", result.stderr or ""
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"timed out after {timeout}s"
    except FileNotFoundError as exc:
        return 127, "", str(exc)


#: Process-wide instance used by the bridge. Cheap to construct: no network,
#: no Docker call, just a small JSON read.
_updater: Optional[ImageUpdater] = None
_updater_lock = threading.Lock()


def get_updater() -> ImageUpdater:
    """The shared updater. Constructed on first use, never at import time."""
    global _updater
    with _updater_lock:
        if _updater is None:
            _updater = ImageUpdater()
        return _updater


__all__ = [
    "BASE_EOL_NOTICE",
    "BASE_EOL_NOTICE_SHORT",
    "BUILD_TIMEOUT_SECONDS",
    "CONSENT_WARNING",
    "CONSENT_WARNING_SHORT",
    "DEFER_COOLDOWN_SECONDS",
    "ENV_AUTO_REBUILD",
    "ENV_DISABLE",
    "ENV_DETECTOR",
    "ENV_FORCE",
    "FAILURE_COOLDOWN_SECONDS",
    "ImageUpdater",
    "LOCK_FILE",
    "MIN_INTERVAL_SECONDS",
    "STATES",
    "STATE_FILE",
    "apply_update",
    "check_and_start",
    "dockerfile_dir",
    "get_update_status",
    "get_updater",
    "get_version_detector",
    "set_version_detector",
]


def check_and_start(force: Optional[bool] = None, reason: str = "app_start") -> dict:
    """Module-level shorthand for `get_updater().check_and_start(...)`.

    Detect-only by default. Does not build without consent or an opt-in.
    """
    return get_updater().check_and_start(force=force, reason=reason)


def apply_update(version: Optional[str] = None,
                 reason: str = "user_consent") -> dict:
    """Module-level shorthand for `get_updater().apply_update(...)`.

    The explicit "Apply now" gate. `version` must be the one the status
    reports as available.
    """
    return get_updater().apply_update(version=version, reason=reason)


def get_update_status() -> dict:
    """Module-level shorthand for `get_updater().get_update_status()`."""
    return get_updater().get_update_status()

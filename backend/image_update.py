"""
Chromium auto-update — the safe *rebuild* half.

WHAT "UPDATE CHROMIUM" MEANS HERE
---------------------------------
Chromium is not installed on the host. It lives inside the `isolated-chrome`
Docker image, built from the repo's own `Dockerfile`. So "updating Chromium"
means rebuilding that image. Numbers that drive every decision below:

    * a CACHED build takes ~31 s
    * a COLD build takes minutes and pulls ~300 MB
    * the resulting image is 1.31 GB

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
    4. NEVER MID-LAUNCH. A running `chrome-*` container defers the build.
    5. BOUNDED.       Timestamped start/finish/result, a debounce floor, and a
                      long cooldown after any failure. No retry loops.
    6. REFUSABLE.     `CHROME_ISOLATION_NO_AUTO_UPDATE=1` disables the whole
                      path. Auto-updating a browser is a policy decision, and
                      the user must be able to say no.

STDOUT IS THE IPC PROTOCOL — NEVER WRITE TO IT
-----------------------------------------------
`bridge.py` speaks newline-delimited JSON on **stdout**. A single stray
`print()` from a background thread interleaves with the response stream and
corrupts the protocol for the renderer. Every diagnostic in this module goes
to **stderr** through `_log()`. This is a real, previously-exploited trap in
this codebase (see `host_share.py`); it is asserted by the test suite.

ENVIRONMENT
-----------
    CHROME_ISOLATION_NO_AUTO_UPDATE=1   kill switch. Disables auto-check AND
                                       manual trigger. Nothing is built.
    CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1
                                       documented manual opt-in: run a rebuild
                                       at the next app start even when the
                                       version check says "no update". Still
                                       respects the single-flight lock and
                                       still defers while a profile container
                                       is running — those protect the install,
                                       they are not politeness knobs.
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

Register it either programmatically (preferred, from the detector's own
module init) or via `CHROME_ISOLATION_VERSION_DETECTOR=module:callable`.

**Fail-closed is mandatory and is what the default does.** With no detector
registered, `latest` is `None` and the updater reports `up_to_date` and builds
nothing. A module that cannot tell whether an update exists must never be
interpreted as "yes, rebuild" — a false positive here costs the user minutes
and 1.31 GB of I/O on every app start, and gets it wrong on every single one.

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

_STATES = (
    "idle", "checking", "building", "verifying", "ready", "failed",
    "up_to_date", "deferred", "disabled", "debounced", "interrupted",
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
        self._status: dict = self._read_state()

    # ------------------------------------------------------------ properties
    @property
    def image_name(self) -> str:
        return self._image

    @property
    def enabled(self) -> bool:
        """False when the kill switch is set. Nothing is built when False."""
        return not _flag(ENV_DISABLE)

    def force_requested(self) -> bool:
        """The documented manual opt-in: rebuild regardless of the version check."""
        return _flag(ENV_FORCE)

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

        Returns a dict with at least:
            state            one of _STATES
            started_at       unix ts when the current/last attempt began
            finished_at      unix ts when it ended
            error            failure detail, or None
            current_version  version detected in the installed image
            latest_version   version a fresh build would produce
        plus `enabled`, `forced`, `last_result`, `next_attempt_at` and
        `retry_in_seconds` for the UI.
        """
        status = dict(self._status)
        status.setdefault("state", "idle")
        status.setdefault("started_at", None)
        status.setdefault("finished_at", None)
        status.setdefault("error", None)
        status.setdefault("current_version", None)
        status.setdefault("latest_version", None)
        self._reconcile_stale_build(status)
        status["enabled"] = self.enabled
        status["forced"] = self.force_requested()
        status["next_attempt_at"] = status.get("next_attempt_at")
        status["retry_in_seconds"] = status.get("retry_in_seconds")
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
    def _preflight(self, force: bool) -> Tuple[bool, dict]:
        """Decide whether a build may start. Cheap, no Docker calls."""
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
        if force and not failed:
            # A manual "rebuild now" may ignore the routine debounce — that is
            # what the user asked for.
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
                        reason: str = "app_start") -> dict:
        """Kick off an update check **and return immediately**.

        This is the only method the request path is allowed to call. It does no
        Docker work, no version check and no waiting: it validates the kill
        switch, takes the single-flight lock (non-blocking), and hands the rest
        to a daemon thread.

        Returns one of:
            {"started": True,  "state": "checking"}
            {"started": False, "reason": "disabled"|"in_progress"|"debounced", ...}
        """
        if force is None:
            force = self.force_requested()

        if not self.enabled:
            _log(f"auto-update disabled via {ENV_DISABLE}; not checking ({reason})")
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
                allowed, refusal = self._preflight(force=bool(force))
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
                target=self._worker, args=(fd, bool(force), reason),
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
    def _worker(self, lock_fd: int, force: bool, reason: str) -> None:
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

            running = self._running_profile_containers()
            if running:
                msg = (f"deferring: {len(running)} profile container(s) running "
                       f"({', '.join(running[:4])})")
                _log(msg)
                self._write_state(
                    state="deferred", finished_at=self._now(),
                    last_result=msg, deferred_at=started,
                    retry_in_seconds=self._defer_cooldown,
                    next_attempt_at=started + self._defer_cooldown,
                )
                return

            current, latest = self._detect_versions()
            force = force or self.force_requested()
            if latest is not None and current is not None and latest == current and not force:
                _log(f"up to date (Chromium {current})")
                self._write_state(
                    state="up_to_date", finished_at=self._now(),
                    current_version=current, latest_version=latest,
                    last_result=f"already at Chromium {current}",
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
                )
                return
            if latest is None and not force:
                # Fail-closed: "cannot tell" is never "yes, rebuild".
                _log("no version detector registered or it could not determine a "
                     "latest version; not rebuilding")
                self._write_state(
                    state="up_to_date", finished_at=self._now(),
                    current_version=current, latest_version=None,
                    last_result="update availability unknown; not rebuilding",
                    retry_in_seconds=self._min_interval,
                    next_attempt_at=started + self._min_interval,
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

    def _detect_versions(self) -> Tuple[Optional[str], Optional[str]]:
        """(current, latest). `latest is None` means "do not rebuild"."""
        detected_current = None
        try:
            detected_current, _ = self._read_installed_versions()
        except Exception as exc:
            _log(f"could not read installed version: {exc}")

        detector = self._detector or get_version_detector()
        if detector is None:
            return detected_current, None
        try:
            raw = detector(self._image)
        except Exception as exc:
            _log(f"version detector raised ({exc}); treating as 'unknown'")
            return detected_current, None
        current, latest = _coerce_versions(raw)
        # The image itself is authoritative for "current" — a detector that
        # disagrees cannot make us skip a real update or chase a phantom one.
        return detected_current or current, latest


def _default_run_cmd(cmd, env=None, timeout=None):
    """Thin subprocess wrapper. Returns (returncode, stdout, stderr)."""
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
    "BUILD_TIMEOUT_SECONDS",
    "DEFER_COOLDOWN_SECONDS",
    "ENV_DISABLE",
    "ENV_DETECTOR",
    "ENV_FORCE",
    "FAILURE_COOLDOWN_SECONDS",
    "ImageUpdater",
    "LOCK_FILE",
    "MIN_INTERVAL_SECONDS",
    "STATE_FILE",
    "check_and_start",
    "dockerfile_dir",
    "get_update_status",
    "get_updater",
    "get_version_detector",
    "set_version_detector",
]


def check_and_start(force: Optional[bool] = None, reason: str = "app_start") -> dict:
    """Module-level shorthand for `get_updater().check_and_start(...)`."""
    return get_updater().check_and_start(force=force, reason=reason)


def get_update_status() -> dict:
    """Module-level shorthand for `get_updater().get_update_status()`."""
    return get_updater().get_update_status()

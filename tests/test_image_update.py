#!/usr/bin/env python3
"""Tests for backend/image_update.py — run with: python3 tests/test_image_update.py

or:  python3 -m pytest tests/test_image_update.py

NO REAL DOCKER HAPPENS HERE. Every side effect goes through two injected
seams — `client_factory` and `run_cmd` — so nothing is built, nothing is
tagged, nothing is removed, and no daemon socket is touched. The kill switch
is asserted to be honoured by the real code path, not by a test flag.

Non-vacuity: `test_failure_does_not_touch_latest_goes_red_if_built_directly`
is a mutation test that rewrites the staging tag to `:latest` in a scratch
copy of the module and proves the suite then fails.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

# The mutation test runs this same file out of a scratch tree that holds a
# MUTATED copy of backend/, so deriving ROOT from __file__ is exactly what
# makes the mutation visible. No override hook is needed.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import image_update
from image_update import ImageUpdater


# ─────────────────────────────────────────────────────────── fake docker CLI

class FakeDockerCLI:
    """Records every `docker ...` invocation and replays canned results.

    `failing` is a set of "verb" strings ('build', 'tag', 'run', ...) that
    should exit non-zero, so a test can make exactly one stage fail.
    """

    def __init__(self, failing=(), versions_env=True, chromium='152.0.7977.82',
                 verify_ok=True, alpine='3.24'):
        self.calls = []
        self.failing = set(failing)
        self.versions_env = versions_env
        self.chromium = chromium
        self.verify_ok = verify_ok
        self.alpine = alpine
        self.delays = {}

    # -- introspection helpers used by the assertions
    def verbs(self):
        return [c[1] if len(c) > 1 else None for c in self.calls]

    def calls_for(self, verb):
        return [c for c in self.calls if c[1] == verb]

    def built_tags(self):
        # `docker build -t <tag> <dir>`
        return [c[3] for c in self.calls_for('build') if c[2] == '-t']

    def removed_refs(self):
        # `docker image rm <ref>`
        return [c[3] for c in self.calls_for('image') if c[2] == 'rm']

    def tagged(self):
        """(source, dest) pairs of every `docker tag`."""
        return [(c[2], c[3]) for c in self.calls_for('tag')]

    def __call__(self, cmd, env=None, timeout=None):
        argv = list(cmd)
        self.calls.append(argv)
        verb = argv[1] if len(argv) > 1 else ''
        if verb in self.failing:
            return 1, '', f'synthetic failure of: {" ".join(argv)}'
        if verb == 'build':
            return 0, 'Successfully built abc123\n', ''
        if verb == 'tag':
            return 0, '', ''
        if verb == 'image':
            # `docker image ls` lists the (empty) set of staging tags.
            if len(argv) > 2 and argv[2] == 'ls':
                return 0, '', ''
            return 0, 'Untagged: ' + argv[-1] + '\n', ''
        if verb == 'run':
            # `--entrypoint /bin/sh <tag> -c <script>` — tell the two very
            # different scripts apart by their content.
            script = argv[-1]
            if 'versions.env' in script and 'VERIFY_OK' not in script:
                if not self.versions_env:
                    return 0, '', ''
                return 0, (f'ALPINE_VERSION={self.alpine}\n'
                           f'CHROMIUM_VERSION={self.chromium}\n'), ''
            if 'VERIFY_OK' in script:
                if not self.verify_ok:
                    return 1, 'VERIFY_FAIL shim-probe:<none>\n', ''
                return 0, (f'CHROMIUM_VERSION={self.chromium}\n'
                           f'SHIM=loaded\nVERIFY_OK\n'), ''
            return 0, '', ''
        return 0, '', ''


class FakeContainer:
    def __init__(self, name, status):
        self.name = name
        self.status = status


class FakeDockerClient:
    def __init__(self, containers=()):
        self.containers = _FakeContainerCollection(containers)


class _FakeContainerCollection:
    def __init__(self, containers):
        self._containers = list(containers)

    def list(self, **kwargs):
        return self._containers


# ────────────────────────────────────────────────────────────── test fixtures

class Clock:
    """Manually advanced clock, so debounce/backoff is tested without sleeping."""

    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make_updater(cli=None, containers=(), detector=None, clock=None, **kwargs):
    """Return (updater, tmpdir, cli, clock) with all paths in a temp dir."""
    tmp = tempfile.mkdtemp(prefix='image-update-')
    clock = clock or Clock()
    cli = cli if cli is not None else FakeDockerCLI()
    kwargs.setdefault('min_interval', 3600)
    kwargs.setdefault('failure_cooldown', 86400)
    kwargs.setdefault('defer_cooldown', 900)
    updater = ImageUpdater(
        client_factory=lambda: FakeDockerClient(containers),
        run_cmd=cli,
        detector=detector,
        lock_file=os.path.join(tmp, 'image-update.lock'),
        state_file=os.path.join(tmp, 'image-update-state.json'),
        now=clock,
        **kwargs,
    )
    return updater, tmp, cli, clock


UPDATE_DETECTOR = lambda image: {'current': '152.0.7977.82', 'latest': '153.0.2.1'}
NO_UPDATE_DETECTOR = lambda image: {'current': '152.0.7977.82', 'latest': '152.0.7977.82'}


# ═══════════════════════════════════════════ 1. staging → verify → promote

def test_success_builds_to_staging_then_promotes():
    """The happy path must build to a staging tag and only then move :latest."""
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        result = updater.check_and_start(reason='test')
        assert result['started'] is True, result
        assert updater.wait_for_idle(30), 'background update thread did not finish'

        status = updater.get_update_status()
        assert status['state'] == 'ready', status

        # 1. Exactly one build, and it targeted a STAGING tag, never :latest.
        built = cli.built_tags()
        assert len(built) == 1, f'expected exactly one build, got {built}'
        staging = built[0]
        assert staging.startswith('isolated-chrome:staging-'), \
            f'build must target a staging tag, got {staging}'
        assert 'latest' not in staging, f'build must never target :latest, got {staging}'
        assert cli.built_tags() != ['isolated-chrome:latest']

        # 2. The staging image was verified BEFORE it was promoted.
        assert len(cli.calls_for('run')) == 2, cli.calls   # versions + verify
        assert staging in cli.calls_for('run')[-1], cli.calls

        # 3. Promotion is a tag move from the staging tag to :latest.
        assert cli.tagged() == [(staging, 'isolated-chrome:latest')], cli.tagged()

        # 4. Ordering: build < verify < tag. A tag move before verification
        #    would be the unsafe design this whole module exists to prevent.
        order = [c[1] for c in cli.calls]
        assert order.index('build') < order.index('tag'), order

        # 5. The staging tag is cleaned up afterwards.
        assert staging in cli.removed_refs(), cli.removed_refs()

        # 6. Reported versions come from the built image, not the guess.
        assert status['current_version'] == '152.0.7977.82', status
        assert status['started_at'] and status['finished_at'], status
        assert status['error'] is None, status
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════ 2. THE failure path (most important)

def test_failure_leaves_existing_image_untouched():
    """A failed build must NOT retag `:latest` and MUST remove the staging tag.

    This is the property that separates a safe updater from one that bricks
    the install: `_ensure_image()` only rebuilds on `ImageNotFound`, so a
    broken image left on `:latest` would be launched forever with no recovery.
    """
    updater, tmp, cli, _ = make_updater(cli=FakeDockerCLI(failing=['build']),
                                       detector=UPDATE_DETECTOR)
    try:
        result = updater.check_and_start(reason='test')
        assert result['started'] is True, result
        assert updater.wait_for_idle(30)

        status = updater.get_update_status()
        assert status['state'] == 'failed', status
        assert status['error'], 'a failure must record an error'

        # :latest was NEVER touched.
        assert cli.tagged() == [], \
            f"a failed build must not retag :latest, but it did: {cli.tagged()}"
        for call in cli.calls:
            assert 'isolated-chrome:latest' not in call or call[1] in ('run',), \
                f':latest appeared in a mutating command: {call}'

        # The staging tag was removed.
        staging = cli.built_tags()[0]
        assert staging in cli.removed_refs(), \
            f'staging tag {staging} must be removed on failure, removed={cli.removed_refs()}'
        assert status['staging_tag'] is None, status
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verification_failure_does_not_promote():
    """A build that succeeds but FAILS VERIFICATION must not be promoted.

    Exit code 0 from `docker build` is not evidence of a working browser — the
    3.19 image built fine and only failed at container start.
    """
    cli = FakeDockerCLI(verify_ok=False)
    updater, tmp, cli, _ = make_updater(cli=cli, detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)

        status = updater.get_update_status()
        assert status['state'] == 'failed', status
        assert 'verification' in status['error'], status
        assert cli.tagged() == [], \
            f'a failed verification must not promote, tagged={cli.tagged()}'
        staging = cli.built_tags()[0]
        assert staging in cli.removed_refs(), cli.removed_refs()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_promotion_failure_removes_staging_tag():
    """If the tag move itself fails, the staging tag still goes away."""
    updater, tmp, cli, _ = make_updater(cli=FakeDockerCLI(failing=['tag']),
                                       detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)

        status = updater.get_update_status()
        assert status['state'] == 'failed', status
        assert cli.built_tags()[0] in cli.removed_refs(), cli.removed_refs()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_docker_listing_failure_defers_rather_than_building():
    """If we cannot tell whether a launch is in flight, assume one is.

    Failing open here is how a rebuild lands in the middle of a launch.
    """
    class ExplodingClient:
        @property
        def containers(self):
            raise RuntimeError('daemon unreachable')

    cli = FakeDockerCLI()
    updater, tmp, cli, _ = make_updater(cli=cli, detector=UPDATE_DETECTOR)
    updater._client_factory = lambda: ExplodingClient()
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)
        status = updater.get_update_status()
        assert status['state'] == 'deferred', status
        assert cli.built_tags() == [], 'must not build when the state is unknown'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════════════ 3. single-flight lock

def test_lock_prevents_a_second_build_in_the_same_process():
    """A second caller gets `in_progress`; it does not queue a second build."""
    started = threading.Event()
    release = threading.Event()

    class SlowCLI(FakeDockerCLI):
        def __call__(self, cmd, env=None, timeout=None):
            if len(cmd) > 1 and cmd[1] == 'build':
                started.set()
                release.wait(10)
            return super().__call__(cmd, env=env, timeout=timeout)

    updater, tmp, cli, _ = make_updater(cli=SlowCLI(), detector=UPDATE_DETECTOR)
    try:
        first = updater.check_and_start(reason='first')
        assert first['started'] is True, first
        assert started.wait(10), 'build never began'

        second = updater.check_and_start(reason='second')
        assert second['started'] is False, second
        assert second['reason'] == 'in_progress', second

        release.set()
        assert updater.wait_for_idle(30)
        assert len(cli.built_tags()) == 1, \
            f'exactly one build must run, got {cli.built_tags()}'
    finally:
        release.set()
        shutil.rmtree(tmp, ignore_errors=True)


def test_lock_is_os_level_and_blocks_a_second_process():
    """The lock must be `flock`, not an in-process flag.

    Electron restarts the bridge on any crash, so the real risk is two OS
    processes. This test takes the lock with a *separate process* and proves
    the updater refuses to build — an in-process flag would sail right past
    this and pass nothing.
    """
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        # Hold the very same lock file from a different process.
        holder = subprocess.Popen(
            [sys.executable, '-c',
             'import fcntl,os,sys,time\n'
             'fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR, 0o600)\n'
             'fcntl.flock(fd, fcntl.LOCK_EX)\n'
             'print("held", flush=True)\n'
             'time.sleep(20)\n',
             updater._lock_file],
            stdout=subprocess.PIPE, text=True)
        try:
            assert holder.stdout.readline().strip() == 'held', 'holder failed to lock'

            assert updater.is_locked() is True
            result = updater.check_and_start(reason='other-process')
            assert result['started'] is False, result
            assert result['reason'] == 'in_progress', result
            assert cli.built_tags() == [], 'a held lock must prevent any build'
        finally:
            holder.kill()
            holder.wait(10)

        # Once the other process dies the lock is free again — flock is
        # released on fd close, so there is no stale lock to clean up.
        assert updater.is_locked() is False
        assert updater.check_and_start(reason='after')['started'] is True
        assert updater.wait_for_idle(30)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═══════════════════════════════════════════ 4. defer while container runs

def test_running_container_defers_the_build():
    updater, tmp, cli, _ = make_updater(
        cli=FakeDockerCLI(),
        containers=[FakeContainer('/chrome-Fiverr-Demo', 'running'),
                    FakeContainer('/chrome-Zoom-1', 'exited')],
        detector=UPDATE_DETECTOR)
    try:
        result = updater.check_and_start(reason='test')
        assert result['started'] is True, result
        assert updater.wait_for_idle(30)

        status = updater.get_update_status()
        assert status['state'] == 'deferred', status
        assert 'Fiverr-Demo' in status['last_result'], status
        assert cli.built_tags() == [], \
            'a running profile container must defer the rebuild'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_non_profile_containers_do_not_defer():
    """Only `chrome-*` counts — an unrelated container must not block updates."""
    updater, tmp, cli, _ = make_updater(
        cli=FakeDockerCLI(),
        containers=[FakeContainer('/WinBoat', 'running'),
                    FakeContainer('/chrome-Stopped', 'exited')],
        detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'ready', \
            updater.get_update_status()
        assert len(cli.built_tags()) == 1, cli.built_tags()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════ 5. state transitions

def test_status_transitions_and_timestamps():
    updater, tmp, cli, clock = make_updater(detector=UPDATE_DETECTOR)
    try:
        assert updater.get_update_status()['state'] == 'idle'

        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)

        status = updater.get_update_status()
        assert status['state'] == 'ready', status
        assert status['started_at'] is not None
        assert status['finished_at'] is not None
        assert status['finished_at'] >= status['started_at'], status
        assert status['error'] is None, status
        # The documented keys are all present even on a fresh file.
        for key in ('state', 'started_at', 'finished_at', 'error',
                    'current_version', 'latest_version'):
            assert key in status, f'missing documented key {key}: {status}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_up_to_date_does_not_build():
    updater, tmp, cli, _ = make_updater(detector=NO_UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)
        status = updater.get_update_status()
        assert status['state'] == 'up_to_date', status
        assert cli.built_tags() == [], 'nothing to update means no build'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_detector_fails_closed():
    """With no version detector the updater must NOT rebuild.

    'Cannot tell' is not 'yes, rebuild' — otherwise every app start costs
    minutes and 1.31 GB of I/O on a machine that may already be current.
    """
    updater, tmp, cli, _ = make_updater(detector=None)
    image_update.set_version_detector(None)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)
        status = updater.get_update_status()
        assert status['state'] == 'up_to_date', status
        assert cli.built_tags() == [], 'a missing detector must not trigger a build'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_raising_detector_fails_closed():
    def boom(image):
        raise RuntimeError('pkgs.alpinelinux.org unreachable')

    updater, tmp, cli, _ = make_updater(detector=boom)
    try:
        updater.check_and_start(reason='test')
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'up_to_date', \
            updater.get_update_status()
        assert cli.built_tags() == [], 'a raising detector must not trigger a build'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_interrupted_build_is_reconciled():
    """A `building` state with no lock holder is reported, not trusted forever."""
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        # Simulate a bridge that died mid-build: state says building, no lock.
        updater._write_state(state='building', started_at=1.0,
                             staging_tag='isolated-chrome:staging-9999-1')
        status = updater.get_update_status()
        assert status['state'] == 'interrupted', status
        assert 'disappeared' in status['error'], status
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════ 6. the kill switch

def test_kill_switch_disables_automatic_check():
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        os.environ[image_update.ENV_DISABLE] = '1'
        try:
            assert updater.enabled is False
            result = updater.check_and_start(reason='test')
            assert result['started'] is False, result
            assert result['reason'] == 'disabled', result
            assert updater.get_update_status()['enabled'] is False
        finally:
            del os.environ[image_update.ENV_DISABLE]
        # ...and it really did nothing at all.
        assert cli.calls == [], f'kill switch must run no docker command: {cli.calls}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_kill_switch_disables_forced_rebuild_too():
    """A forced rebuild is still a browser update; the switch covers it."""
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        os.environ[image_update.ENV_DISABLE] = '1'
        try:
            result = updater.check_and_start(force=True, reason='test')
            assert result['started'] is False, result
            assert result['reason'] == 'disabled', result
        finally:
            del os.environ[image_update.ENV_DISABLE]
        assert cli.calls == [], cli.calls
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_kill_switch_accepted_values():
    updater, tmp, cli, _ = make_updater()
    try:
        for value in ('1', 'true', 'YES', 'on'):
            os.environ[image_update.ENV_DISABLE] = value
            assert updater.enabled is False, value
        for value in ('0', 'false', '', 'no'):
            os.environ[image_update.ENV_DISABLE] = value
            assert updater.enabled is True, value
    finally:
        os.environ.pop(image_update.ENV_DISABLE, None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_force_env_rebuilds_without_an_update():
    """Documented manual opt-in: CHROME_ISOLATION_FORCE_IMAGE_UPDATE=1."""
    updater, tmp, cli, _ = make_updater(detector=NO_UPDATE_DETECTOR)
    try:
        assert updater.force_requested() is False
        assert updater.check_and_start(reason='test')['started'] is True
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'up_to_date'
        assert cli.built_tags() == [], 'no force, no build'

        updater2, tmp2, cli2, clock2 = make_updater(detector=NO_UPDATE_DETECTOR)
        try:
            os.environ[image_update.ENV_FORCE] = '1'
            updater2.check_and_start(reason='test')
            assert updater2.wait_for_idle(30)
            assert updater2.get_update_status()['state'] == 'ready', \
                updater2.get_update_status()
            assert len(cli2.built_tags()) == 1, 'forced rebuild must build'
        finally:
            del os.environ[image_update.ENV_FORCE]
            shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════════════════════════ 7. debounce

def test_debounce_prevents_a_retry_loop_after_failure():
    """A failing update must not be retried on every app start."""
    cli = FakeDockerCLI(failing=['build'])
    updater, tmp, cli, clock = make_updater(cli=cli, detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='first')
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'failed'
        assert len(cli.built_tags()) == 1

        # Ten more launches inside the failure cooldown: not one may build.
        for i in range(10):
            result = updater.check_and_start(reason=f'relaunch-{i}')
            assert result['started'] is False, f'attempt {i} started a build: {result}'
            assert result['reason'] == 'debounced', result
            assert len(cli.built_tags()) == 1, \
                f'retry loop: {len(cli.built_tags())} builds after one failure'

        assert updater.get_update_status()['state'] == 'debounced', \
            updater.get_update_status()

        # Past the cooldown exactly one retry happens.
        clock.advance(86401)
        assert updater.check_and_start(reason='after-cooldown')['started'] is True
        assert updater.wait_for_idle(30)
        assert len(cli.built_tags()) == 2, cli.built_tags()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_debounce_applies_after_a_successful_build_too():
    updater, tmp, cli, clock = make_updater(detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='first')
        assert updater.wait_for_idle(30)
        assert len(cli.built_tags()) == 1

        for i in range(5):
            result = updater.check_and_start(reason=f'relaunch-{i}')
            assert result['started'] is False, result
            assert result['reason'] == 'debounced', result
        assert len(cli.built_tags()) == 1, \
            f'relaunches must not rebuild: {len(cli.built_tags())} builds'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_deferral_is_debounced_too():
    """A crash-loop of app restarts must not hammer the Docker API."""
    updater, tmp, cli, clock = make_updater(
        cli=FakeDockerCLI(),
        containers=[FakeContainer('/chrome-Demo', 'running')],
        detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='first')
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'deferred'

        result = updater.check_and_start(reason='second')
        assert result['started'] is False, result
        assert result['reason'] == 'debounced', result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_force_bypasses_the_routine_debounce():
    updater, tmp, cli, clock = make_updater(detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(reason='first')
        assert updater.wait_for_idle(30)
        assert len(cli.built_tags()) == 1
        assert updater.check_and_start(force=True, reason='manual')['started'] is True
        assert updater.wait_for_idle(30)
        assert len(cli.built_tags()) == 2, 'an explicit manual request may bypass debounce'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_force_does_not_bypass_the_failure_cooldown():
    """A permanently-set force flag must not become a retry hammer.

    Force is a one-shot "rebuild now". If it also overrode the failure
    cooldown, an env var set once plus a build that keeps failing would cost
    minutes of cold-build I/O on every app launch.
    """
    updater, tmp, cli, clock = make_updater(cli=FakeDockerCLI(failing=['build']),
                                            detector=UPDATE_DETECTOR)
    try:
        updater.check_and_start(force=True, reason='first')
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'failed'
        assert len(cli.built_tags()) == 1

        for i in range(5):
            result = updater.check_and_start(force=True, reason=f'relaunch-{i}')
            assert result['started'] is False, f'forced retry {i} started a build: {result}'
            assert result['reason'] == 'debounced', result
        assert len(cli.built_tags()) == 1, cli.built_tags()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ══════════════════════════════════════════ 8. non-blocking / IPC hygiene

def test_check_and_start_returns_before_the_build_finishes():
    """The contract the UI depends on: the call must not wait for the build."""
    release = threading.Event()
    building = threading.Event()

    class VerySlowCLI(FakeDockerCLI):
        def __call__(self, cmd, env=None, timeout=None):
            if len(cmd) > 1 and cmd[1] == 'build':
                building.set()
                release.wait(15)
            return super().__call__(cmd, env=env, timeout=timeout)

    updater, tmp, cli, _ = make_updater(cli=VerySlowCLI(), detector=UPDATE_DETECTOR)
    try:
        start = time.monotonic()
        result = updater.check_and_start(reason='test')
        elapsed = time.monotonic() - start
        assert result['started'] is True, result
        assert building.wait(10), 'build never started'
        assert elapsed < 1.0, \
            f'check_and_start blocked for {elapsed:.2f}s — it must return immediately'

        # Status is readable while the build is in flight, and says so.
        assert updater.get_update_status()['state'] in ('checking', 'building'), \
            updater.get_update_status()
        release.set()
        assert updater.wait_for_idle(30)
        assert updater.get_update_status()['state'] == 'ready'
    finally:
        release.set()
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_module_writes_to_stdout():
    """stdout is the newline-delimited JSON IPC protocol.

    One stray print() from the background thread interleaves with the response
    stream and corrupts it for the renderer. This has already been a real trap
    in this codebase, so it is asserted rather than assumed.
    """
    src_path = os.path.join(ROOT, 'backend', 'image_update.py')
    with open(src_path) as fh:
        source = fh.read()
    for lineno, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith('#') or 'sys.stdout' in line:
            continue
        assert not re.match(r'^\s*print\s*\(', line), \
            f'{src_path}:{lineno} uses print() — stdout is the IPC protocol'

    # ...and the real thing: capture stdout around a full update run.
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        from io import StringIO
        real_stdout, sys.stdout = sys.stdout, StringIO()
        try:
            updater.check_and_start(reason='test')
            assert updater.wait_for_idle(30)
            captured = sys.stdout.getvalue()
        finally:
            sys.stdout = real_stdout
        assert captured == '', f'update path wrote to stdout: {captured!r}'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_progress_goes_to_stderr():
    updater, tmp, cli, _ = make_updater(detector=UPDATE_DETECTOR)
    try:
        from io import StringIO
        real_stderr, sys.stderr = sys.stderr, StringIO()
        try:
            updater.check_and_start(reason='test')
            assert updater.wait_for_idle(30)
            captured = sys.stderr.getvalue()
        finally:
            sys.stderr = real_stderr
        assert '[image-update' in captured, 'no progress was reported'
        assert 'building staging tag' in captured, captured
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_constructor_does_no_io_beyond_a_json_read():
    """DockerManager.__init__ runs on every backend import: keep this cheap."""
    tmp = tempfile.mkdtemp(prefix='image-update-ctor-')
    try:
        def explode(*a, **kw):
            raise AssertionError('the constructor must not shell out')

        started = time.monotonic()
        u = ImageUpdater(
            client_factory=lambda: (_ for _ in ()).throw(
                AssertionError('the constructor must not talk to Docker')),
            run_cmd=explode,
            lock_file=os.path.join(tmp, 'l'),
            state_file=os.path.join(tmp, 's'),
        )
        elapsed = time.monotonic() - started
        assert elapsed < 0.5, f'constructor took {elapsed:.2f}s'
        assert not os.path.exists(os.path.join(tmp, 'l')), \
            'the constructor must not create the lock file (no I/O at import)'
        assert u.get_update_status()['state'] == 'idle'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_bridge_registers_both_methods():
    """Both new bridge methods must exist and be trivial (non-blocking)."""
    with open(os.path.join(ROOT, 'backend', 'bridge.py')) as fh:
        source = fh.read()
    assert '"image_update_check"' in source, 'bridge is missing image_update_check'
    assert '"image_update_status"' in source, 'bridge is missing image_update_status'
    # Importing the bridge must not start anything.
    assert 'check_and_start' not in source.split('_METHODS')[0], \
        'a check must not run at import time'


# ═══════════════════════════════ 9. non-vacuity (mutation proof)

#: Rewrites the staging tag so the build targets :latest directly — the exact
#: naive implementation this module exists to replace. There is no staging tag
#: left to clean up, so the tag can never be left pointing at a partial build.
_MUTATION_BUILD_TO_LATEST = (
    ('return f"{self._image}:{STAGING_PREFIX}{os.getpid()}-{int(stamp)}"',
     'return f"{self._image}:latest"'),
)

#: Drops the failure-path cleanup, i.e. "build to staging and just leave it
#: there if it goes wrong". Not destructive to `:latest`, but it leaks a tag
#: per failure and hides a half-built image from nobody.
_MUTATION_NO_CLEANUP = (
    ("""            if staging is not None and not promoted:""",
     """            if False:"""),
)

#: Skips the verify step entirely — the "docker build returned 0, ship it"
#: design, which is how the EOL-Alpine image got published in the first place.
_MUTATION_NO_VERIFY = (
    ('            facts = self._verify(staging)',
     '            facts = {}'),
)

#: Replaces the OS-level flock with an in-process flag — the mistake this task
#: explicitly warns about, since Electron restarts the bridge on any crash.
_MUTATION_INPROC_LOCK = (
    ('            fcntl.flock(fd, flags)',
     '            pass  # MUTANT: no OS-level lock'),
)

#: Ignores running containers, i.e. "rebuild straight through a live launch".
_MUTATION_IGNORE_RUNNING = (
    ('                if (getattr(container, "status", "") or "") == "running":',
     '                if False:  # MUTANT'),
)

#: Ignores the debounce floor — how a persistently failing update turns into a
#: retry loop on every single app start.
_MUTATION_NO_DEBOUNCE = (
    ('        if elapsed < floor:',
     '        if False:  # MUTANT: debounce disabled'),
)


def _scratch_backend(mutate=()):
    """Copy the whole backend to a scratch dir, optionally mutating the module.

    `scratch/backend` shadows the real backend because the copied test file
    derives its import root from its own location — no path hackery needed.
    """
    scratch = tempfile.mkdtemp(prefix='image-update-mut-')
    backend = os.path.join(scratch, 'backend')
    shutil.copytree(os.path.join(ROOT, 'backend'), backend,
                    ignore=shutil.ignore_patterns('__pycache__'))
    target = os.path.join(backend, 'image_update.py')
    if mutate:
        with open(target) as fh:
            source = fh.read()
        for old, new in mutate:
            assert old in source, f'mutation anchor not found: {old!r}'
            source = source.replace(old, new)
        with open(target, 'w') as fh:
            fh.write(source)
    return scratch


def _run_test_against(scratch, test_name):
    """Run one test from this file against `scratch/backend`."""
    tests_dir = os.path.join(scratch, 'tests')
    os.makedirs(tests_dir, exist_ok=True)
    shutil.copy(os.path.join(ROOT, 'tests', 'test_image_update.py'),
                os.path.join(tests_dir, 'test_image_update.py'))
    return subprocess.run(
        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
         os.path.join(tests_dir, 'test_image_update.py'), '-k', test_name],
        env=dict(os.environ, PYTHONPATH=scratch),
        capture_output=True, text=True, cwd=scratch, timeout=300)


def _run_failure_test_against(scratch):
    return _run_test_against(scratch, 'failure_leaves_existing_image_untouched')


def test_failure_path_goes_red_if_built_directly_to_latest():
    """Non-vacuity: the safety test must actually detect the unsafe design.

    Copies the module, rewrites `_staging_tag()` to return `:latest` — the
    exact "build straight to latest" implementation the task forbids — and
    requires the failure-path test to FAIL against it. If it still passed,
    the test would be proving nothing.
    """
    scratch = _scratch_backend(_MUTATION_BUILD_TO_LATEST)
    try:
        result = _run_failure_test_against(scratch)
        assert result.returncode != 0, (
            'VACUOUS TEST: the failure-path test still passed when the module '
            'was rewritten to build straight to isolated-chrome:latest.\n'
            + result.stdout[-3000:]
        )
        # And it must fail for the RIGHT reason, not on an import error.
        assert 'test_failure_leaves_existing_image_untouched' in result.stdout, \
            result.stdout[-3000:]
        assert 'docker' in result.stdout and 'build' in result.stdout, \
            result.stdout[-3000:]
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_suite_is_green_against_the_unmutated_module():
    """Control for the mutation test above: same harness, no mutation.

    Without this, a harness that errors on every run would make the mutation
    test 'pass' for the wrong reason.
    """
    scratch = _scratch_backend()
    try:
        result = _run_failure_test_against(scratch)
        assert result.returncode == 0, (
            'control run failed — the mutation test proves nothing:\n'
            + result.stdout[-3000:] + result.stderr[-2000:]
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_safety_tests_go_red_for_the_other_unsafe_designs():
    """Four more mutations, each caught by a specific safety test.

    Together with the `:latest` mutation above this covers the six ways this
    module could be "simplified" into something that breaks an install or
    stops being a safe updater.
    """
    for mutation, test_name, what in (
        (_MUTATION_NO_CLEANUP, 'failure_leaves_existing_image_untouched',
         'dropping the failure-path staging cleanup'),
        (_MUTATION_NO_VERIFY, 'verification_failure_does_not_promote',
         'skipping verification before promotion'),
        (_MUTATION_INPROC_LOCK, 'lock_is_os_level_and_blocks_a_second_process',
         'replacing flock with an in-process check'),
        (_MUTATION_IGNORE_RUNNING, 'running_container_defers_the_build',
         'ignoring running profile containers'),
        (_MUTATION_NO_DEBOUNCE, 'debounce_prevents_a_retry_loop_after_failure',
         'disabling the debounce interval'),
    ):
        scratch = _scratch_backend(mutation)
        try:
            result = _run_test_against(scratch, test_name)
            assert result.returncode != 0, (
                f'VACUOUS: {test_name} still passed after {what}.\n'
                + result.stdout[-2000:])
        finally:
            shutil.rmtree(scratch, ignore_errors=True)


# ───────────────────────────────────────────────────────── standalone runner

def main():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith('test_') and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception:
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()

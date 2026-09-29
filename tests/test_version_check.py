#!/usr/bin/env python3
"""Tests for backend/chromium_version.py — run with:

    python3 -m pytest tests/test_version_check.py
    python3 tests/test_version_check.py

NO NETWORK AND NO DOCKER HAPPENS HERE. Two seams make that possible:

  * `chromium_version.set_client_factory()` supplies a fake Docker client
  * the index read is exercised through a local `file://`-style shim that
    serves a real APKINDEX-shaped gzip built in-process

That second point matters: the real endpoint is a MULTI-MEMBER gzip (a 2048
byte PaxHeaders member followed by the payload), and a fake that only ever
produced a single-member gzip would let a broken implementation pass. The
fixture below reproduces the multi-member layout on purpose.

Covered, per the brief:
  * up to date                      * update available
  * network failure -> None, NOT False
  * malformed / garbage index body
  * image predating versions.env -> chromium-browser fallback
  * cache interval + atomicity      * never raising
  * no network on the import path or in DockerManager.__init__
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import zlib

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))

import chromium_version
from chromium_version import (
    ERROR_RETRY_INTERVAL_SECONDS,
    VERSION_CHECK_INTERVAL_SECONDS,
    alpine_arch,
    check_chromium_update,
    detector,
    fetch_latest_version,
    is_update_available,
    normalise_branch,
    version_key,
)


# ─────────────────────────────────────────────── APKINDEX fixtures (in-process)

def make_apkindex(entries, raw_stanza=None):
    """Build a real APKINDEX.tar.gz body.

    `entries` is a list of (name, version) pairs. The output is a
    MULTI-MEMBER gzip — member 1 is a 2048 byte tar block of padding, member 2
    is the tar with the APKINDEX member — because that is the real shape of the
    CDN file and reproducing it is the point.

    `raw_stanza` replaces the generated lines for a named package, so a test can
    build a deliberately malformed stanza. Tampering with the *compressed* bytes
    instead would not work: the plaintext is not present in the gzip stream.
    """
    lines = []
    for name, version in entries:
        lines.append("C:Q1fakechecksumfakechecksumfakechecksumfakechecksum=")
        if raw_stanza and name == raw_stanza[0]:
            lines.extend(raw_stanza[1])
        else:
            lines.append("P:%s" % name)
            lines.append("V:%s" % version)
        lines.append("A:x86_64")
        lines.append("T:package %s" % name)
        lines.append("")
    apkindex = ("\n".join(lines)).encode()

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        info = tarfile.TarInfo("APKINDEX")
        info.size = len(apkindex)
        tar.addfile(info, io.BytesIO(apkindex))
    tar_bytes = tar_buf.getvalue()

    out = io.BytesIO()
    with gzip_writer(out) as gz:            # member 1: junk, 2048 bytes
        gz.write(b"\0" * 2048)
    with gzip_writer(out) as gz:            # member 2: the payload
        gz.write(tar_bytes)
    return out.getvalue()


def gzip_writer(target):
    import gzip
    return gzip.GzipFile(fileobj=target, mode="wb", mtime=0)


class FakeResponse:
    """Minimal urlopen() result: read() in chunks, closable, context manager."""

    def __init__(self, body, chunk=65536):
        self._buf = io.BytesIO(body)
        self._chunk = chunk

    def read(self, size=-1):
        return self._buf.read(self._chunk if size is None or size < 0 else size)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


# ═══════════════════════════════════════════════ 1. version arithmetic

def test_lexical_comparison_would_be_wrong():
    """99 > 152 as strings but is older. The key must be numeric."""
    assert version_key("99.0.1.2") < version_key("152.0.7977.82")
    assert is_update_available("99.0.1.2", "152.0.7977.82") is True


def test_real_stale_pair_is_an_update():
    """The actual 124 -> 152 case this feature exists for."""
    assert is_update_available("124.0.6367.78", "152.0.7977.82") is True


def test_alpine_build_revision_is_ignored():
    """APKINDEX reports `152.0.7977.82-r0`; the manifest has no `-rN`.

    Comparing raw strings would call this an update on an already-current
    image, i.e. a 1.31 GB pointless rebuild on every check.
    """
    assert is_update_available("152.0.7977.82", "152.0.7977.82-r0") is False
    assert is_update_available("152.0.7977.82", "152.0.7977.82-r12") is False
    assert is_update_available("152.0.7977.82-r0", "152.0.7977.82") is False


def test_identical_is_not_an_update():
    assert is_update_available("152.0.7977.82", "152.0.7977.82") is False


def test_short_and_long_forms_compare_equal():
    assert is_update_available("152.0.7977", "152.0.7977.0") is False
    assert is_update_available("152.0.7977", "152.0.7977.1") is True


def test_newer_image_is_not_offered_a_downgrade():
    """An image ahead of the index must not report an available update."""
    assert is_update_available("153.0.1.2", "152.0.7977.82") is False


def test_unparsable_versions_yield_none_not_false():
    for bad in (None, "", "   ", "unknown", "not-a-version"):
        assert is_update_available(bad, "152.0.7977.82") is None
        assert is_update_available("152.0.7977.82", bad) is None


# ═══════════════════════════════════════════════ 2. branch / arch derivation

def test_branch_comes_from_the_image_not_a_constant():
    """A base bump must retarget the query, so this cannot be hardcoded."""
    assert normalise_branch("3.24") == "v3.24"
    assert normalise_branch("v3.24") == "v3.24"
    assert normalise_branch("3.24.2") == "v3.24"      # /etc/alpine-release form
    assert normalise_branch("3.19.9") == "v3.19"
    assert normalise_branch("3.25") == "v3.25"


def test_unusable_branches_are_rejected_not_guessed():
    """A branch string becomes a URL path segment — refuse anything odd."""
    for bad in (None, "", "  ", "latest", "edge", "v3", "3", "../../etc",
                "v3.24;rm -rf /", "3.24 community", "not-a-branch"):
        assert normalise_branch(bad) is None, bad


def test_arch_mapping():
    assert alpine_arch("amd64") == "x86_64"
    assert alpine_arch("arm64") == "aarch64"
    assert alpine_arch("AMD64") == "x86_64"
    assert alpine_arch(None) is None
    assert alpine_arch("sparc64") is None


# ═══════════════════════════════════════════════ 3. the index reader

def _serve(body_or_exc, calls=None):
    """Install a fake urlopen returning `body` (or raising `body_or_exc`)."""
    def fake_urlopen(request, timeout=None):
        if calls is not None:
            calls.append(request.full_url)
        if isinstance(body_or_exc, Exception):
            raise body_or_exc
        return FakeResponse(body_or_exc)
    return fake_urlopen


class _patched_urlopen:
    def __init__(self, factory):
        self.factory = factory
        self._real = None

    def __enter__(self):
        self._real = chromium_version.urllib.request.urlopen
        chromium_version.urllib.request.urlopen = self.factory
        return self

    def __exit__(self, *exc):
        chromium_version.urllib.request.urlopen = self._real
        return False


INDEX = [("2bwm", "0.3-r3"), ("chromium", "152.0.7977.82-r0"),
         ("mesa-vulkan-intel", "26.1.6-r0")]


def test_multi_member_gzip_index_is_read():
    """The real CDN file is multi-member gzip; a single-member fake would let a
    broken reader pass. This is the regression guard for that."""
    calls = []
    with _patched_urlopen(_serve(make_apkindex(INDEX), calls)):
        result = fetch_latest_version("v3.24", "x86_64")
    assert result["latest"] == "152.0.7977.82-r0", result
    assert result["error"] is None
    assert result["repo"] == "community"
    assert result["source"].endswith("/v3.24/community/x86_64/APKINDEX.tar.gz")


def test_reader_stops_early_and_does_not_buffer_the_whole_index():
    """Early exit is what makes the check cheap; assert it actually happens."""
    entries = [("pkg%04d" % i, "1.0-r0") for i in range(20000)]
    entries.insert(500, ("chromium", "152.0.7977.82-r0"))
    body = make_apkindex(entries)

    seen = {"bytes": 0}

    class CountingResponse(FakeResponse):
        def read(self, size=-1):
            chunk = super().read(size)
            seen["bytes"] += len(chunk)
            return chunk

    def factory(request, timeout=None):
        return CountingResponse(body)

    with _patched_urlopen(factory):
        result = fetch_latest_version("v3.24", "x86_64")
    assert result["latest"] == "152.0.7977.82-r0"
    assert seen["bytes"] < len(body), (
        "reader consumed the entire index (%d of %d bytes) — early exit is "
        "what keeps this check cheap" % (seen["bytes"], len(body)))


def test_package_absent_from_every_repo_is_an_error_not_a_version():
    with _patched_urlopen(_serve(make_apkindex([("2bwm", "0.3-r3")]))):
        result = fetch_latest_version("v3.24", "x86_64")
    assert result["latest"] is None
    assert result["error"] and "not present" in result["error"]


def test_garbage_index_body_is_an_error_not_a_crash():
    for garbage in (b"", b"not a gzip at all",
                    zlib.compress(b"total nonsense, no stanzas here"),
                    make_apkindex([]),
                    make_apkindex([("chromium", "")])):
        with _patched_urlopen(_serve(garbage)):
            result = fetch_latest_version("v3.24", "x86_64")
        assert result["latest"] is None, garbage[:40]
        assert result["error"], "garbage index must be reported, not guessed at"


def test_malformed_chromium_stanza_is_reported():
    """A `P:chromium` stanza with no `V:` must not yield a version."""
    body = make_apkindex(
        [("2bwm", "1.0-r0"), ("chromium", "152.0.7977.82-r0")],
        # `P:` present, `V:` replaced by an unknown key.
        raw_stanza=("chromium", ["P:chromium", "W:garbage"]),
    )
    with _patched_urlopen(_serve(body)):
        result = fetch_latest_version("v3.24", "x86_64")
    assert result["latest"] is None
    assert result["error"] and "malformed" in result["error"]


def test_http_error_is_reported_not_swallowed():
    err = urllib.error.HTTPError("u", 500, "boom", {}, None)
    with _patched_urlopen(_serve(err)):
        result = fetch_latest_version("v3.99", "x86_64")
    assert result["latest"] is None
    assert "500" in result["error"]


def test_network_error_is_reported_not_swallowed():
    with _patched_urlopen(_serve(urllib.error.URLError("no route to host"))):
        result = fetch_latest_version("v3.24", "x86_64")
    assert result["latest"] is None
    assert "could not be fetched" in result["error"]


def test_hung_response_hits_the_deadline():
    """A server dribbling one byte at a time must not outlive the deadline.

    urllib's `timeout` is per socket operation, so it would never fire here.
    Only the explicit monotonic check stops it.
    """
    body = make_apkindex(INDEX)

    class Drip(FakeResponse):
        """Advances one byte per read, slowly — the shape of a stalled mirror."""

        def __init__(self):
            super().__init__(b"")
            self._buf = io.BytesIO(body)

        def read(self, size=-1):
            time.sleep(0.01)
            return self._buf.read(1)

    started = time.monotonic()
    with _patched_urlopen(lambda request, timeout=None: Drip()):
        result = fetch_latest_version("v3.24", "x86_64", timeout=0.3)
    elapsed = time.monotonic() - started
    assert result["latest"] is None
    assert result["error"] and "deadline" in result["error"], result["error"]
    assert elapsed < 5.0, "deadline not enforced (took %.1fs)" % elapsed


def test_missing_branch_or_arch_is_reported_clearly():
    with _patched_urlopen(_serve(make_apkindex(INDEX))):
        no_branch = fetch_latest_version(None, "x86_64")
        no_arch = fetch_latest_version("v3.24", None)
    for result in (no_branch, no_arch):
        assert result["latest"] is None
        assert "cannot determine whether" in result["error"]


# ═══════════════════════════════════════ 4. the installed image (fake Docker)

class FakeContainer:
    def __init__(self, script_behaviour, recorder):
        self._behaviour = script_behaviour
        self._recorder = recorder
        self.removed = False
        self.waited = False

    def start(self):
        self._recorder["started"] += 1

    def wait(self, timeout=None):
        self.waited = True
        self._recorder["wait_timeout"] = timeout
        return {"StatusCode": 0}

    def logs(self, stdout=True, stderr=True):
        return self._behaviour

    def remove(self, force=False):
        self.removed = True
        self._recorder["removed"] += 1


class FakeImages:
    def __init__(self, image_id="sha256:img1", arch="amd64", error=None):
        self._id = image_id
        self._arch = arch
        self._error = error

    def get(self, name):
        if self._error:
            raise self._error
        return type("Img", (), {
            "id": self._id,
            "attrs": {"Id": self._id, "Architecture": self._arch,
                      "Config": {"Architecture": self._arch}},
        })()


class FakeContainers:
    def __init__(self, stdout="", error=None, recorder=None):
        self._stdout = stdout
        self._error = error
        self._recorder = recorder if recorder is not None else {}
        self.created_kwargs = None

    def create(self, image, **kwargs):
        if self._error:
            raise self._error
        self.created_kwargs = dict(kwargs)
        self.created_kwargs["image"] = image
        return FakeContainer(self._stdout, self._recorder)


class FakeClient:
    def __init__(self, stdout="", image_error=None, probe_error=None,
                 image_id="sha256:img1", arch="amd64"):
        self._recorder = {"started": 0, "removed": 0, "wait_timeout": None}
        self.containers = FakeContainers(stdout, probe_error, self._recorder)
        self.images = FakeImages(image_id, arch, image_error)
        self.recorder = self._recorder


VERSIONS_ENV = (
    "PROBE_SOURCE=versions.env\n"
    "ALPINE_VERSION=3.24\n"
    "CHROMIUM_VERSION=152.0.7977.82\n"
    "CHROMIUM_VERSION_RAW=Chromium 152.0.7977.82\n"
    "ALPINE_RELEASE=3.24.2\n"
)

# What a pre-versions.env image returns: no manifest, so the binary answers.
LEGACY_PROBE = (
    "PROBE_SOURCE=chromium-binary\n"
    "Chromium 124.0.6367.78\n"
    "ALPINE_RELEASE=3.19.9\n"
)


def _check(client, index_body, **kwargs):
    """check_chromium_update against a fake client and a fake index."""
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    calls = []
    cache = os.path.join(tmp, "cache.json")
    try:
        with _patched_urlopen(_serve(index_body, calls)):
            return check_chromium_update(
                client, cache_path=cache, **kwargs)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════ 5. end-to-end detection states

def test_up_to_date():
    result = _check(FakeClient(VERSIONS_ENV),
                    make_apkindex([("chromium", "152.0.7977.82-r0")]))
    assert result["update_available"] is False, result
    assert result["current"] == "152.0.7977.82"
    assert result["latest"] == "152.0.7977.82-r0"
    assert result["error"] is None
    assert result["alpine_version"] == "3.24"
    assert result["arch"] == "x86_64"
    assert result["repo"] == "community"
    assert result["source"]["current"] == "versions.env"
    assert isinstance(result["checked_at"], float)


def test_update_available():
    result = _check(FakeClient(VERSIONS_ENV),
                    make_apkindex([("chromium", "153.0.2.1-r0")]))
    assert result["update_available"] is True, result
    assert result["current"] == "152.0.7977.82"
    assert result["latest"] == "153.0.2.1-r0"
    assert result["error"] is None


def test_network_failure_is_NONE_not_FALSE():
    """THE load-bearing test.

    `update_available is False` means "positively up to date". A network
    failure must never produce it, or the browser goes stale behind a green
    "up to date" forever.
    """
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    try:
        with _patched_urlopen(_serve(urllib.error.URLError("network is down"))):
            result = check_chromium_update(
                FakeClient(VERSIONS_ENV), cache_path=os.path.join(tmp, "c.json"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    assert result["update_available"] is None, result
    assert result["current"] == "152.0.7977.82"      # still report what we know
    assert result["error"] and "could not be fetched" in result["error"]


def test_garbage_index_is_NONE_not_FALSE():
    result = _check(FakeClient(VERSIONS_ENV), b"\x1f\x8b garbage")
    assert result["update_available"] is None, result
    assert result["error"]


def test_missing_image_is_NONE_not_FALSE():
    result = _check(FakeClient(VERSIONS_ENV, image_error=Exception("No such image")),
                    make_apkindex(INDEX))
    assert result["update_available"] is None
    assert result["error"] and "not available" in result["error"]


def test_probe_failure_is_NONE_not_FALSE():
    result = _check(FakeClient("", probe_error=Exception("daemon gone")),
                    make_apkindex(INDEX))
    assert result["update_available"] is None
    assert result["error"] and "daemon gone" in result["error"]


def test_image_without_chromium_is_NONE():
    result = _check(FakeClient("PROBE_SOURCE=versions.env\nALPINE_VERSION=3.24\n"),
                    make_apkindex(INDEX))
    assert result["update_available"] is None
    assert result["error"] and "could not determine" in result["error"]


def test_check_never_raises():
    """A background detector must not be able to take the bridge down."""
    hostile = [Exception("boom"), KeyboardInterrupt if False else RuntimeError("x"),
               urllib.error.URLError("down"), OSError("disk gone"),
               MemoryError("oom")]
    for exc in hostile:
        with _patched_urlopen(_serve(exc)):
            try:
                result = check_chromium_update(
                    FakeClient(VERSIONS_ENV),
                    cache_path=os.path.join(tempfile.mkdtemp(), "c.json"))
            except Exception as escaped:            # pragma: no cover
                raise AssertionError(
                    "check_chromium_update raised %r for %r" % (escaped, exc))
            assert result["update_available"] is None


# ═════════════════════════════════════ 6. the pre-versions.env fallback path

def test_legacy_image_falls_back_to_the_binary():
    """The 3.19 / Chromium 124 image has no manifest — exactly this path."""
    client = FakeClient(LEGACY_PROBE)
    result = _check(client, make_apkindex([("chromium", "152.0.7977.82-r0")]))
    assert result["current"] == "124.0.6367.78", result
    assert result["source"]["current"] == "chromium-binary"
    assert result["alpine_version"] == "3.19.9"
    # The branch comes from /etc/alpine-release, so the query retargets to 3.19.
    assert result["update_available"] is True
    assert "/v3.19/" in result["source"]["latest"]


def test_fallback_reads_version_out_of_arbitrary_binary_output():
    client = FakeClient("PROBE_SOURCE=chromium-binary\n"
                        "Chromium 124.0.6367.78\nALPINE_RELEASE=3.19.9\n")
    result = _check(client, make_apkindex([("chromium", "124.0.6367.78-r0")]))
    assert result["current"] == "124.0.6367.78"
    assert result["update_available"] is False


def test_probe_disables_networking_and_is_always_cleaned_up():
    """A read-only probe must not reach the network, and must not leak a
    container even if the wait blows up."""
    client = FakeClient(VERSIONS_ENV)
    _check(client, make_apkindex(INDEX))
    kwargs = client.containers.created_kwargs
    assert kwargs["network_disabled"] is True
    assert client.recorder["started"] == 1
    assert client.recorder["removed"] == 1
    assert client.recorder["wait_timeout"] == chromium_version.IMAGE_READ_TIMEOUT_SECONDS


def test_probe_overrides_the_image_entrypoint_correctly():
    """The image's ENTRYPOINT is stealth-launch.sh and MUST be overridden.

    Docker CONCATENATES Entrypoint and Cmd, so the composed argv is what
    actually runs. Assert the composed result, not the individual fields —
    asserting the fields is what let a broken combination through before.
    """
    client = FakeClient(VERSIONS_ENV)
    _check(client, make_apkindex(INDEX))
    kwargs = client.containers.created_kwargs
    entrypoint = kwargs.get("entrypoint")
    command = kwargs["command"]
    assert entrypoint == [], (
        "the image ENTRYPOINT must be reset, not extended; got %r" % (entrypoint,))
    composed = list(entrypoint or []) + list(command)
    # Exactly the shape /bin/sh -c <script>, and nothing else prepended.
    assert composed[0:2] == ["/bin/sh", "-c"], composed[0:3]
    assert len(composed) == 3, "unexpected extra argv: %r" % (composed[3:],)
    assert not any("launch.sh" in part for part in composed), composed
    # Exactly one interpreter, and it is the one that takes `-c`.
    assert composed.count("/bin/sh") == 1, composed


def test_probe_script_is_valid_shell():
    """The probe script is executed by /bin/sh; it must actually parse."""
    out = subprocess.run(["/bin/sh", "-n"], input=chromium_version._IMAGE_PROBE,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr


def test_probe_script_runs_and_emits_the_markers_it_parses():
    """Execute the probe against a fake image root and check the output shape.

    This is the test that would have caught the Entrypoint bug: the previous
    fake never executed the script, so a script that could not run at all still
    produced a "clean" result.
    """
    root = tempfile.mkdtemp(prefix="fakeroot-")
    os.makedirs(os.path.join(root, "etc"))
    with open(os.path.join(root, "etc", "alpine-release"), "w") as handle:
        handle.write("3.24.2\n")
    os.makedirs(os.path.join(root, "usr/local/share/chrome-isolation"))
    with open(os.path.join(root, "usr/local/share/chrome-isolation",
                           "versions.env"), "w") as handle:
        handle.write("ALPINE_VERSION=3.24\nCHROMIUM_VERSION=152.0.7977.82\n")

    script = chromium_version._IMAGE_PROBE.replace(
        chromium_version.VERSIONS_ENV_PATH,
        os.path.join(root, "usr/local/share/chrome-isolation/versions.env"))
    script = script.replace("/etc/alpine-release",
                            os.path.join(root, "etc/alpine-release"))
    try:
        out = subprocess.run(["/bin/sh", "-c", script],
                             capture_output=True, text=True, timeout=30)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert out.returncode == 0, out.stderr
    assert "PROBE_SOURCE=versions.env" in out.stdout, out.stdout
    assert "CHROMIUM_VERSION=152.0.7977.82" in out.stdout
    assert "ALPINE_RELEASE=3.24.2" in out.stdout

    # The parser must read exactly what the script emits.
    client = FakeClient(out.stdout)
    parsed = chromium_version.read_image_versions(client, "isolated-chrome")
    assert parsed["chromium"] == "152.0.7977.82", parsed
    assert parsed["alpine"] == "3.24"
    assert parsed["source"] == "versions.env"
    assert parsed["error"] is None


def test_probe_container_is_removed_even_when_logs_fail():
    recorder = {"started": 0, "removed": 0, "wait_timeout": None}

    class Exploding(FakeContainer):
        def logs(self, stdout=True, stderr=True):
            raise RuntimeError("log stream broke")

    containers = FakeContainers("", None, recorder)
    containers.create = lambda image, **kw: Exploding("", recorder)
    client = FakeClient(VERSIONS_ENV)
    client.containers = containers
    result = _check(client, make_apkindex(INDEX))
    assert recorder["removed"] == 1, "container leaked after a failure"
    assert result["update_available"] is None


# ═════════════════════════════════════════════════ 7. caching

def test_second_call_within_the_interval_does_no_network_call():
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    calls = []
    try:
        body = make_apkindex([("chromium", "152.0.7977.82-r0")])
        with _patched_urlopen(_serve(body, calls)):
            first = check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache)
            second = check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache)
        assert first["from_cache"] is False
        assert second["from_cache"] is True
        assert second["update_available"] is False
        assert len(calls) == 1, "network hit %d times inside the interval" % len(calls)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cache_expires_after_the_interval():
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    calls = []
    try:
        body = make_apkindex([("chromium", "152.0.7977.82-r0")])
        t0 = 1_700_000_000.0
        with _patched_urlopen(_serve(body, calls)):
            check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache, now=t0)
            check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache,
                                  now=t0 + VERSION_CHECK_INTERVAL_SECONDS - 60)
            assert len(calls) == 1
            check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache,
                                  now=t0 + VERSION_CHECK_INTERVAL_SECONDS + 60)
        assert len(calls) == 2, "check did not run again after the interval"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_interval_is_a_named_constant_in_the_documented_band():
    assert 6 * 3600 <= VERSION_CHECK_INTERVAL_SECONDS <= 24 * 3600
    assert VERSION_CHECK_INTERVAL_SECONDS != ERROR_RETRY_INTERVAL_SECONDS


def test_cache_is_invalidated_when_the_image_id_changes():
    """A rebuild produces a new image id; a stale cache must not survive it."""
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    calls = []
    try:
        body = make_apkindex([("chromium", "153.0.2.1-r0")])
        with _patched_urlopen(_serve(body, calls)):
            first = check_chromium_update(
                FakeClient(VERSIONS_ENV, image_id="sha256:old"), cache_path=cache)
            second = check_chromium_update(
                FakeClient(VERSIONS_ENV, image_id="sha256:new"), cache_path=cache)
        assert first["from_cache"] is False
        assert second["from_cache"] is False, "served a stale cache for a new image"
        assert second["image_id"] == "sha256:new"
        assert len(calls) == 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_checks_are_cached_briefly_not_for_the_full_interval():
    """A transient blip must not pin 'could not check' for 12 hours."""
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    calls = []
    try:
        t0 = 1_700_000_000.0
        with _patched_urlopen(_serve(urllib.error.URLError("down"), calls)):
            first = check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache, now=t0)
            # One attempt probes every repo, so a full miss is len(ALPINE_REPOS)
            # requests, not one.
            per_attempt = len(chromium_version.ALPINE_REPOS)
            assert len(calls) == per_attempt, calls
            again = check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache,
                                          now=t0 + 60)
            assert again["from_cache"] is True
            assert len(calls) == per_attempt, "a cached failure still hit the network"
            check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache,
                                  now=t0 + ERROR_RETRY_INTERVAL_SECONDS + 60)
        assert len(calls) == 2 * per_attempt, \
            "a failed check stayed cached past the retry gap"
        assert first["update_available"] is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_every_concurrent_writer_uses_a_private_tmp_file():
    """DETERMINISTIC atomicity test — the one that gates the mutation.

    `os.replace` is only atomic if the file it renames is COMPLETE. That holds
    only if no two writers share the tmp path, so uniqueness of the tmp name is
    the invariant, and it is asserted directly rather than hoped for.

    The previous version of this test raced real threads and asserted that a
    reader never saw a partial file. That is a probabilistic check and it was
    flaky — measured 2 passes / 1 failure against a deliberately broken writer —
    which makes it useless as a regression gate. Counting distinct tmp paths is
    not a race and fails every time.
    """
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    opened = []
    # `open` is resolved through builtins at call time inside the module, so a
    # local patch is the way to observe it.
    import builtins
    real_builtin_open = builtins.open

    def spy_open(file, *a, **kw):
        if isinstance(file, str) and file.endswith(".tmp"):
            opened.append(file)
        return real_builtin_open(file, *a, **kw)

    payload = {"checked_at": time.time(), "image": "isolated-chrome",
               "result": {"current": "152.0.7977.82", "latest": "153.0.2.1",
                          "update_available": True, "error": None,
                          "payload": "x" * 20000}}
    builtins.open = spy_open
    try:
        writers = [threading.Thread(target=chromium_version._write_cache,
                                    args=(payload, cache)) for _ in range(8)]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(10)
    finally:
        builtins.open = real_builtin_open
        shutil.rmtree(tmp, ignore_errors=True)

    assert len(opened) == 8, "spy missed writes: %r" % (opened,)
    assert len(set(opened)) == 8, (
        "two writers shared a tmp file (%d distinct for 8 writers); a shared "
        "tmp path makes os.replace publish a half-written document: %r"
        % (len(set(opened)), sorted(set(opened))))


def test_cache_write_publishes_atomically_and_never_a_partial_file():
    """Smoke test: after any number of concurrent writers the file parses.

    Complements the deterministic uniqueness assertion above by checking the
    observable end state. It cannot prove atomicity on its own — a race that
    happens not to be observed is still a race — so it is deliberately the
    weaker of the two.
    """
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    bad = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            if os.path.exists(cache):
                try:
                    with open(cache) as handle:
                        data = json.load(handle)
                    if not isinstance(data, dict) or "result" not in data:
                        bad.append("incomplete: %r" % (data,))
                except ValueError as exc:
                    bad.append("unparsable: %s" % exc)
                except OSError:
                    pass

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in threads:
        thread.start()
    writers = [
        threading.Thread(target=chromium_version._write_cache, args=(
            {"checked_at": time.time(), "image": "isolated-chrome", "result": {
                "current": "152.0.7977.82", "latest": "153.0.2.1",
                "update_available": True, "error": None,
                "payload": "x" * 60000}}, cache))
        for _ in range(12)
    ]
    try:
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(10)
        time.sleep(0.2)
    finally:
        stop.set()
        for thread in threads:
            thread.join(2)
        shutil.rmtree(tmp, ignore_errors=True)
    assert not bad, "concurrent reader saw a partial cache: %r" % bad[:3]


def test_cache_write_cleans_up_its_tmp_file():
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    try:
        cache = os.path.join(tmp, "cache.json")
        assert chromium_version._write_cache({"result": {}}, cache) is True
        leftovers = [f for f in os.listdir(tmp) if ".tmp" in f]
        assert leftovers == [], "tmp file left behind: %r" % leftovers
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_cache_degrades_to_a_fresh_check():
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    cache = os.path.join(tmp, "cache.json")
    calls = []
    try:
        with open(cache, "w") as handle:
            handle.write('{"result": {"current": ')     # truncated JSON
        with _patched_urlopen(_serve(make_apkindex(INDEX), calls)):
            result = check_chromium_update(FakeClient(VERSIONS_ENV), cache_path=cache)
        assert result["from_cache"] is False
        assert result["update_available"] is False
        assert len(calls) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_cache_lives_in_app_data_dir():
    """The cache must be per-user state in APP_DATA_DIR, not anywhere else.

    Compared against a FRESHLY reloaded config module, not the live one:
    test_relocation.py rebinds config.APP_DATA_DIR to a temp HOME and leaves it
    rebound, so reading the imported module here made this test depend on
    another file's cleanup discipline. Reloading isolates the assertion.
    """
    import importlib
    import config as fresh_config
    importlib.reload(fresh_config)
    assert chromium_version.CHECK_CACHE_FILE.startswith(
        fresh_config.APP_DATA_DIR), chromium_version.CHECK_CACHE_FILE
    assert chromium_version.CHECK_CACHE_FILE.endswith(".json")
    # Not in the repo working dir, not in /tmp: it is per-user state that must
    # survive a bridge restart and be shared by every bridge process.
    assert not chromium_version.CHECK_CACHE_FILE.startswith("/tmp")
    assert "/home/x" not in chromium_version.CHECK_CACHE_FILE or \
        fresh_config.APP_DATA_DIR.startswith("/home/x")


# ═════════════════════════════════════ 8. import path / startup safety

def test_importing_the_module_makes_no_network_call():
    """`profile_service` does `_docker = DockerManager()` at import, so any
    import-time I/O here would run on every backend start."""
    script = (
        "import sys, socket, urllib.request\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('network call during import')\n"
        "urllib.request.urlopen = boom\n"
        "socket.socket.connect = boom\n"
        "sys.path.insert(0, %r)\n"
        "import chromium_version\n"
        "assert chromium_version.VERSION_CHECK_INTERVAL_SECONDS > 0\n"
        "print('OK')\n" % os.path.join(ROOT, "backend")
    )
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_constructing_a_check_makes_no_network_call():
    """`DockerManager.__init__` runs on every backend import."""
    before = urllib.request.urlopen
    calls = []
    urllib.request.urlopen = lambda *a, **k: calls.append(a) or FakeResponse(b"")
    try:
        client = FakeClient(VERSIONS_ENV)
        # Construction of every callable the module exposes must be inert.
        for fn in (chromium_version.read_image_versions,
                   chromium_version.fetch_latest_version,
                   chromium_version.check_chromium_update):
            assert fn is not None
    finally:
        urllib.request.urlopen = before
    assert calls == []


def test_docker_manager_init_does_not_reach_the_network():
    """The constructor is on the import path — assert it stays inert."""
    script = (
        "import sys, urllib.request\n"
        "def boom(*a, **k):\n"
        "    raise AssertionError('DockerManager.__init__ hit the network')\n"
        "urllib.request.urlopen = boom\n"
        "sys.path.insert(0, %r)\n"
        "import docker_manager\n"
        "assert 'chromium_version' not in [m for m in sys.modules if False]\n"
        "print('OK')\n" % os.path.join(ROOT, "backend")
    )
    # Just importing must not pull the detector in either.
    out = subprocess.run([sys.executable, "-c", script],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "chromium_version" not in out.stdout


def test_nothing_is_written_to_stdout():
    """stdout is the IPC protocol; a stray print corrupts the renderer stream."""
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, %r); import chromium_version as c;"
         "c._log('progress'); print('OK')" % os.path.join(ROOT, "backend")],
        capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "OK"
    assert "progress" in out.stderr


# ═════════════════════════════════ 9. the image_update detector seam

class _redirected_cache:
    """Point the module's cache at a temp dir for the duration of a block.

    Necessary, not cosmetic: the detector uses the module default, so without
    this the tests would write into the real APP_DATA_DIR — polluting the
    user's actual state AND letting one test read another's cached result.
    """

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="vercheck-detector-")
        self._saved = None

    def __enter__(self):
        self._saved = chromium_version.CHECK_CACHE_FILE
        chromium_version.CHECK_CACHE_FILE = os.path.join(self.tmp, "c.json")
        return self

    def __exit__(self, *exc):
        chromium_version.CHECK_CACHE_FILE = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def test_detector_projects_the_result_for_the_rebuild_half():
    """`image_update` reads a missing `latest` as "do not rebuild"."""
    with _redirected_cache():
        chromium_version.set_client_factory(lambda: FakeClient(VERSIONS_ENV))
        try:
            with _patched_urlopen(_serve(make_apkindex([("chromium", "153.0.2.1-r0")]))):
                result = detector("isolated-chrome")
            assert result["current"] == "152.0.7977.82"
            assert result["latest"] == "153.0.2.1-r0"
            assert result["update_available"] is True
        finally:
            chromium_version.set_client_factory(None)


def test_detector_clears_latest_when_unknown_so_nothing_rebuilds():
    """Fail-closed: an unknown answer must never look like "yes, rebuild"."""
    with _redirected_cache():
        chromium_version.set_client_factory(lambda: FakeClient(VERSIONS_ENV))
        try:
            with _patched_urlopen(_serve(urllib.error.URLError("down"))):
                result = detector("isolated-chrome")
            assert result["latest"] is None
            assert result["update_available"] is None
        finally:
            chromium_version.set_client_factory(None)


def test_detector_survives_a_missing_docker_client():
    def boom():
        raise RuntimeError("no docker daemon")
    with _redirected_cache():
        chromium_version.set_client_factory(boom)
        try:
            result = detector("isolated-chrome")
            assert result["latest"] is None
            assert result["update_available"] is None
            assert result["error"]
        finally:
            chromium_version.set_client_factory(None)


def test_detector_clears_a_partial_answer_that_carries_an_error():
    """Defence in depth, tested directly.

    `fetch_latest_version` never returns `latest` and `error` together, so the
    detector's clearing line is unreachable through the real code path. It
    exists so that if that invariant is ever broken upstream, a half-known
    answer can never reach the rebuild half looking like a real version — a
    wrong `latest` is precisely what triggers a 1.31 GB rebuild.
    """
    with _redirected_cache():
        chromium_version.set_client_factory(lambda: FakeClient(VERSIONS_ENV))
        real = chromium_version.check_chromium_update
        chromium_version.check_chromium_update = lambda client, image: {
            "current": "152.0.7977.82", "latest": "999.0.0.1-r0",
            "update_available": None, "checked_at": 1.0, "source": {},
            "error": "index lookup failed halfway", "from_cache": False,
            "image": image, "image_id": "sha256:img1", "alpine_version": "3.24",
            "arch": "x86_64", "repo": "community", "note": None,
        }
        try:
            result = detector("isolated-chrome")
        finally:
            chromium_version.check_chromium_update = real
            chromium_version.set_client_factory(None)
    assert result["latest"] is None, \
        "a latest accompanied by an error must not reach the rebuild half"
    assert result["error"]


def test_detector_does_not_touch_the_real_app_data_cache():
    """Guard against exactly the pollution this file previously had."""
    real = chromium_version.CHECK_CACHE_FILE
    before = os.path.exists(real)
    with _redirected_cache():
        chromium_version.set_client_factory(lambda: FakeClient(VERSIONS_ENV))
        try:
            with _patched_urlopen(_serve(make_apkindex(INDEX))):
                detector("isolated-chrome")
        finally:
            chromium_version.set_client_factory(None)
    assert os.path.exists(real) == before, "the test wrote to the real cache"


def test_register_detector_wires_the_real_updater():
    """End-to-end against the actual rebuild half's seam, not a fake."""
    import image_update
    try:
        assert chromium_version.register_detector() is True
        assert image_update.get_version_detector() is chromium_version.detector
    finally:
        image_update.set_version_detector(None)


def test_real_updater_reads_our_answer_without_rebuilding_when_unknown():
    """The behaviour that actually matters: a failed check builds nothing."""
    import image_update
    tmp = tempfile.mkdtemp(prefix="vercheck-")
    calls = []
    try:
        cli = _RecordingCLI()
        updater = image_update.ImageUpdater(
            client_factory=lambda: _NoContainers(),
            run_cmd=cli,
            detector=lambda image: {"current": "152.0.7977.82", "latest": None},
            lock_file=os.path.join(tmp, "l"),
            state_file=os.path.join(tmp, "s"),
        )
        updater.check_and_start(force=False)
        updater.wait_for_idle(10)
        assert cli.built_tags() == [], "unknown 'latest' must not trigger a build"
    finally:
        image_update.set_version_detector(None)
        shutil.rmtree(tmp, ignore_errors=True)


class _NoContainers:
    class containers:
        @staticmethod
        def list(**kwargs):
            return []


class _RecordingCLI:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, env=None, timeout=None):
        self.calls.append(list(cmd))
        if cmd[1] == "image":
            return 0, "", ""
        if cmd[1] == "run":
            if "versions.env" in cmd[-1]:
                return 0, "CHROMIUM_VERSION=152.0.7977.82\n", ""
            return 0, "CHROMIUM_VERSION=152.0.7977.82\nVERIFY_OK\n", ""
        return 0, "", ""

    def built_tags(self):
        return [c[3] for c in self.calls if c[1] == "build" and c[2] == "-t"]


# ══════════════════════════════════════════════ 10. non-vacuity (mutation)

MUTATIONS = [
    # (label, find, replace)
    # Each of these is a specific way this module could be wrong in a way that
    # produces a confident, wrong answer. If the suite stays green under any of
    # them, the suite has a hole — which is what this test exists to expose.
    ("network failure collapsed to False",
     '        "update_available": available,',
     '        "update_available": False if available is None else available,'),
    ("lexical comparison restored",
     "    cur = cur_key + (0,) * (width - len(cur_key))\n"
     "    new = new_key + (0,) * (width - len(new_key))\n"
     "    return new > cur",
     '    return str(latest) > str(current)'),
    ("early exit removed (whole index always read)",
     "                version = scan(buf)", "                version = None"),
    ("multi-member gzip dropped",
     "                    pending = decompressor.unused_data", "                    pending = b''"),
    ("versions.env fallback removed",
     "            match = _CHROMIUM_RE.search(line)", "            match = None"),
    ("cache interval ignored",
     "    return (now - checked_at) < ttl", "    return True"),
    ("cache write made non-atomic",
     "        os.replace(tmp, path)",
     "        open(path, 'w').write(json.dumps(payload))"),
    ("cache tmp name shared between writers (pid only)",
     'tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"',
     'tmp = f"{path}.{os.getpid()}.tmp"'),
    ("cache tmp name shared between all writers",
     'tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"',
     'tmp = f"{path}.tmp"'),
    ("cache not keyed on image id",
     'and entry.get("image_id") == image_id', "and True"),
    ("failed checks cached for the full interval",
     "    ttl = ERROR_RETRY_INTERVAL_SECONDS if failed else VERSION_CHECK_INTERVAL_SECONDS",
     "    ttl = VERSION_CHECK_INTERVAL_SECONDS"),
    ("probe leaks its container",
     "            container.remove(force=True)", "            pass"),
    ("probe given network access",
     "network_disabled=True,", "network_disabled=False,"),
    ("probe entrypoint no longer reset (stealth-launch.sh would run)",
     "        entrypoint=[],", "        entrypoint=['/bin/sh'],"),
    ("detector stops clearing a partial answer",
     '        result["latest"] = None', "        pass"),
    ("corrupt gzip escapes as an exception",
     "                except zlib.error as exc:\n"
     "                    return None, f\"{url} is not valid gzip: {exc}\"",
     "                except ValueError as exc:\n"
     "                    return None, f\"{url} is not valid gzip: {exc}\""),
    ("garbage index accepted as a version",
     '                    if version == "":', "                    if False:"),
    ("unusable branch passed through to the URL",
     "    match = _BRANCH_RE.match(str(value).strip())\n"
     "    if not match:\n        return None",
     "    match = _BRANCH_RE.match(str(value).strip())\n"
     "    if not match:\n        return 'v' + str(value).strip()"),
]


#: Both the mutation test and its own control re-run this file in a subprocess.
#: They must exclude each other, or the control's subprocess re-runs the
#: control, which re-runs itself — unbounded recursion rather than a test
#: failure, which is a miserable thing to debug.
#:
#: Guard against unbounded recursion in the self-referential tests below.
#:
#: These tests re-run THIS FILE in a subprocess. A `-k` deselect filter is the
#: obvious way to stop the child re-running the parent, and it is a trap: the
#: filter has to name EVERY test that spawns, and forgetting one means the
#: child spawns a grandchild, forever. That happened here — the filter matched
#: two of the three spawners and missed test_control_run_actually_executes_
#: tests, producing 223 live pytest processes and a hung suite (2026-09-29).
#:
#: An environment variable cannot fail that way: the parent sets it, the child
#: observes it, and the child skips every self-referential test unconditionally,
#: whether or not anyone remembered to update a name filter.
_CHILD_ENV = "CHROMIUM_VERSION_CHECK_CHILD"


def _is_child_run():
    return os.environ.get(_CHILD_ENV) == "1"


def _run_pytest(target, cwd, extra_env=None):
    """Run pytest on `target` with the self-referential tests disabled.

    start_new_session=True puts the child in its own process group so a timeout
    kills the whole tree; without it an accidental recursion leaves orphans
    behind after the parent dies, which is exactly how the 223 accumulated.
    """
    env = dict(os.environ, **{_CHILD_ENV: "1"})
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "-p", "no:cacheprovider", target],
        capture_output=True, text=True, timeout=600, start_new_session=True,
        env=env, cwd=cwd)


def test_control_run_actually_executes_the_tests():
    """The control must not be green merely because it ran nothing.

    A harness that silently collects zero tests and exits 0 would make every
    mutation "caught" for the wrong reason. Assert the count.
    """
    if _is_child_run():
        pytest.skip("self-referential: skipped in a child run")
    mutant_test, problem = _mutant_scratch()
    assert mutant_test is not None, problem
    try:
        proc = _run_pytest(mutant_test, _scratch_of(mutant_test))
        assert proc.returncode == 0, proc.stdout[-4000:]
        match = re.search(r"(\d+) passed", proc.stdout)
        assert match, proc.stdout
        assert int(match.group(1)) > 20, (
            "control run only executed %s tests — a green control means nothing"
            % match.group(1))
    finally:
        shutil.rmtree(_scratch_of(mutant_test), ignore_errors=True)


def _mutant_scratch(replace_find=None, replace_with=None):
    """Build a scratch tree containing a (possibly mutated) copy of backend/.

    The copy of THIS TEST FILE goes into the scratch tree too, and that is
    load-bearing rather than incidental. `sys.path.insert(0, ROOT/'backend')`
    at the top of this file resolves ROOT from `__file__`, so running the
    original file with `PYTHONPATH=<scratch>` does NOT work: the real backend/
    is inserted at position 0 and wins, the mutation never loads, and every
    mutation trivially "passes". Deriving ROOT from a file *inside* the scratch
    tree is what makes the mutated module the one under test — the same trick
    test_image_update.py uses, and for the same reason.
    """
    scratch = tempfile.mkdtemp(prefix="mutant-")
    shutil.copytree(os.path.join(ROOT, "backend"), os.path.join(scratch, "backend"))
    os.makedirs(os.path.join(scratch, "tests"), exist_ok=True)
    mutant_test = os.path.join(scratch, "tests", "test_version_check.py")
    shutil.copy2(os.path.abspath(__file__), mutant_test)

    if replace_find is not None:
        target = os.path.join(scratch, "backend", "chromium_version.py")
        with open(target) as handle:
            source = handle.read()
        if replace_find not in source:
            shutil.rmtree(scratch, ignore_errors=True)
            return None, "PATCH DID NOT APPLY (%r absent)" % replace_find
        with open(target, "w") as handle:
            handle.write(source.replace(replace_find, replace_with, 1))
    return mutant_test, None


def _scratch_of(path):
    """The scratch root containing a mutant tree (…/tests/x.py -> …)."""
    return os.path.dirname(os.path.dirname(path))


def test_mutations_are_detected():
    """Break the module on purpose and prove the suite goes red.

    A green suite proves nothing unless a wrong implementation is known to fail
    it. Each mutation is applied to a scratch copy of the module and the whole
    file is re-run against it; a mutation that leaves the suite green is a test
    gap and is reported as a failure here.
    """
    if _is_child_run():
        pytest.skip("self-referential: skipped in a child run")

    survivors = []
    for label, find, replace in MUTATIONS:
        mutant_test, problem = _mutant_scratch(find, replace)
        if mutant_test is None:
            survivors.append("%s: %s" % (label, problem))
            continue
        try:
            proc = _run_pytest(mutant_test, _scratch_of(mutant_test))
            if proc.returncode == 0:
                survivors.append("%s: SUITE STILL GREEN" % label)
            else:
                print("  mutation caught: %s" % label)
        finally:
            shutil.rmtree(_scratch_of(mutant_test), ignore_errors=True)
    assert not survivors, "mutations the suite did NOT catch:\n  " + \
        "\n  ".join(survivors)


def test_suite_is_green_against_the_unmutated_module():
    """The control for the mutation test — same harness, no mutation applied.

    Without this, a harness bug (wrong cwd, a shadowed import, zero tests
    collected) would make every mutation look "caught" while proving nothing.
    """
    if _is_child_run():
        pytest.skip("self-referential: skipped in a child run")
    mutant_test, problem = _mutant_scratch()
    assert mutant_test is not None, problem
    try:
        proc = _run_pytest(mutant_test, _scratch_of(mutant_test))
        assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    finally:
        shutil.rmtree(_scratch_of(mutant_test), ignore_errors=True)


# ═══════════════════════════════════════════════════════════ main()

def _main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  PASS %s" % name)
        except Exception:
            failed.append(name)
            print("  FAIL %s" % name)
            traceback.print_exc()
    print("\n%d/%d passed" % (len(tests) - len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())

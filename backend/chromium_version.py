"""
Chromium version DETECTION — the read-only half of auto-update.

Chromium is not installed on this host. It lives inside the `isolated-chrome`
Docker image, built from the repo's own Dockerfile (`FROM alpine:${VER}`,
`apk add chromium`). So "updating Chromium" means rebuilding a 1.31 GB image
(a cached build is ~31 s; a cold one is minutes and pulls ~300 MB).

This module answers ONE question, cheaply and honestly:

    "what Chromium is installed, what is the newest Chromium published for the
     image's own Alpine branch, and are they the same?"

It deliberately does NOT rebuild, promote, tag or remove anything. The
rebuild/apply half lives in `image_update.py`. This module is the detector
that half is designed to consume (see `register_detector` below).

WHY THE CHECK IS CHEAP ENOUGH TO RUN AT STARTUP
-----------------------------------------------
    * The network read is a *streaming* inflate of the official Alpine
      APKINDEX that stops the instant the `chromium` stanza is seen. Measured
      against v3.24/community: 0.20 MB transferred, 0.54 s. The full index is
      2.5 MB, so early exit is a real 12x saving, not a theoretical one.
    * The result is cached on disk in APP_DATA_DIR, so the network call
      happens at most once per `VERSION_CHECK_INTERVAL_SECONDS` (12 h), not on
      every app start.
    * The cache is keyed on the IMAGE ID, so a rebuild invalidates it without
      anyone having to remember to clear a file.
    * Every network read has a hard deadline. A hung request cannot wedge the
      caller.
    * NOTHING here runs at import time or in `DockerManager.__init__`, both of
      which execute on every backend import. Constructing the check costs a
      dict.

THE DETECTION SOURCE, AND WHY IT IS THIS ONE
--------------------------------------------
Chosen: the signed APKINDEX published by Alpine's own CDN, streamed and
early-exited.

    https://dl-cdn.alpinelinux.org/alpine/v<BRANCH>/<repo>/<arch>/APKINDEX.tar.gz

Rejected: the HTML page at pkgs.alpinelinux.org, which is what was used
manually before. Measured, not assumed:

  * It has NO JSON API. `/json/packages`, `/api/packages`, `/api/v1/packages`
    and `package/....json` all return 404. There is no structured endpoint to
    fall back to.
  * An unknown branch returns HTTP **500**, not 404. A scraper that treats
    "not 200" as "no update" would silently mis-report a typo'd or
    not-yet-released branch as "nothing to do".
  * The version only exists as a version-dependent HTML fragment
    (`<strong>` inside a `<td>` after a `Version` header). Any template change
    turns into a parse failure, and a parse failure that is swallowed is how a
    browser stays stale behind a green UI.

APKINDEX is a documented, line-oriented format consumed by `apk` itself —
`P:` is the package name, `V:` the version — so a layout change upstream is a
hard parse failure we can detect, not a silent miss. It is also the *same*
index `apk add chromium` will consult during a rebuild, so a positive
detection corresponds to a real version that a rebuild would actually install.
That correspondence is the whole point; a UI page could disagree with it.

Honest limits of this source:
  * The `.tar.gz` carries a `.SIGN.RSA...` signature member. We do NOT verify
    it — that needs Alpine's public key, which is not vendored here. Transport
    is HTTPS to the official CDN, which is what the `apk` client itself
    relies on for the index. This is a freshness hint, not a trust decision;
    the actual package install is what is signature-checked.
  * The index reflects what the CDN has published. A build that starts while
    Alpine is mid-release can legitimately see the previous version.

REPO SELECTION
--------------
`chromium` lives in `community` on every branch checked here (3.19 and 3.24).
It is not hardcoded: `community` is tried first, then `main`, and the repo the
answer came from is reported. If upstream ever moves the package, this keeps
working. A branch that has neither simply reports "could not check".

BRANCH END-OF-LIFE — WHY THE CHECK ABOVE IS NOT ENOUGH
------------------------------------------------------
The comparison above is BRANCH-RELATIVE, and that is a real hole rather than a
technicality. Measured against the shipped image: `124.0.6367.78` installed,
`124.0.6367.78-r0` published for its own branch, so `update_available` is
`False` — and would stay `False` forever. The image's branch is Alpine 3.19,
which is EOL and frozen: its APKINDEX will never gain another Chromium, so
"up to date within this branch" and "up to date" are the same string with
completely different meanings, and only the first one is true.

So this module also answers a second, separate question: is the image's Alpine
branch still the current stable one?

    https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/<arch>/latest-releases.yaml

Verified, not assumed:
  * The bare directory listing `.../alpine/latest-stable/` returns 200 and 480
    bytes, but it lists only `community/`, `main/`, `releases/` — the branch is
    a symlink the CDN resolves for us, so the listing does NOT name it. It is
    useless for this question and would have been the obvious wrong choice.
  * `.../alpine/latest-stable/releases/latest-releases.yaml` (arch-less) is
    404. The file is per-arch.
  * The per-arch file exists and carries the answer as a plain `branch:`
    field. Measured over 7 requests on x86_64: 3,320 bytes, median 0.99 s, and
    1.2–4.2 KB across every architecture Alpine ships. Against the 0.20 MB /
    0.54 s streaming APKINDEX read this is ~1.6% of the bytes, so it is
    genuinely negligible and is cached on exactly the same schedule.

The file is a YAML list of release stanzas whose `branch:` is constant across
the whole document. Every `branch:` line is read and they must all agree: a
disagreement is a real upstream change, and guessing which one is current is
the failure mode this module exists to avoid.

THE RETURN CONTRACT
-------------------
`check_chromium_update()` returns a dict, and NEVER raises. It always has
these keys:

    current           str | None   version installed in the image
    latest            str | None   newest version published for its branch
    update_available  bool | None  True / False / **None = UNKNOWN**
    branch_eol        bool | None  True / False / **None = UNKNOWN** (see below)
    stable_branch     str | None   current stable branch, e.g. "v3.24"
    stable_version    str | None   stable branch's point release, e.g. "3.24.2"
    branch_eol_error  str | None   why `branch_eol` is UNKNOWN
    checked_at        float | None unix epoch of the check
    source            dict         provenance, per value
    error             str | None   why the answer is UNKNOWN
    from_cache        bool         True if served without a network call
    image             str          image name that was inspected
    image_id          str | None   image id the result is pinned to
    alpine_version    str | None   branch the image was built from
    arch              str | None   Alpine arch that was queried
    repo              str | None   repo the answer came from
    note              str | None   human-readable caveat, when it matters

`update_available` is deliberately tri-state and this is the single most
important property of the module:

    False  -> positively determined: the image is current.
    True   -> positively determined: a newer version is published.
    None   -> NOT DETERMINED. No image, no network, an unparsable index, a
              branch that does not exist. `error` says which.

A caller MUST NOT collapse None into False. "Could not check" and "up to date"
are opposite states: conflating them yields a permanently stale browser
displayed as healthy, which is the single worst outcome this feature has.
`image_update` already fails closed on this (a detector that cannot tell
reports no update and builds nothing); this module is the reason that
fail-closed path is reachable at all.

`branch_eol` follows the SAME tri-state discipline and is deliberately a
SEPARATE field rather than being folded into `update_available`:

    True   -> the image's branch is positively known NOT to be the current
              stable one. Frozen: no further Chromium will ever be published
              for it, and no security fixes reach it.
    False  -> the image's branch IS the current stable one.
    None   -> NOT DETERMINED: no image, no network, no arch to build the URL
              from, an unparsable file, or branches that disagree. The reason
              is in `branch_eol_error`.

Why it is NOT folded in. Flipping `update_available` to True on an EOL branch
would ask the rebuild half to rebuild the user's PINNED OLD BRANCH — which
installs the newest Chromium that branch will ever publish and leaves it just
as unpatched, at the cost of 1.31 GB. Worse, it is a silent side effect of a
"Chromium update": changing the user's base OS is a different decision from
changing a package inside a branch, and it is not this module's decision to
make. The caller has to be told the fact and choose.

`branch_eol` is strictly ADVISORY and strictly NON-BLOCKING. It is computed
after the Chromium comparison and can never change it: if the EOL lookup
fails, the comparison result is exactly what it would have been, and
`update_available` is unaffected. A failed EOL lookup is also deliberately
NOT recorded in `error` — `error` means "the Chromium answer is unknown", and
overloading it would make `image_update` throw away a perfectly good `latest`
and skip a legitimate update.

A caller that wants to act on the answer should branch on `update_available is
True` only, and surface `error` verbatim when it is None. `branch_eol` is
reported alongside for the caller to act on *separately*.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from typing import Any, Dict, Optional, Tuple

from config import APP_DATA_DIR, DOCKER_IMAGE_NAME

# ---------------------------------------------------------------------------
# Locations & tuning
# ---------------------------------------------------------------------------

#: Disk cache. Lives in APP_DATA_DIR so every bridge process for this user
#: shares it and it survives an Electron-driven bridge restart. Written
#: atomically (tmp file in the same directory + os.replace) because the bridge
#: can be spawned more than once and a concurrent reader must never observe a
#: half-written file.
CHECK_CACHE_FILE = os.path.join(APP_DATA_DIR, "chromium-version-check.json")

#: How long a completed check stays valid. 12 h sits in the 6-24 h band the
#: brief asked for: short enough that a security-relevant browser update is
#: picked up the same day, long enough that the network cost is invisible.
VERSION_CHECK_INTERVAL_SECONDS = 12 * 3600

#: A FAILED check is cached far more briefly than a successful one. Pinning a
#: transient network blip to "could not check" for 12 h would be as bad as a
#: false negative, and the whole point of caching is to avoid retrying on every
#: app start — 30 min achieves that without hiding an outage for half a day.
ERROR_RETRY_INTERVAL_SECONDS = 30 * 60

#: Hard deadline for the index read. urllib's timeout is per-socket-operation
#: and does not bound total elapsed time, so the deadline is enforced
#: explicitly against a monotonic clock. A hung mirror cannot wedge a caller.
NETWORK_TIMEOUT_SECONDS = 15.0

#: Reading versions.env out of the image is a `docker run` round trip: create,
#: start, wait, logs, remove. Local socket, but still a container lifecycle.
IMAGE_READ_TIMEOUT_SECONDS = 45.0

#: Refuse to buffer more than this from the index endpoint. The real files are
#: 2.5 MB (community) and 0.5 MB (main); this is a backstop against a
#: misbehaving or hostile endpoint, not a size the design depends on.
MAX_INDEX_BYTES = 32 * 1024 * 1024

#: In-image manifest written by the Dockerfile at build time.
VERSIONS_ENV_PATH = "/usr/local/share/chrome-isolation/versions.env"

#: Alpine CDN base. The branch and arch are validated and appended, never
#: interpolated raw.
ALPINE_CDN_BASE = "https://dl-cdn.alpinelinux.org/alpine"

#: Where Alpine publishes the CURRENT STABLE branch. `latest-stable` is a
#: symlink the CDN resolves, so the directory listing at that path does NOT
#: name the branch — measured, it returns only community/ main/ releases/.
#: The release manifest beneath it does, in a plain `branch:` field, and is
#: 1.2-4.2 KB. It is per-arch; the arch-less form is a measured 404.
#:
#: The arch goes through `_ARCH_MAP` and is re-validated against it here, so
#: it is still a known-constant value at the point it becomes a path segment.
ALPINE_STABLE_RELEASES_URL = (
    ALPINE_CDN_BASE + "/latest-stable/releases/{arch}/latest-releases.yaml"
)

#: The stable-release manifest is 1.2-4.2 KB in practice. This is a backstop
#: against a misbehaving endpoint, several orders of magnitude above the real
#: size, not a budget the design spends.
MAX_STABLE_BYTES = 1024 * 1024

#: Every `branch:` in the manifest must agree, so a document naming two is
#: rejected rather than guessed at. Indented, because the real file is a YAML
#: sequence and its keys sit two spaces in — measured on the live endpoint, not
#: assumed, and an unanchored `^` here would silently match nothing.
_STABLE_BRANCH_RE = re.compile(r"^[ \t]*branch:[ \t]*(\S+)[ \t]*$", re.MULTILINE)
_STABLE_VERSION_RE = re.compile(r"^[ \t]*version:[ \t]*(\S+)[ \t]*$", re.MULTILINE)

#: Repositories to try, in order. `chromium` is in `community` on every branch
#: checked (3.19, 3.24); `main` is a fallback, not a guess about the future.
ALPINE_REPOS = ("community", "main")

_USER_AGENT = "chrome-isolation-manager/1.1 (chromium version check)"

# Docker architecture -> Alpine architecture.
_ARCH_MAP = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "386": "x86",
    "armv7l": "armv7",
}

# "3.24", "v3.24" -> "v3.24". Anything else is rejected: this string becomes a
# URL path segment, and a branch we do not understand is a branch we must not
# guess at.
_BRANCH_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.\d+)*$")

# The version text a browser prints, e.g. "Chromium 152.0.7977.82".
_CHROMIUM_RE = re.compile(r"Chromium\s+(\d+(?:\.\d+)*)", re.IGNORECASE)


def _log(message: str) -> None:
    """Progress/diagnostics — STDERR ONLY.

    stdout is the newline-delimited JSON IPC protocol owned by bridge.py. A
    stray print from here would corrupt the renderer stream. This is the same
    trap host_share.py documents.
    """
    try:
        sys.stderr.write(f"[chromium-version] {message}\n")
        sys.stderr.flush()
    except Exception:  # stderr closed during shutdown; never fatal
        pass


# ---------------------------------------------------------------------------
# Version string handling
# ---------------------------------------------------------------------------

def normalise_branch(value: Any) -> Optional[str]:
    """`3.24` / `v3.24` / `3.24.1` -> `v3.24`. None if unusable.

    Alpine's CDN path uses the MAJOR.MINOR series (`v3.24`), while
    /etc/alpine-release and the image manifest report a full version. Truncating
    here is what keeps a base-image point bump from silently querying a branch
    that does not exist.
    """
    if value is None:
        return None
    match = _BRANCH_RE.match(str(value).strip())
    if not match:
        return None
    return f"v{match.group(1)}.{match.group(2)}"


def alpine_arch(docker_arch: Any) -> Optional[str]:
    """Docker platform string -> Alpine arch directory name."""
    if not docker_arch:
        return None
    return _ARCH_MAP.get(str(docker_arch).strip().lower())


def version_key(value: Any) -> Optional[Tuple[int, ...]]:
    """Sortable key for a browser version, or None if unparsable.

    Two things make the naive `str` comparison wrong here:

      * LEXICAL. '99.0.1.2' > '152.0.7977.82' as strings, but is older. The
        predecessor image really was 124 vs 152, and a 3.x -> 15x transition
        would break any lexical compare outright.
      * The Alpine package version carries an `-rN` build revision
        (`152.0.7977.82-r0`) that the manifest's `CHROMIUM_VERSION` does not.
        Comparing the raw strings would report an update available for an
        image that is already exactly current.

    So: strip the packaging revision, then compare the dotted numeric
    components as integers. Missing trailing components compare as 0, so
    '152.0.7977' == '152.0.7977.0'.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Drop the Alpine packaging revision: -r0, -r12, -r1.
    text = re.sub(r"-r\d+$", "", text)
    # Take the leading dotted-numeric run and ignore any suffix.
    match = re.match(r"^(\d+(?:\.\d+)*)", text)
    if not match:
        return None
    parts = match.group(1).split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_update_available(current: Any, latest: Any) -> Optional[bool]:
    """Compare two versions. None when either side is unparsable.

    `latest` older than `current` is NOT an update: the image can legitimately
    be ahead of the published index (built from a branch whose release has not
    propagated to this mirror yet). That is reported as no update, with a note,
    rather than as a positive that would trigger a pointless downgrade rebuild.
    """
    cur_key = version_key(current)
    new_key = version_key(latest)
    if cur_key is None or new_key is None:
        return None
    width = max(len(cur_key), len(new_key))
    cur = cur_key + (0,) * (width - len(cur_key))
    new = new_key + (0,) * (width - len(new_key))
    return new > cur


# ---------------------------------------------------------------------------
# Reading the version INSTALLED in the image
# ---------------------------------------------------------------------------

# Run as the image's own non-root `chrome` user (the Dockerfile sets USER
# chrome, and build time already proves `chromium-browser --version` runs as
# that user). Reads the manifest when present; otherwise falls back to asking
# the binary, which is the ONLY option for images built before the manifest
# existed — including the 3.19 / Chromium 124 image this feature exists for.
#
# `/etc/alpine-release` is read unconditionally: it is the independent,
# always-present source for the branch, so a manifest-less image still reports
# which Alpine series to query.
_IMAGE_PROBE = f"""
M="{VERSIONS_ENV_PATH}"
if [ -r "$M" ]; then
    echo "PROBE_SOURCE=versions.env"
    cat "$M"
else
    echo "PROBE_SOURCE=chromium-binary"
    chromium-browser --version 2>&1 || chromium --version 2>&1 || true
fi
echo "ALPINE_RELEASE=$(cat /etc/alpine-release 2>/dev/null || true)"
"""


def run_probe_in_image(client: Any, image: str,
                       timeout: float = IMAGE_READ_TIMEOUT_SECONDS) -> str:
    """Run `_IMAGE_PROBE` in a throwaway container and return its stdout.

    Uses create/start/wait rather than `containers.run()` on purpose:

      * `run()` waits on `container.wait()` with no timeout, so a wedged
        container would block the caller indefinitely. Here the wait is bounded
        and the container is force-removed in `finally` either way.
      * `run()` also re-pulls the image if it is missing. A version *check*
        must never cause a 1.31 GB network pull as a side effect.

    Networking is disabled and nothing is mounted: the probe only reads two
    files, and it must not be able to reach the network.
    """
    # The image sets ENTRYPOINT ["/home/chrome/scripts/stealth-launch.sh"], so
    # it MUST be overridden. `entrypoint` and `command` are CONCATENATED by
    # Docker, so the correct reset is an empty entrypoint with the whole
    # invocation in the command: that yields exactly
    #     /bin/sh -c <script>
    # Setting entrypoint=["/bin/sh"] alongside command=["/bin/sh","-c",...]
    # instead produces `/bin/sh /bin/sh -c <script>`, which sh reads as
    # $0=/bin/sh, $1=-c and then tries to run a file literally named "-c" —
    # it fails, and the probe silently reports "no version". Found by running
    # this against the real image; the fakes never exercised the composition.
    container = client.containers.create(
        image,
        command=["/bin/sh", "-c", _IMAGE_PROBE],
        entrypoint=[],
        network_disabled=True,
        tty=False,
    )
    try:
        container.start()
        container.wait(timeout=timeout)
        raw = container.logs(stdout=True, stderr=True)
    finally:
        # Best effort: a leftover stopped container is untidy, not dangerous,
        # and must never turn a readable error into a raised one.
        try:
            container.remove(force=True)
        except Exception as exc:
            _log(f"could not remove probe container: {exc}")
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw or ""


def read_image_versions(client: Any, image: str,
                        timeout: float = IMAGE_READ_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Version actually installed in `image`.

    Returns `{"chromium": str|None, "alpine": str|None, "arch": str|None,
    "source": str, "error": str|None}`. Never raises.

    The manifest is preferred; `chromium-browser --version` is the fallback for
    images predating it. Nothing is hardcoded, so a 2.5-year-old image and a
    fresh one are both read correctly.
    """
    out: Dict[str, Any] = {
        "chromium": None, "alpine": None, "arch": None,
        "source": None, "error": None,
    }
    try:
        raw = run_probe_in_image(client, image, timeout=timeout)
    except Exception as exc:
        out["error"] = f"could not read versions from image: {exc}"
        return out

    lines = raw.splitlines()
    fields: Dict[str, str] = {}
    for line in lines:
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()

    out["source"] = fields.get("PROBE_SOURCE") or None

    # Alpine branch: prefer the manifest's own record, fall back to the
    # always-present /etc/alpine-release.
    out["alpine"] = (fields.get("ALPINE_VERSION")
                     or fields.get("ALPINE_RELEASE") or None)

    chromium = fields.get("CHROMIUM_VERSION")
    if not chromium:
        # Manifest absent (or lacked the key): ask the binary. This is the path
        # an old image takes, and the only reason it is here.
        #
        # Scan the RAW LINES, not the parsed `=` fields. `chromium-browser
        # --version` prints "Chromium 124.0.6367.78" — no `=` anywhere on the
        # line — so a fields-only scan silently discards the one line that
        # carries the answer, and the fallback path never fires at all.
        for line in lines:
            match = _CHROMIUM_RE.search(line)
            if match:
                chromium = match.group(1)
                break
        if chromium and out["source"] is None:
            out["source"] = "chromium-binary"

    if not chromium:
        out["error"] = ("could not determine the Chromium version installed in "
                        f"{image} (neither {VERSIONS_ENV_PATH} nor "
                        "'chromium-browser --version' yielded one)")
        return out

    out["chromium"] = chromium
    return out


def _image_identity(client: Any, image: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(image_id, docker_arch, error) from a single cheap inspect call.

    This is deliberately much cheaper than the container probe — it is an
    inspect, not a lifecycle — because it runs on EVERY call, including cache
    hits, to decide whether the cached answer still describes the image the
    user is actually running.
    """
    try:
        img = client.images.get(image)
    except Exception as exc:
        return None, None, f"image {image!r} is not available: {exc}"
    attrs = getattr(img, "attrs", None) or {}
    return (getattr(img, "id", None) or attrs.get("Id"),
            attrs.get("Architecture") or (attrs.get("Config") or {}).get("Architecture"),
            None)


# ---------------------------------------------------------------------------
# Discovering the LATEST published version
# ---------------------------------------------------------------------------

def _index_url(branch: str, repo: str, arch: str) -> str:
    return f"{ALPINE_CDN_BASE}/{branch}/{repo}/{arch}/APKINDEX.tar.gz"


def _iter_index_versions(url: str, package: str,
                         deadline_seconds: float) -> Tuple[Optional[str], Optional[str]]:
    """Stream the APKINDEX and return the first `V:` for `package`.

    Returns (version, error). Exactly one is set.

    Two real hazards, both found by measurement rather than by reading docs:

      * The file is a MULTI-MEMBER gzip. Member 1 is 2048 bytes of PaxHeaders
        and member 2 is the ~10 MB payload. A plain `zlib.decompressobj(...).decompress(all)`
        returns the 2048 bytes, sets `eof=True`, and the payload sits in
        `unused_data` — i.e. the obvious implementation reports "no such
        package" on a perfectly good index. `unused_data` is therefore fed
        into a fresh decompressor.
      * `urllib`'s timeout is per socket operation, so a server dribbling one
        byte per interval would never trip it. Elapsed time is checked against
        a monotonic clock on every chunk instead.

    Buffers are trimmed to a small tail between scans so peak memory stays at
    one chunk rather than the whole index.
    """
    marker = ("\nP:%s\n" % package).encode()
    tail_keep = len(marker)
    started = time.monotonic()
    transferred = 0
    buf = b""

    def scan(blob: bytes) -> Optional[str]:
        idx = blob.find(marker)
        if idx < 0:
            return None
        stanza = blob[idx + 1:].split(b"\n\n", 1)[0]
        for line in stanza.split(b"\n"):
            if line.startswith(b"V:"):
                return line[2:].decode("utf-8", "replace").strip() or None
        # Stanza present but malformed — a real signal, not "not found".
        return ""

    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        response = urllib.request.urlopen(request, timeout=deadline_seconds)
    except urllib.error.HTTPError as exc:
        return None, f"{url} returned HTTP {exc.code}"
    except urllib.error.URLError as exc:
        return None, f"{url} could not be fetched: {exc.reason}"
    except Exception as exc:
        return None, f"{url} could not be fetched: {exc}"

    try:
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        while True:
            if time.monotonic() - started > deadline_seconds:
                return None, (f"{url} exceeded the {deadline_seconds:g}s deadline "
                              f"after {transferred} bytes")
            try:
                chunk = response.read(65536)
            except Exception as exc:
                return None, f"{url} read failed: {exc}"
            if not chunk:
                break
            transferred += len(chunk)
            if transferred > MAX_INDEX_BYTES:
                return None, f"{url} exceeded the {MAX_INDEX_BYTES} byte cap"

            pending = chunk
            while pending:
                # A truncated or corrupt body raises zlib.error here. It must
                # surface as an `error`, not escape the function — a detector
                # that raises is indistinguishable from one that found an
                # update as far as the rebuild half is concerned.
                try:
                    buf += decompressor.decompress(pending)
                except zlib.error as exc:
                    return None, f"{url} is not valid gzip: {exc}"
                if decompressor.eof:
                    # Next gzip member (see docstring): hand the remainder to a
                    # fresh decompressor instead of dropping it.
                    pending = decompressor.unused_data
                    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
                else:
                    pending = b""
                version = scan(buf)
                if version is not None:
                    if version == "":
                        return None, f"{url} has a malformed {package} stanza"
                    return version, None
                if len(buf) > 8192:
                    buf = buf[-tail_keep:]
    finally:
        try:
            response.close()
        except Exception:
            pass

    return None, f"{package} is not present in {url}"


def fetch_latest_version(branch: Optional[str], arch: Optional[str],
                         package: str = "chromium",
                         timeout: float = NETWORK_TIMEOUT_SECONDS) -> Dict[str, Any]:
    """Newest version of `package` published for `branch`/`arch`.

    Returns `{"latest": str|None, "branch": str|None, "arch": str|None,
    "repo": str|None, "source": str|None, "error": str|None}`. Never raises.

    Repos are tried in order and the first hit wins; the repo used is reported
    so a caller can see which index answered. If every repo errors, the FIRST
    error is surfaced — "could not check", never a fabricated "up to date".
    """
    out: Dict[str, Any] = {
        "latest": None, "branch": branch, "arch": arch, "repo": None,
        "source": None, "error": None,
    }
    if not branch:
        out["error"] = ("the image does not report a usable Alpine version, so "
                        "there is no branch to query; cannot determine whether "
                        "an update exists")
        return out
    if not arch:
        out["error"] = ("could not determine the image architecture, so there is "
                        "no Alpine index to query; cannot determine whether an "
                        "update exists")
        return out

    first_error = None
    for repo in ALPINE_REPOS:
        url = _index_url(branch, repo, arch)
        version, error = _iter_index_versions(url, package, timeout)
        if error is None and version:
            out["latest"] = version
            out["repo"] = repo
            out["source"] = url
            return out
        if first_error is None:
            first_error = error
        _log(f"index lookup for {package} in {branch}/{repo}/{arch} did not answer: {error}")

    out["error"] = (f"could not determine the latest {package} for {branch}/{arch}: "
                    f"{first_error or 'no repository contained it'}")
    return out


# ---------------------------------------------------------------------------
# Discovering the current STABLE branch (is the image's branch EOL?)
# ---------------------------------------------------------------------------

def _stable_releases_url(arch: str) -> str:
    """The per-arch stable-release manifest URL, or None for an unknown arch.

    `arch` is checked against the values `_ARCH_MAP` can produce rather than
    formatted blindly: this string becomes a URL path segment, and the arch
    never originates from the image itself (it comes from `docker inspect`'s
    `Architecture`, funnelled through the map), so an unmapped value means
    "we do not recognise this platform", not "try it and see".
    """
    if arch not in _ARCH_MAP.values():
        return None
    return ALPINE_STABLE_RELEASES_URL.format(arch=arch)


def fetch_stable_branch(arch: Optional[str],
                        timeout: float = NETWORK_TIMEOUT_SECONDS
                        ) -> Dict[str, Any]:
    """Which Alpine branch is currently stable, and at what point release.

    Returns `{"stable_branch": str|None, "stable_version": str|None,
    "source": str|None, "error": str|None}`. Never raises.

    This is what makes `branch_eol` answerable. It is deliberately a SEPARATE,
    tiny request rather than a flag on the APKINDEX read: the index lookup
    early-exits as soon as the `chromium` stanza appears, so a branch that is
    merely OLD looks identical to a current one, and there is nothing in the
    index that says which branch Alpine considers current.

    Cost, measured over 7 requests (x86_64): 3,320 bytes, median 0.99 s
    against the 0.20 MB / 0.54 s streaming index read — about 1.6% of the
    bytes. Cached on the same 12 h schedule as the index, so the amortised
    cost is a rounding error.

    Reading is a single bounded `read()` under an explicit monotonic deadline:
    `urllib`'s timeout is per socket operation and does not bound elapsed
    time, exactly as in `_iter_index_versions`.
    """
    out: Dict[str, Any] = {
        "stable_branch": None, "stable_version": None,
        "source": None, "error": None,
    }
    if not arch:
        out["error"] = ("could not determine the image architecture, so there "
                        "is no stable-release manifest to query; the branch's "
                        "support status cannot be determined")
        return out
    url = _stable_releases_url(arch)
    if not url:
        out["error"] = (f"{arch!r} is not an architecture this module queries, "
                        "so the branch's support status cannot be determined")
        return out

    started = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        out["error"] = f"{url} returned HTTP {exc.code}"
        return out
    except urllib.error.URLError as exc:
        out["error"] = f"{url} could not be fetched: {exc.reason}"
        return out
    except Exception as exc:
        out["error"] = f"{url} could not be fetched: {exc}"
        return out

    try:
        # Chunked, not one big `read()`. urllib's timeout is per socket
        # operation, so a server that dribbles one byte per interval inside a
        # single read() would never trip it — the deadline is only meaningful if
        # it is checked BETWEEN reads, which is exactly what `_iter_index_versions`
        # does for the same reason.
        chunks = []
        while True:
            if time.monotonic() - started > timeout:
                out["error"] = (f"{url} exceeded the {timeout:g}s deadline "
                                f"after {sum(len(c) for c in chunks)} bytes")
                return out
            chunk = response.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > MAX_STABLE_BYTES:
                out["error"] = f"{url} exceeded the {MAX_STABLE_BYTES} byte cap"
                return out
    except Exception as exc:
        # A read that fails is "could not determine", never a guess.
        out["error"] = f"{url} read failed: {exc}"
        return out
    finally:
        try:
            response.close()
        except Exception:
            pass

    body = b"".join(chunks)
    # The loop above already checks the cap per read, but a hostile or broken
    # `read()` may hand back more than was asked for. This makes the bound
    # unconditional rather than a convention the endpoint is trusted to honour.
    if len(body) > MAX_STABLE_BYTES:
        out["error"] = f"{url} exceeded the {MAX_STABLE_BYTES} byte cap"
        return out

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        out["error"] = f"{url} is not valid UTF-8: {exc}"
        return out

    branches = set(_STABLE_BRANCH_RE.findall(text))
    if not branches:
        out["error"] = f"{url} contains no branch: field, so the current stable branch is undetermined"
        return out
    if len(branches) > 1:
        # The manifest has one branch per document and they are all the same.
        # A disagreement means upstream changed shape, and picking one would be
        # a coin toss presented as a fact.
        out["error"] = (f"{url} names several branches ({', '.join(sorted(branches))}); "
                        "the current stable branch is undetermined")
        return out

    stable = normalise_branch(branches.pop())
    if not stable:
        out["error"] = (f"{url} named {branches or 'an unusable'} as the stable "
                        "branch, which is not a branch we understand")
        return out

    versions = set(_STABLE_VERSION_RE.findall(text))
    out["stable_branch"] = stable
    # A point release is a nicety, not the answer. If the file somehow carries
    # several, report none rather than an arbitrary one.
    out["stable_version"] = next(iter(versions)) if len(versions) == 1 else None
    out["source"] = url
    return out


def is_branch_eol(branch: Optional[str],
                  stable_branch: Optional[str]) -> Optional[bool]:
    """Is `branch` known to be past its end of life? None when undetermined.

    The same tri-state discipline as `is_update_available`, and for the same
    reason: "could not check" and "still supported" are opposite states.

        True   -> positively known not to be the current stable branch.
        False  -> it IS the current stable branch.
        None   -> either side is missing or unusable, so nothing is claimed.

    The comparison is on the normalised MAJOR.MINOR series, so `3.19.9` and
    `v3.19` are the same branch and a point bump is not mistaken for a branch
    change.

    BOTH sides are validated before they are compared. A `stable_branch` that
    is not a branch we understand yields None rather than being compared as
    unequal: `v3.24 != None` is `True`, so an unvalidated right-hand side
    would report every image as EOL the moment the field format changed
    upstream. "Definitely different" and "could not be compared" are different
    answers, and only the first is `True`.
    """
    if not branch or not stable_branch:
        return None
    current = normalise_branch(branch)
    stable = normalise_branch(stable_branch)
    if not current or not stable:
        return None
    return current != stable


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

def _read_cache(path: str = CHECK_CACHE_FILE) -> Optional[dict]:
    """Load a cached result, or None if absent/unusable. Never raises."""
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        # A corrupt or half-written cache must degrade to "no cache", never to a
        # crash on a path the app start depends on.
        _log(f"ignoring unreadable cache {path}: {exc}")
        return None
    return data if isinstance(data, dict) else None


def _write_cache(payload: dict, path: str = CHECK_CACHE_FILE) -> bool:
    """Write the cache atomically. Returns True on success.

    The tmp name must be unique per WRITER, not per process. A fixed name
    (`cache.json.tmp`) is a real race: two bridge processes — Electron restarts
    the bridge on crash — could both open it, interleave their writes, and have
    the first `os.replace` publish a file the other is still appending to, so a
    reader is served truncated JSON. Using the PID alone does NOT fix this: all
    threads in one process share a PID and would collide exactly the same way.
    PID *and* thread id give every writer a private file, and `os.replace` is
    atomic within a filesystem, so a concurrent reader sees one complete
    version or the other, never a partial one.
    """
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError as exc:
        _log(f"could not write cache {path}: {exc}")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def _cache_is_fresh(entry: dict, now: float) -> bool:
    """True when a cached entry is still inside its interval.

    A FAILED entry uses the much shorter ERROR_RETRY_INTERVAL_SECONDS so a
    transient outage is not cached for half a day.

    A PARTIAL failure — the Chromium answer is good but the branch-support
    lookup failed — is treated the same way, for the same reason. Pinning
    `branch_eol: None` ("we could not determine whether your base OS is
    supported") for 12 h would hide exactly the condition this field exists to
    surface, and doing so on a 3 KB endpoint that is merely flaky is not a
    defensible trade. The retry is only expensive while that endpoint is
    actually broken, which is transient by nature.
    """
    try:
        checked_at = float(entry.get("checked_at"))
    except (TypeError, ValueError):
        return False
    if not entry.get("result"):
        return False
    result = entry.get("result") or {}
    failed = bool(result.get("error")) or bool(result.get("branch_eol_error"))
    ttl = ERROR_RETRY_INTERVAL_SECONDS if failed else VERSION_CHECK_INTERVAL_SECONDS
    return (now - checked_at) < ttl


# ---------------------------------------------------------------------------
# The public check
# ---------------------------------------------------------------------------

def _unknown(image: str, image_id: Optional[str], reason: str,
              alpine: Optional[str] = None, arch: Optional[str] = None,
              current: Optional[str] = None) -> Dict[str, Any]:
    # `branch_eol` is None here, not False: we never got far enough to ask.
    # Claiming "supported" for an image we could not even inspect is the exact
    # lie this field exists to prevent.
    return {
        "current": current,
        "latest": None,
        "update_available": None,
        "branch_eol": None,
        "stable_branch": None,
        "stable_version": None,
        "branch_eol_error": reason,
        "checked_at": None,
        "source": {"current": "image" if current else None, "latest": None},
        "error": reason,
        "from_cache": False,
        "image": image,
        "image_id": image_id,
        "alpine_version": alpine,
        "arch": arch,
        "repo": None,
        "note": None,
    }


def check_chromium_update(client: Any, image: str = DOCKER_IMAGE_NAME, *,
                          force: bool = False, now: float = None,
                          cache_path: Optional[str] = None,
                          network_timeout: float = NETWORK_TIMEOUT_SECONDS,
                          image_timeout: float = IMAGE_READ_TIMEOUT_SECONDS,
                          ) -> Dict[str, Any]:
    """Detect whether a newer Chromium is published for this image's branch.

    NEVER raises. Always returns the documented contract; `update_available` is
    True, False, or None for "could not determine".

    `force=True` bypasses a fresh cache entry and re-checks. It is the manual
    "check now" path — a version check is cheap enough to repeat on request.

    `cache_path` defaults to `CHECK_CACHE_FILE` resolved AT CALL TIME rather
    than as a default argument, so rebinding the module constant actually takes
    effect. (A default argument binds at def time, which silently ignores the
    rebinding and makes redirecting the cache in a test impossible.)
    """
    now = time.time() if now is None else now
    cache_path = CHECK_CACHE_FILE if cache_path is None else cache_path

    # A single inspect decides both whether there is an image at all and
    # whether a cached answer still describes THAT image. Cheaper than the
    # container probe, so it is safe on every call including cache hits.
    image_id, docker_arch, image_error = _image_identity(client, image)
    if image_error:
        _log(image_error)
        result = _unknown(image, None, image_error)
        _write_cache({"checked_at": now, "image": image, "result": result},
                     cache_path)
        return result

    if not force:
        entry = _read_cache(cache_path)
        if (entry and entry.get("image") == image
                and entry.get("image_id") == image_id
                and _cache_is_fresh(entry, now)):
            cached = dict(entry["result"])
            cached["from_cache"] = True
            cached["image_id"] = image_id
            return cached

    installed = read_image_versions(client, image, timeout=image_timeout)
    if installed.get("error"):
        result = _unknown(image, image_id, installed["error"],
                          alpine=installed.get("alpine"),
                          arch=alpine_arch(docker_arch))
        result["current"] = installed.get("chromium")
        _write_cache({"checked_at": now, "image": image,
                      "image_id": image_id, "result": result}, cache_path)
        return result

    branch = normalise_branch(installed.get("alpine"))
    arch = alpine_arch(docker_arch)
    current = installed.get("chromium")

    published = fetch_latest_version(branch, arch, timeout=network_timeout)
    latest = published.get("latest")

    available = is_update_available(current, latest)
    note = None
    if available is None and published.get("error") is None:
        note = "a version could not be parsed, so availability is undetermined"
    if available is False and published.get("error") is None:
        if version_key(latest) < version_key(current):
            note = (f"installed {current} is NEWER than the published "
                    f"{latest}; the image is ahead of this index, so no "
                    f"downgrade is offered")

    error = published.get("error")
    if error and installed.get("error") is None:
        _log(error)

    # Branch EOL, computed AFTER the comparison above and entirely separately.
    #
    # Nothing below this line can change `current`, `latest` or
    # `update_available`. The ordering is the guarantee: the Chromium answer is
    # already final, so a failure here degrades only the new field.
    #
    # Its error goes in `branch_eol_error` and NOT in `error`. `error` is the
    # contract's "the Chromium answer is UNKNOWN" channel, and `detector()`
    # reads a truthy `error` as "discard `latest`, do not rebuild". Folding a
    # failed 3 KB side-lookup into it would throw away a perfectly good version
    # answer and skip a legitimate update — the module's own stated rule is
    # that the EOL check must never be mandatory for a positive result.
    stable = fetch_stable_branch(arch, timeout=network_timeout)
    stable_branch = stable.get("stable_branch")
    branch_eol = is_branch_eol(branch, stable_branch)
    branch_eol_error = stable.get("error")
    if branch_eol_error:
        _log(f"branch support status undetermined: {branch_eol_error}")

    if branch_eol is True:
        # Do NOT set update_available. Rebuilding here would install the newest
        # Chromium this frozen branch will ever publish and leave the image
        # exactly as unpatched, at 1.31 GB a pop — and silently repinning the
        # user's base OS is a bigger decision than updating a package inside
        # it. The fact is reported; the caller decides.
        eol_note = (f"the image's Alpine branch {branch} is no longer the current "
                    f"stable one ({stable_branch}); it is end-of-life and frozen, "
                    "so no further Chromium will be published for it and no "
                    "security fixes reach it — rebuilding within this branch "
                    "cannot fix that")
        note = f"{note}; {eol_note}" if note else eol_note

    result = {
        "current": current,
        "latest": latest,
        "update_available": available,
        "branch_eol": branch_eol,
        "stable_branch": stable_branch,
        "stable_version": stable.get("stable_version"),
        "branch_eol_error": branch_eol_error,
        "checked_at": now,
        "source": {
            "current": installed.get("source") or "image",
            "latest": published.get("source"),
            "stable": stable.get("source"),
        },
        "error": error,
        "from_cache": False,
        "image": image,
        "image_id": image_id,
        "alpine_version": installed.get("alpine"),
        "arch": arch,
        "repo": published.get("repo"),
        "note": note,
    }
    _write_cache({"checked_at": now, "image": image, "image_id": image_id,
                  "result": result}, cache_path)
    return result


# ---------------------------------------------------------------------------
# Wiring into the rebuild half (image_update)
# ---------------------------------------------------------------------------
#
# `image_update.ImageUpdater` consumes a detector callable shaped as
#     detector(image_name) -> {"current": str, "latest": str | None}
# and treats a raising or latest-less answer as "cannot tell" -> no rebuild.
# That is exactly the right reading of `update_available: None`, so this
# adapter is a faithful projection: it exposes `latest` only when the check
# actually determined it.

#: Overridable client factory, so tests never touch a daemon socket.
_client_factory = None


def set_client_factory(factory) -> None:
    """Install the factory used to obtain a Docker client. Tests use this."""
    global _client_factory
    _client_factory = factory


def _default_client():
    if _client_factory is not None:
        return _client_factory()
    import docker  # imported lazily: module import must stay side-effect free
    return docker.from_env()


def detector(image_name: str) -> Dict[str, Any]:
    """Detector adapter for `image_update`. Never raises.

    Returns the FULL result dict (it already carries `current` and `latest`, so
    it satisfies the seam as-is) with `latest` cleared when the answer is
    unknown, because the rebuild half reads a missing `latest` as "do not
    rebuild" and that is the correct fail-closed behaviour.
    """
    try:
        client = _default_client()
    except Exception as exc:
        _log(f"no Docker client available: {exc}")
        return {"current": None, "latest": None, "update_available": None,
                "branch_eol": None, "stable_branch": None, "stable_version": None,
                "branch_eol_error": f"no Docker client: {exc}",
                "checked_at": None, "source": {}, "error": f"no Docker client: {exc}",
                "from_cache": False, "image": image_name, "image_id": None,
                "alpine_version": None, "arch": None, "repo": None, "note": None}

    result = check_chromium_update(client, image_name)
    if result.get("error") or result.get("latest") is None:
        result["latest"] = None
    return result


def register_detector(target: Any = None) -> bool:
    """Install `detector` as `image_update`'s version detector.

    Returns True on success. Best effort and side-effect free at import: this is
    called explicitly (or the env-var opt-in is used), never on the import path,
    so it cannot slow backend startup.

    The env-var route needs no code at all:
        CHROME_ISOLATION_VERSION_DETECTOR=chromium_version:detector
    """
    try:
        if target is None:
            import image_update
            target = image_update
        target.set_version_detector(detector)
        return True
    except Exception as exc:
        _log(f"could not register the version detector: {exc}")
        return False


__all__ = [
    "ALPINE_REPOS",
    "ALPINE_STABLE_RELEASES_URL",
    "CHECK_CACHE_FILE",
    "ERROR_RETRY_INTERVAL_SECONDS",
    "NETWORK_TIMEOUT_SECONDS",
    "VERSION_CHECK_INTERVAL_SECONDS",
    "alpine_arch",
    "check_chromium_update",
    "detector",
    "fetch_latest_version",
    "fetch_stable_branch",
    "is_branch_eol",
    "is_update_available",
    "normalise_branch",
    "read_image_versions",
    "register_detector",
    "run_probe_in_image",
    "set_client_factory",
    "version_key",
]

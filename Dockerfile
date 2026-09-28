ARG ALPINE_VERSION=3.24

# ── Host-identity spoof shim (T4 / F2) — built in a throwaway stage ──────────
# /proc and /sys are read-only in an unprivileged container and Docker's default
# seccomp profile blocks unprivileged user-namespace creation, so the real
# cpuinfo/meminfo/DMI cannot be replaced, bind-mounted over or unlinked. This
# preload shim is the best mitigation available without privileges: it
# redirects libc open()/fopen() of an allow-list of paths onto the per-profile
# fake tree and overrides sysinfo()/uname()/getifaddrs(). It is strictly
# best-effort — see scripts/docker/HOST-SPOOFING.md — and hardware-spoof.sh
# tolerates its absence. Built against musl here so no toolchain lands in the
# runtime image.
FROM alpine:${ALPINE_VERSION} AS hwspoof-build
RUN apk add --no-cache build-base
COPY scripts/docker/libhwspoof.c /src/libhwspoof.c
RUN cc -O2 -fPIC -shared -o /libhwspoof.so /src/libhwspoof.c \
    && strip /libhwspoof.so \
    && test -s /libhwspoof.so

FROM alpine:${ALPINE_VERSION}

ARG USER_ID=1000
ARG GROUP_ID=1000
ARG ALPINE_VERSION

# Alpine releases are named here so the base version is one edit + a rebuild.
# 3.24 is the current latest-stable (verified via
# https://dl-cdn.alpinelinux.org/alpine/latest-stable/).
#
# Note on Vulkan between 3.19 -> 3.24: the Mesa ICD package names were
# reshuffled. `mesa-vulkan-radeon` no longer exists; ATI is now
# `mesa-vulkan-ati` and the old `mesa` metapackage was split up. The two
# drivers this image actually needs keep their names, so the package list
# below is stable across that reshuffle. `mesa-vulkan-nouveau`,
# `mesa-vulkan-swrast` and `mesa-vulkan-virtio` are new names in 3.24 —
# nouveau (16 MiB) is the NVIDIA-on-open-driver case; add it here if a
# profile needs to run Vulkan on an NVIDIA card.

# ── Runtime deps ──────────────────────────────────────────────────────────────
RUN apk add --no-cache \
        chromium \
        alsa-lib \
        mesa-gbm \
        mesa-gl \
        mesa-vulkan-intel \
        mesa-vulkan-ati \
        vulkan-loader \
        libxcb \
        gtk+3.0 \
        libcanberra-gtk3 \
        font-noto \
        pulseaudio-alsa \
        pipewire \
        pipewire-pulse \
        bash \
        python3 \
        coreutils \
        util-linux \
    && addgroup -g ${GROUP_ID} chrome \
    && adduser -u ${USER_ID} -G chrome -D -s /bin/bash chrome \
    && adduser chrome audio \
    && adduser chrome video \
    && mkdir -p /home/chrome/.config/chromium /home/chrome/Downloads /home/chrome/scripts \
    && mkdir -p /home/chrome/.runtime && chmod 700 /home/chrome/.runtime \
    && chown -R chrome:chrome /home/chrome \
    && rm -rf /var/cache/apk/* /tmp/* /var/tmp/* \
    && rm -rf /usr/share/man /usr/share/doc /usr/share/info /usr/share/locale \
    && find /usr -name "*.a" -delete 2>/dev/null || true \
    && find /usr -name "*.la" -delete 2>/dev/null || true

# ── Build-time verification ───────────────────────────────────────────────────
# A build that silently produces an image with no working Chromium is the
# failure mode this guards against: the old image shipped an EOL Alpine whose
# repositories no longer carry the package, and the failure only surfaced at
# container start. Resolve the binary and record what we actually got, and
# fail the build loudly if it is missing.
#
# This also writes the runtime version manifest the UI/backend reads, so the
# reported versions can never drift from what is really installed.
RUN set -eu; \
    bin="$(command -v chromium-browser || true)"; \
    if [ -z "$bin" ]; then \
        echo "FATAL: chromium-browser not found in image" >&2; \
        exit 1; \
    fi; \
    raw="$("$bin" --version 2>&1)"; \
    case "$raw" in \
        *Chromium*) ;; \
        *) echo "FATAL: chromium-browser did not report a version: $raw" >&2; exit 1;; \
    esac; \
    ver="$(printf '%s' "$raw" | sed -n 's/.*Chromium \([0-9][0-9.]*\).*/\1/p')"; \
    if [ -z "$ver" ]; then \
        echo "FATAL: could not parse Chromium version from: $raw" >&2; exit 1; \
    fi; \
    # The launcher must be runnable by the non-root user the container runs
    # as — a Chromium that only works as root would fail at container start.
    # runuser comes from util-linux, which is in the package list above.
    runuser -u chrome -- "$bin" --version >/dev/null; \
    mkdir -p /usr/local/share/chrome-isolation; \
    { \
        echo "ALPINE_VERSION=$ALPINE_VERSION"; \
        echo "CHROMIUM_VERSION=$ver"; \
        echo "CHROMIUM_VERSION_RAW=$raw"; \
    } > /usr/local/share/chrome-isolation/versions.env; \
    chmod 644 /usr/local/share/chrome-isolation/versions.env; \
    echo "Image build OK: Alpine $ALPINE_VERSION / Chromium $ver"

# ── Container scripts (COPY keeps Dockerfile readable, no inline printf) ─────
COPY --chown=chrome:chrome scripts/docker/hardware-spoof.sh   /home/chrome/scripts/
COPY --chown=chrome:chrome scripts/docker/user-agent-spoof.sh /home/chrome/scripts/
COPY --chown=chrome:chrome scripts/docker/stealth-launch.sh   /home/chrome/scripts/
COPY --chown=chrome:chrome scripts/docker/proxy-wrapper.py    /home/chrome/scripts/

RUN chmod +x /home/chrome/scripts/*.sh

# ── The spoof shim from the build stage (T4 / F2) ────────────────────────────
# Readable + executable by the `chrome` user; never fatal if missing.
COPY --from=hwspoof-build /libhwspoof.so /usr/lib/libhwspoof.so
RUN chmod 755 /usr/lib/libhwspoof.so

# ── Version metadata ──────────────────────────────────────────────────────────
# Discoverable two ways: OCI labels via `docker inspect` (what the backend
# already has a Docker socket for), and /usr/local/share/chrome-isolation/
# versions.env for in-container reads. user-agent-spoof.sh deliberately keeps
# deriving the UA from the binary itself — this metadata is for reporting only
# and is never used to build the UA string.
LABEL org.opencontainers.image.title="isolated-chrome" \
      org.opencontainers.image.description="Isolated Chrome profile container for chrome-isolation-manager" \
      org.opencontainers.image.base.name="docker.io/library/alpine:${ALPINE_VERSION}" \
      org.opencontainers.image.base.version="Alpine ${ALPINE_VERSION}" \
      org.opencontainers.image.vendor="chrome-isolation-manager"

# ── Environment ───────────────────────────────────────────────────────────────
ENV CHROME_PROFILE=default
ENV DISPLAY=:0

USER chrome
WORKDIR /home/chrome

# ── Healthcheck ───────────────────────────────────────────────────────────────
# Checks the two things that actually break in practice: the Chromium binary
# being present/executable for the chrome user, and the version manifest that
# the UI reads. Deliberately does not invoke Chromium's GUI entrypoint.
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD test -x /usr/bin/chromium-browser \
        && test -r /usr/local/share/chrome-isolation/versions.env \
        || exit 1

# stealth-launch.sh uses --no-sandbox: Docker's default seccomp profile blocks
# unprivileged user-namespace creation, so Chromium's internal sandbox cannot
# start. The container itself is the security boundary.
ENTRYPOINT ["/home/chrome/scripts/stealth-launch.sh"]
CMD []

FROM alpine:3.19

ARG USER_ID=1000
ARG GROUP_ID=1000

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
    && find /usr/lib -name "*.a" -delete 2>/dev/null || true \
    && find /usr/lib -name "*.la" -delete 2>/dev/null || true

# ── Container scripts (COPY keeps Dockerfile readable, no inline printf) ─────
COPY --chown=chrome:chrome scripts/docker/hardware-spoof.sh   /home/chrome/scripts/
COPY --chown=chrome:chrome scripts/docker/user-agent-spoof.sh /home/chrome/scripts/
COPY --chown=chrome:chrome scripts/docker/stealth-launch.sh   /home/chrome/scripts/

RUN chmod +x /home/chrome/scripts/*.sh

# ── Environment ───────────────────────────────────────────────────────────────
ENV CHROME_PROFILE=default
ENV DISPLAY=:0

USER chrome
WORKDIR /home/chrome

# stealth-launch.sh uses --no-sandbox: Docker's default seccomp profile blocks
# unprivileged user-namespace creation, so Chromium's internal sandbox cannot
# start. The container itself is the security boundary.
ENTRYPOINT ["/home/chrome/scripts/stealth-launch.sh"]
CMD []

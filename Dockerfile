# BUILD_FROM is injected by the Home Assistant Supervisor per architecture, from the
# `build_from:` map in build.yaml — Supervisor calls
# `docker build --build-arg BUILD_FROM=<value for that arch>` (confirmed against
# https://developers.home-assistant.io/docs/apps/configuration/#app-dockerfile).
# build.yaml pins every architecture to the same value, `python:3.12-alpine` — the
# official, Docker-Hub-hosted, multi-arch Python image this add-on has always run on
# (coordinator decision: this is what's actually running for every user today; the
# alternative HA-maintained base images were rejected — the legacy per-arch
# `*-base-python:3.12-alpine3.18` hasn't been rebuilt since 2024-12-09, and the
# current multi-arch `base-python` image only covers amd64/arm64, dropping
# armhf/armv7/i386). Verified via the Docker Hub registry API (anonymous token) that
# `python:3.12-alpine`'s manifest list covers all 5 architectures this add-on
# declares in config.yaml: linux/amd64, linux/arm64/v8 (aarch64), linux/arm/v7
# (armv7), linux/arm/v6 (armhf), linux/386 (i386).
#
# No default value on purpose: build.yaml exists in this repo (5 archs), so the
# Supervisor always provides BUILD_FROM. A plain `docker build .` without
# `--build-arg BUILD_FROM=<value>` (i.e. bypassing the Supervisor/build.yaml) is
# expected to fail fast here rather than silently building against an unrelated image.
ARG BUILD_FROM
FROM ${BUILD_FROM}

# python:3.12-alpine ships neither an init system nor these CLI tools — restored
# exactly as they were before BUILD_FROM was wired up (`git show HEAD:Dockerfile`):
# git (GitPython, tools/git_ops.py), bash + jq (run.sh reads /data/options.json with
# jq and is a bash script), curl (the HEALTHCHECK below).
RUN apk add --no-cache \
    git \
    bash \
    curl \
    jq

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x run.sh

EXPOSE 7123

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:7123/health || exit 1

# No ENTRYPOINT: python:3.12-alpine has none (unlike the HA base images, which set
# `ENTRYPOINT ["/init"]` for s6-overlay) — run.sh runs directly as PID 1's command,
# same as every previous release.
CMD ["./run.sh"]

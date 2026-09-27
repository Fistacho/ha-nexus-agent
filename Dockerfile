# Hardcoded on purpose — NOT an ARG BUILD_FROM fed by build.yaml (0.22.0 tried that;
# reverted in 0.22.1). Root cause, confirmed against home-assistant/supervisor tag
# 2026.09.2 (supervisor/apps/validate.py, SCHEMA_BUILD_CONFIG):
#
#   RE_DOCKER_IMAGE_BUILD = re.compile(
#       r"^([a-zA-Z\-\.:\d{}]+/)*?([\-\w{}]+)/([\-\w{}]+)(:[\.\-\w{}]+)?$"
#   )
#
# requires a namespace/repository split (at least one '/'). A bare official-image
# reference like `python:3.12-alpine` — valid for `docker pull`/`FROM`, since Docker
# Hub resolves it to `library/python:3.12-alpine` — fails that regex. When it does,
# `AppBuild._read_build_config` doesn't raise; it silently substitutes
# `ghcr.io/home-assistant/base:latest` (a bare OS image, no Python, no apk) as the
# `--build-arg BUILD_FROM=...` Supervisor passes to `docker buildx build`, which is
# what actually broke `update.install` under the real Supervisor (HA error log:
# "Error updating Nexus Agent: An unknown error occurred while trying to build the
# image for app 0e2d93bb_nexus_agent").
#
# `AppBuild.create()` also warns, whenever any build.yaml is present at all,
# regardless of content: "App %s uses build.yaml which is deprecated. Move build
# parameters into the Dockerfile directly." — this add-on needs no per-architecture
# build difference (see multi-arch verification below), so there is nothing to move:
# a literal FROM here is both what Supervisor already recommends and exactly what
# every release through 0.21.0 shipped and verified working. No build.yaml in this
# repo (see tests/test_build_image_consistency.py) — Supervisor never reads a
# BUILD_FROM value for this add-on at all, so this regex can't bite again.
#
# `python:3.12-alpine` is the official, Docker-Hub-hosted, multi-arch Python image
# this add-on has always run on (coordinator decision: this is what's actually
# running for every user today; the alternative HA-maintained base images were
# rejected — the legacy per-arch `*-base-python:3.12-alpine3.18` hasn't been rebuilt
# since 2024-12-09, and the current multi-arch `base-python` image only covers
# amd64/arm64, dropping armhf/armv7/i386). Verified via the Docker Hub registry API
# (anonymous token) that `python:3.12-alpine`'s manifest list covers all 5
# architectures this add-on declares in config.yaml: linux/amd64, linux/arm64/v8
# (aarch64), linux/arm/v7 (armv7), linux/arm/v6 (armhf), linux/386 (i386) — `docker
# buildx build --platform <target>` (what Supervisor's AppBuild.get_docker_args()
# always passes) picks the right manifest entry from this single literal FROM.
FROM python:3.12-alpine

# python:3.12-alpine ships neither an init system nor these CLI tools — installed
# explicitly: git (GitPython, tools/git_ops.py), bash + jq (run.sh reads
# /data/options.json with jq and is a bash script), curl (the HEALTHCHECK below).
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

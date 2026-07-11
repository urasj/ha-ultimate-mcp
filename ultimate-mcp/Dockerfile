ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.24
FROM $BUILD_FROM

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2

# Build context is THIS directory (home-assistant/builder convention); the CI
# workflow stages the repo's server/ package in here before building.
COPY server /opt/umcp
RUN pip install --no-cache-dir /opt/umcp

COPY rootfs /

HEALTHCHECK --interval=60s --timeout=10s \
    CMD wget -qO- http://127.0.0.1:8099/health || exit 1

# Base image pinned by digest so every build uses the same Python 3.12 slim.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

# Released Atesto version and the sha256 of its PyPI wheel. Both are required
# build arguments: the gate template (integrations/github-actions/atesto-gate.yml)
# is the single committed pin, and a default here would silently duplicate it.
ARG ATESTO_VERSION
ARG ATESTO_SHA256

# The labels let release verification compare the image back to the wheel and
# the template pin without unpacking the image.
LABEL org.opencontainers.image.title="Atesto" \
      org.opencontainers.image.version="${ATESTO_VERSION}" \
      org.opencontainers.image.source="https://github.com/guihirsch/atesto" \
      dev.atesto.wheel-sha256="${ATESTO_SHA256}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin atesto \
    && mkdir -p /repo \
    && chown atesto:atesto /repo

# --require-hashes only accepts hashes in requirements-file syntax, so the
# pinned wheel is written to a temporary file. Atesto is stdlib-only, hence
# --no-deps. No gh/glab: provider adapters run where those CLIs exist.
RUN test -n "$ATESTO_VERSION" && test -n "$ATESTO_SHA256" \
    || { echo "ATESTO_VERSION and ATESTO_SHA256 build args are required" >&2; exit 1; }
RUN printf 'atesto==%s --hash=sha256:%s\n' "$ATESTO_VERSION" "$ATESTO_SHA256" \
      > /tmp/atesto-requirements.txt \
    && python -m pip install \
         --no-cache-dir \
         --require-hashes \
         --no-deps \
         -r /tmp/atesto-requirements.txt \
    && rm -f /tmp/atesto-requirements.txt

USER atesto
WORKDIR /repo
ENTRYPOINT ["atesto"]

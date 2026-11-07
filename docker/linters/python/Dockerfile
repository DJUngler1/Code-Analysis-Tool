# Linter/formatter image for Python (step 2): Ruff.
# Definition: docs/linterConventions/README.md
FROM python:3.13.18-slim

ARG RUFF_VERSION=0.17.4
RUN pip install --no-cache-dir --disable-pip-version-check "ruff==${RUFF_VERSION}"

ENV HOME=/tmp/home
RUN mkdir -p /opt/code-analysis \
    && printf 'ruff\t%s\n' "$(ruff --version)" > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

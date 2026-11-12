# Python package manager tools (step 4): pip-audit (PyPA) for the audit, and
# the package managers whose lock files are exported for it or generated
# when missing. Each tool in its own virtual environment (no shared
# dependencies). Definition: docs/dependencyAudit/README.md
FROM python:3.13.18-slim

ARG PIP_AUDIT_VERSION=2.10.1
ARG UV_VERSION=0.13.7
ARG POETRY_VERSION=2.5.1
ARG POETRY_EXPORT_VERSION=1.10.1
ARG PDM_VERSION=2.29.7
ARG PIPENV_VERSION=2026.11.0

# git: dependencies from git repositories.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

RUN set -e; \
    install() { name="$1"; shift; python -m venv "/opt/$name"; "/opt/$name/bin/pip" install --no-cache-dir --disable-pip-version-check "$@"; }; \
    install pip-audit "pip-audit==${PIP_AUDIT_VERSION}"; \
    install uv "uv==${UV_VERSION}"; \
    install poetry "poetry==${POETRY_VERSION}" "poetry-plugin-export==${POETRY_EXPORT_VERSION}"; \
    install pdm "pdm==${PDM_VERSION}"; \
    install pipenv "pipenv==${PIPENV_VERSION}"; \
    ln -s /opt/pip-audit/bin/pip-audit /opt/uv/bin/uv /opt/poetry/bin/poetry /opt/pdm/bin/pdm \
        /opt/pipenv/bin/pipenv /usr/local/bin/

# The containers run as the host user, so all caches live in /tmp.
ENV HOME=/tmp/home \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_CREATE=false \
    PIPENV_VENV_IN_PROJECT=0 \
    UV_CACHE_DIR=/tmp/uv-cache
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'pip-audit\t%s\n' "$(pip-audit --version)"; \
        printf 'uv\t%s\n' "$(uv --version)"; \
        printf 'poetry\t%s\n' "$(poetry --version)"; \
        printf 'pdm\t%s\n' "$(pdm --version)"; \
        printf 'pipenv\t%s\n' "$(pipenv --version)"; \
        printf 'python\tPython %s\n' "$(python -c 'import platform; print(platform.python_version())')"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

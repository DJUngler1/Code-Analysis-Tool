# Test image for Python (step 5): the package managers that install the
# project and its test dependencies into a virtual environment (.venv) in the
# copy of the project; the tests run with pytest from that environment.
# The full image (not -slim) has a C compiler and headers for packages that
# are built from source. Definition: docs/testAnalysis/README.md
FROM python:3.13.18

ARG UV_VERSION=0.13.7
ARG POETRY_VERSION=2.5.1
ARG PDM_VERSION=2.29.7
ARG PIPENV_VERSION=2026.11.0

RUN git config --system --add safe.directory '*'

# Each tool in its own virtual environment (no shared dependencies).
RUN set -e; \
    install() { name="$1"; shift; python -m venv "/opt/$name"; "/opt/$name/bin/pip" install --no-cache-dir --disable-pip-version-check "$@"; }; \
    install uv "uv==${UV_VERSION}"; \
    install poetry "poetry==${POETRY_VERSION}"; \
    install pdm "pdm==${PDM_VERSION}"; \
    install pipenv "pipenv==${PIPENV_VERSION}"; \
    ln -s /opt/uv/bin/uv /opt/poetry/bin/poetry /opt/pdm/bin/pdm /opt/pipenv/bin/pipenv /usr/local/bin/

# The containers run as the host user, so all caches live in /tmp or in the
# step folder (set per run). The project's environment is .venv in the copy.
ENV HOME=/tmp/home \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    PIPENV_VENV_IN_PROJECT=1 \
    PDM_VENV_IN_PROJECT=true
RUN mkdir -p /opt/code-analysis \
    && { \
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

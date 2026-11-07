# Scheduler: the Python orchestrator of the Code-Analysis-Tool.
# Build context is the repository root (see docker/docker-compose.yml).
FROM python:3.13-slim

# git: acceptance check (clean working tree). safe.directory: mounted projects
# belong to the host user, which git would reject as "dubious ownership".
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app
COPY scheduler/pyproject.toml ./
COPY scheduler/src ./src
RUN pip install --no-cache-dir .

# The container only idles; the tool (console script `code-analysis`) is started
# in it with `docker exec`, see bin/code-analysis. Stopping is immediate because
# docker-compose.yml runs this under an init process (`init: true`).
CMD ["sleep", "infinity"]

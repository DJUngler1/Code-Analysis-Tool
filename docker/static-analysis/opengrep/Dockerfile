# OpenGrep image (step 3): the OpenGrep binary and the rule sets that are active
# in config/opengrep.toml, so a scan needs no network. Rule sets that are only
# defined are not downloaded; activating one needs a rebuild.
# Build context is the repository root (see docker/docker-compose.yml).
# Definition: docs/staticAnalysis/README.md
FROM python:3.13.18-slim

# The checksums below belong to this version (digests of the GitHub release assets).
ARG OPENGREP_VERSION=1.33.1
# Set by BuildKit: amd64 or arm64.
ARG TARGETARCH

# git: OpenGrep scans the files git knows (`git ls-files`) and the build step
# fetches the rule sets. safe.directory: mounted projects belong to the host
# user, which git would reject as "dubious ownership".
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && git config --system --add safe.directory '*'

# Self-contained release binary (no Python needed at run time).
RUN case "${TARGETARCH}" in \
        amd64) asset=opengrep_manylinux_x86; sha256=46e8133cc490f9f50518061590ce2f6be5a08afaeda034471c0ceda4b3899808 ;; \
        arm64) asset=opengrep_manylinux_aarch64; sha256=d1433f63a90b8bc0b6ad4b498a13b372b856ce9a754a43484ba4920fe2d76dcf ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /usr/local/bin/opengrep \
        "https://github.com/opengrep/opengrep/releases/download/v${OPENGREP_VERSION}/${asset}" \
    && echo "${sha256}  /usr/local/bin/opengrep" | sha256sum -c - \
    && chmod 755 /usr/local/bin/opengrep

# OpenGrep's own default ignore list. The scheduler passes it explicitly, so
# that a .semgrepignore of the analysed project (which would replace the
# default list) is ignored.
RUN mkdir -p /opt/code-analysis/opengrep \
    && curl -fsSL -o /opt/code-analysis/opengrep/default.semgrepignore \
        "https://raw.githubusercontent.com/opengrep/opengrep/v${OPENGREP_VERSION}/cli/src/semgrep/templates/.semgrepignore" \
    && echo "e6acfd569980d21db340e98529dd632bc45d750c1b1f0c9784730e086f298bf5  /opt/code-analysis/opengrep/default.semgrepignore" \
        | sha256sum -c -

# The binary unpacks itself below $HOME on its first start (about 250 MB);
# every RUN that starts it removes that copy again, so it stays out of the image.
ENV HOME=/tmp/home
RUN mkdir -p /tmp/home \
    && printf 'opengrep\t%s\n' "$(opengrep --version)" > /opt/code-analysis/versions.tsv \
    && rm -rf /tmp/home

# Active rule sets: fetched at the pinned commits and loaded once by OpenGrep,
# which fails the build if it rejects a rule. Last, so that a change of the
# config file only repeats this step.
COPY config/opengrep.toml docker/static-analysis/opengrep/fetch_rulesets.py /opt/code-analysis/opengrep/

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN mkdir -p /tmp/home \
    && python3 /opt/code-analysis/opengrep/fetch_rulesets.py \
        /opt/code-analysis/opengrep/opengrep.toml /opt/code-analysis/opengrep \
    && chmod -R a+rX /opt/code-analysis \
    && find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

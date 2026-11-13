# Test image for JavaScript/TypeScript (step 5): Node.js with npm, Yarn and
# pnpm (corepack) and Bun, to install the project and run its test framework;
# c8 for the test coverage of frameworks without their own (Mocha, node:test).
# The full image (not -slim) has Python, make and a C++ compiler for native
# addons (node-gyp). No browsers: Playwright, Cypress and Karma are not run.
# Definition: docs/testAnalysis/README.md
FROM node:22.23.4

ARG BUN_VERSION=1.4.4
ARG C8_VERSION=12.0.0
# Set by BuildKit: amd64 or arm64.
ARG TARGETARCH

# Yarn and pnpm through corepack, as in lint-node: the version of the
# project's "packageManager" field, else the versions pinned in corepack.
RUN corepack enable \
    && npm install -g "c8@${C8_VERSION}" \
    && npm cache clean --force
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    COREPACK_DEFAULT_TO_LATEST=0

# Bun, checked against the digest of the GitHub release asset.
RUN apt-get update \
    && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
        amd64) asset=bun-linux-x64; sha256=bf8ccbfb2b0b80159e0f9a6d15c55317add8077534dbc6130a3edb52fccb395c ;; \
        arm64) asset=bun-linux-aarch64; sha256=baa6bb36f51d6e58561e02bbea825038a9df454f673d6d776e8bbf86f8c7850e ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/bun.zip "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/${asset}.zip" \
    && echo "${sha256}  /tmp/bun.zip" | sha256sum -c - \
    && unzip -q /tmp/bun.zip -d /opt \
    && install -m 755 "/opt/${asset}/bun" /usr/local/bin/bun \
    && rm -rf /tmp/bun.zip "/opt/${asset}"

ENV HOME=/tmp/home
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'node\tNode.js %s\n' "$(node --version)"; \
        printf 'npm\tnpm %s\n' "$(npm --version)"; \
        printf 'corepack\tcorepack %s\n' "$(corepack --version)"; \
        printf 'bun\tBun %s\n' "$(bun --version)"; \
        printf 'c8\tc8 %s\n' "$(c8 --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

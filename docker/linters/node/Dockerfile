# JavaScript/TypeScript image: Biome (step 2, installed at build time, so a
# lint run downloads nothing) and the package managers for the audits of
# step 4: npm (bundled), Yarn and pnpm (corepack), Bun.
# Definitions: docs/linterConventions/README.md, docs/dependencyAudit/README.md
FROM node:22.23.4-slim

ARG BIOME_VERSION=2.6.6
ARG BUN_VERSION=1.4.4
# Set by BuildKit: amd64 or arm64.
ARG TARGETARCH
RUN npm install -g "@biomejs/biome@${BIOME_VERSION}" && npm cache clean --force

# Yarn and pnpm through corepack: it fetches the version named in the
# project's "packageManager" field when they first run. Without that field it
# uses the versions pinned in this corepack release, not the latest ones
# (COREPACK_DEFAULT_TO_LATEST=0), so runs are reproducible.
RUN corepack enable
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 \
    COREPACK_DEFAULT_TO_LATEST=0

# Bun, checked against the digest of the GitHub release asset.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
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
        printf 'biome\t%s on Node %s\n' "$(biome --version)" "$(node --version)"; \
        printf 'npm\tnpm %s\n' "$(npm --version)"; \
        printf 'corepack\tcorepack %s\n' "$(corepack --version)"; \
        printf 'bun\tBun %s\n' "$(bun --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

# Linter/formatter image for JavaScript and TypeScript (step 2): Biome.
# Installed at build time, so a run downloads nothing.
# Definition: docs/linterConventions/README.md
FROM node:22.23.4-slim

ARG BIOME_VERSION=2.6.6
RUN npm install -g "@biomejs/biome@${BIOME_VERSION}" && npm cache clean --force

ENV HOME=/tmp/home
RUN mkdir -p /opt/code-analysis \
    && printf 'biome\t%s on Node %s\n' "$(biome --version)" "$(node --version)" > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

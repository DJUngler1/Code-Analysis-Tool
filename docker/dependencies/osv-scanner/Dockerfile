# OSV-Scanner (step 4, stage 1): the official image, pinned by its digest, plus
# the version file the scheduler reads. Definition: docs/dependencyAudit/README.md
FROM ghcr.io/google/osv-scanner:v2.7.1@sha256:a70ffffc2bfd33482953e5d6b64c6fcdb30239717a07b0f0d3ab2859ce3e4cde

# The scheduler passes the full command (`osv-scanner scan source ...`), as for
# the other tool images.
ENTRYPOINT []
ENV HOME=/tmp/home
RUN ln -s /osv-scanner /usr/local/bin/osv-scanner \
    && mkdir -p /opt/code-analysis \
    && printf 'osv-scanner\t%s\n' "$(osv-scanner --version | head -n 1 | sed 's/^osv-scanner version: //')" \
        > /opt/code-analysis/versions.tsv \
    && find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 -p /tmp/home

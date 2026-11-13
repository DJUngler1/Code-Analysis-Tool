# Test image for Rust (step 5): cargo test. The full image (not -slim) has a
# C compiler, pkg-config and the OpenSSL headers that many build scripts need.
# Definition: docs/testAnalysis/README.md
FROM rust:1.99.1

# The containers run as the host user: the crate registry (CARGO_HOME) is set
# per run to the step folder. The cargo/rustup binaries stay in /usr/local/cargo/bin.
ENV CARGO_HOME=/tmp/cargo \
    PATH=/usr/local/cargo/bin:$PATH \
    HOME=/tmp/home
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'cargo\t%s\n' "$(cargo --version)"; \
        printf 'rustc\t%s\n' "$(rustc --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

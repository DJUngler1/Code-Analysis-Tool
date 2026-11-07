# Linter/formatter image for Rust (step 2): Clippy and rustfmt.
# Definition: docs/linterConventions/README.md
FROM rust:1.99.1-slim

# Many crates build C code or link OpenSSL in their build scripts.
RUN apt-get update \
    && apt-get install -y --no-install-recommends pkg-config libssl-dev \
    && rm -rf /var/lib/apt/lists/* \
    && rustup component add clippy rustfmt

# Empty configuration files: pointing Clippy and rustfmt here makes them ignore
# the project's clippy.toml and rustfmt.toml.
RUN mkdir -p /opt/code-analysis/clippy /opt/code-analysis/rustfmt \
    && touch /opt/code-analysis/clippy/clippy.toml /opt/code-analysis/rustfmt/rustfmt.toml

# The containers run as the host user: the crate registry goes to /tmp.
# The cargo/rustup binaries stay in /usr/local/cargo/bin.
ENV CARGO_HOME=/tmp/cargo \
    PATH=/usr/local/cargo/bin:$PATH \
    HOME=/tmp/home
RUN { \
        printf 'clippy\t%s\n' "$(cargo clippy --version)"; \
        printf 'rustfmt\t%s\n' "$(rustfmt --version)"; \
        printf 'cargo\t%s\n' "$(cargo --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

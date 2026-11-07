# Linter/formatter image for C and C++ (step 2): clang-format and clang-tidy 22,
# plus CMake, Conan and vcpkg to generate compile_commands.json.
# Definition: docs/linterConventions/README.md
FROM debian:trixie-slim

ARG CONAN_VERSION=2.34.0
ARG VCPKG_TAG=2026.10.16

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        clang-22 clang-format-22 clang-tidy-22 libc6-dev libstdc++-14-dev \
        cmake ninja-build make pkg-config git ca-certificates curl zip unzip tar \
        python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/bin/clang-format-22 /usr/local/bin/clang-format \
    && ln -s /usr/bin/clang-tidy-22 /usr/local/bin/clang-tidy \
    && ln -s /usr/bin/run-clang-tidy-22 /usr/local/bin/run-clang-tidy

RUN python3 -m venv /opt/conan \
    && /opt/conan/bin/pip install --no-cache-dir "conan==${CONAN_VERSION}" \
    && ln -s /opt/conan/bin/conan /usr/local/bin/conan

# vcpkg runs as the host user: its root must be writable (buildtrees, packages).
# On arm64 Linux, vcpkg needs the system cmake/ninja instead of its own downloads.
ENV VCPKG_ROOT=/opt/vcpkg \
    VCPKG_FORCE_SYSTEM_BINARIES=1 \
    VCPKG_DISABLE_METRICS=1
RUN git clone --depth 1 --branch "${VCPKG_TAG}" https://github.com/microsoft/vcpkg "${VCPKG_ROOT}" \
    && "${VCPKG_ROOT}/bootstrap-vcpkg.sh" -disableMetrics \
    && ln -s "${VCPKG_ROOT}/vcpkg" /usr/local/bin/vcpkg \
    && chmod -R a+rwX "${VCPKG_ROOT}"

# The containers run as the host user, so all caches live in /tmp.
ENV CC=clang-22 \
    CXX=clang++-22 \
    HOME=/tmp/home \
    CONAN_HOME=/tmp/conan \
    VCPKG_DOWNLOADS=/tmp/vcpkg-downloads \
    VCPKG_DEFAULT_BINARY_CACHE=/tmp/vcpkg-cache
RUN mkdir -p /opt/code-analysis && { \
        printf 'clang-format\t%s\n' "$(clang-format-22 --version | head -n 1)"; \
        printf 'clang-tidy\t%s\n' "$(clang-tidy-22 --version | grep -i 'version' | head -n 1 | sed 's/^ *//')"; \
        printf 'cmake\t%s\n' "$(cmake --version | head -n 1)"; \
        printf 'conan\t%s\n' "$(conan --version)"; \
        printf 'vcpkg\t%s\n' "$(vcpkg version | head -n 1)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home /tmp/vcpkg-cache /tmp/vcpkg-downloads

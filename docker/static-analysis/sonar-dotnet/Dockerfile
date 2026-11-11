# SonarScanner for .NET (step 3): C# analysis needs it ("The SonarScanner CLI
# doesn't support C# or VB.NET analysis"); it analyses the code while
# `dotnet build` runs. Same .NET SDK as the image of step 2 (lint-dotnet).
# The scanner gets its Java runtime from the SonarQube server (JRE provisioning).
# Definition: docs/staticAnalysis/README.md
FROM mcr.microsoft.com/dotnet/sdk:10.0.403

ARG SCANNER_VERSION=12.1.0
ARG NODE_VERSION=24.22.3
# Set by BuildKit: amd64 or arm64.
ARG TARGETARCH

# Node.js (Active LTS) for the other languages the scanner analyses
# (sonar.scanner.scanAll): the JS/TS analyzer needs it, as in the CLI image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && case "${TARGETARCH}" in \
        amd64) arch=x64; sha256=4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb ;; \
        arm64) arch=arm64; sha256=6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && echo "${sha256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /opt \
    && ln -s "/opt/node-v${NODE_VERSION}-linux-${arch}/bin/node" /usr/local/bin/node \
    && rm /tmp/node.tar.xz

ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    HOME=/tmp/home \
    DOTNET_CLI_HOME=/tmp/home \
    NUGET_PACKAGES=/tmp/nuget \
    SONAR_USER_HOME=/tmp/home/.sonar \
    PATH="/opt/dotnet-tools:${PATH}"
RUN dotnet tool install --tool-path /opt/dotnet-tools dotnet-sonarscanner --version "${SCANNER_VERSION}" \
    && chmod -R a+rX /opt/dotnet-tools \
    && mkdir -p /opt/code-analysis \
    && { \
        printf 'dotnet-sonarscanner\tSonarScanner for .NET %s\n' "${SCANNER_VERSION}"; \
        printf 'dotnet\t.NET SDK %s\n' "$(dotnet --version)"; \
        printf 'node\tNode.js %s\n' "$(node --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

# SonarScanner CLI (step 3), plus JDK, Maven and Gradle to compile Java
# projects first (SonarQube's Java analysis needs the bytecode), and Node.js
# for the JavaScript/TypeScript analysis.
# The official image sonarsource/sonar-scanner-cli exists only for amd64, so
# this image installs the official scanner distribution for the architecture.
# Definition: docs/staticAnalysis/README.md
# JDK 21 (LTS): runs the build tools of most projects, e.g. Gradle 8.5 and
# later (Gradle supports Java 25 only from 9.1), and compiles for Java 8 to 21.
FROM eclipse-temurin:21-jdk-noble

ARG SCANNER_VERSION=8.3.1.16491
ARG MAVEN_VERSION=3.9.16
ARG MAVEN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6
ARG GRADLE_VERSION=9.9.1
ARG GRADLE_SHA256=d8a18a7a841dd5df825d35a685fad6f2d7f12ac0c280e57b67c27b4a520ba228
ARG NODE_VERSION=24.22.3
# Set by BuildKit: amd64 or arm64.
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git unzip xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Scanner with its embedded Java runtime. Checksums published by SonarSource
# (<zip>.sha256 next to each download).
RUN case "${TARGETARCH}" in \
        amd64) arch=linux-x64; sha256=0f304e4c5982f0cfbd06762306d6e60e6ed9c8e3853629ae3b8ec2f4dc9bdc04 ;; \
        arm64) arch=linux-aarch64; sha256=aca51f414a581b8f088207e089f281b1b2985fb202160e8ca2e135a305639d26 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && curl -fsSL -o /tmp/scanner.zip \
        "https://binaries.sonarsource.com/Distribution/sonar-scanner-cli/sonar-scanner-cli-${SCANNER_VERSION}-${arch}.zip" \
    && echo "${sha256}  /tmp/scanner.zip" | sha256sum -c - \
    && unzip -q /tmp/scanner.zip -d /opt \
    && mv "/opt/sonar-scanner-${SCANNER_VERSION}-${arch}" /opt/sonar-scanner \
    && ln -s /opt/sonar-scanner/bin/sonar-scanner /usr/local/bin/sonar-scanner \
    && rm /tmp/scanner.zip

# Build tools for Java projects without a wrapper (mvnw, gradlew).
RUN curl -fsSL -o /tmp/maven.tar.gz \
        "https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz" \
    && echo "${MAVEN_SHA512}  /tmp/maven.tar.gz" | sha512sum -c - \
    && tar -xzf /tmp/maven.tar.gz -C /opt \
    && ln -s "/opt/apache-maven-${MAVEN_VERSION}/bin/mvn" /usr/local/bin/mvn \
    && curl -fsSL -o /tmp/gradle.zip "https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip" \
    && echo "${GRADLE_SHA256}  /tmp/gradle.zip" | sha256sum -c - \
    && unzip -q /tmp/gradle.zip -d /opt \
    && ln -s "/opt/gradle-${GRADLE_VERSION}/bin/gradle" /usr/local/bin/gradle \
    && rm /tmp/maven.tar.gz /tmp/gradle.zip

# Node.js (Active LTS): the JS/TS analyzer did not start its embedded Node.js
# on linux-arm64 ("Error when running: 'node -v'"). Checksums from SHASUMS256.txt.
RUN case "${TARGETARCH}" in \
        amd64) arch=x64; sha256=4f2b175a6d53a5c617a80dba2676238a0688cef28c6da4eba35c7d844e66fcfb ;; \
        arm64) arch=arm64; sha256=6e51a2ada00f0011cc266f65d98922b403d7ad88f809ca2aa615a5fe1ce6e86c ;; \
    esac \
    && curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    && echo "${sha256}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /opt \
    && ln -s "/opt/node-v${NODE_VERSION}-linux-${arch}/bin/node" /usr/local/bin/node \
    && rm /tmp/node.tar.xz

# The containers run as the host user, so all caches live in /tmp.
ENV HOME=/tmp/home \
    SONAR_USER_HOME=/tmp/home/.sonar \
    GRADLE_USER_HOME=/tmp/home/.gradle
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'sonar-scanner\tSonarScanner CLI %s\n' "${SCANNER_VERSION}"; \
        printf 'java\t%s\n' "$(java -version 2>&1 | head -n 1)"; \
        printf 'maven\tMaven %s\n' "${MAVEN_VERSION}"; \
        printf 'gradle\tGradle %s\n' "${GRADLE_VERSION}"; \
        printf 'node\tNode.js %s\n' "$(node --version)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

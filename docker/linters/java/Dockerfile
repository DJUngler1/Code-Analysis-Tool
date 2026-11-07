# Linter/formatter image for Java (step 2): google-java-format and PMD.
# The newest JDK: google-java-format needs a JDK at least as new as the Java
# version of the formatted sources. Definition: docs/linterConventions/README.md
FROM eclipse-temurin:27-jdk

ARG GJF_VERSION=2.0.0
ARG GJF_SHA256=c78feb48c549fa7114b7ea11db3b456808f7b371ffe84e6643a44dd2f9588639
ARG PMD_VERSION=7.29.0
ARG PMD_SHA256=d27177c185b837fa57f097e482d1253468aa035379c5eb2671fa0a279109b179

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/google-java-format \
    && curl -fsSL -o /opt/google-java-format/google-java-format.jar \
        "https://github.com/google/google-java-format/releases/download/v${GJF_VERSION}/google-java-format-${GJF_VERSION}-all-deps.jar" \
    && echo "${GJF_SHA256}  /opt/google-java-format/google-java-format.jar" | sha256sum -c - \
    && printf '#!/bin/sh\nexec java -jar /opt/google-java-format/google-java-format.jar "$@"\n' \
        > /usr/local/bin/google-java-format \
    && chmod 755 /usr/local/bin/google-java-format

RUN curl -fsSL -o /tmp/pmd.zip \
        "https://github.com/pmd/pmd/releases/download/pmd_releases%2F${PMD_VERSION}/pmd-dist-${PMD_VERSION}-bin.zip" \
    && echo "${PMD_SHA256}  /tmp/pmd.zip" | sha256sum -c - \
    && unzip -q /tmp/pmd.zip -d /opt \
    && mv "/opt/pmd-bin-${PMD_VERSION}" /opt/pmd \
    && ln -s /opt/pmd/bin/pmd /usr/local/bin/pmd \
    && rm /tmp/pmd.zip

ENV HOME=/tmp/home
RUN mkdir -p /opt/code-analysis && { \
        printf 'google-java-format\t%s on %s\n' "${GJF_VERSION}" "$(java -version 2>&1 | head -n 1)"; \
        printf 'pmd\t%s\n' "$(pmd --version 2>/dev/null | grep -i 'pmd' | head -n 1)"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

# Test image for Java (step 5): JDK, Maven and Gradle to run the tests of
# Maven and Gradle builds (projects with a wrapper use mvnw/gradlew instead).
# Same JDK and build tool versions as the build for SonarQube in step 3.
# Definition: docs/testAnalysis/README.md
FROM eclipse-temurin:21-jdk-noble

ARG MAVEN_VERSION=3.9.16
ARG MAVEN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6
ARG GRADLE_VERSION=9.9.1
ARG GRADLE_SHA256=d8a18a7a841dd5df825d35a685fad6f2d7f12ac0c280e57b67c27b4a520ba228

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git unzip \
    && rm -rf /var/lib/apt/lists/*

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

# The containers run as the host user, so all caches live in /tmp or in the
# step folder (Maven repository and GRADLE_USER_HOME are set per run).
ENV HOME=/tmp/home \
    GRADLE_USER_HOME=/tmp/home/.gradle
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'java\t%s\n' "$(java -version 2>&1 | head -n 1)"; \
        printf 'maven\tMaven %s\n' "${MAVEN_VERSION}"; \
        printf 'gradle\tGradle %s\n' "${GRADLE_VERSION}"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

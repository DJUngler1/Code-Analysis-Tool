# SonarQube server (step 3): the official Community Build image plus the
# community plugin for C and C++, which the Community Build does not cover.
# Definition: docs/staticAnalysis/README.md
FROM sonarqube:26.9.0.129388-community

# sonar-cxx (LGPL-3.0), "Tested and released with ... SonarQube Community Build
# 26.9.0.129388" (release notes of 2.4.0-beta.1). Checksum: digest of the
# GitHub release asset.
ARG CXX_TAG=cxx-2.4.0-beta.1
ARG CXX_JAR=sonar-cxx-plugin-2.4.0.1689.jar
ARG CXX_SHA256=1de9a2df8070e94a394db909f8c9da9be794fedbebcaca8e18264c5e7ed352d5

# Plugins live in the image, not in a volume: a volume would keep an old
# plugin after an update of the image.
RUN curl -fsSL -o "/opt/sonarqube/extensions/plugins/${CXX_JAR}" \
        "https://github.com/SonarOpenCommunity/sonar-cxx/releases/download/${CXX_TAG}/${CXX_JAR}" \
    && echo "${CXX_SHA256}  /opt/sonarqube/extensions/plugins/${CXX_JAR}" | sha256sum -c -

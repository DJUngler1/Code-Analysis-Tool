# Linter/formatter image for C# (step 2): dotnet format from the .NET SDK.
# Definition: docs/linterConventions/README.md
FROM mcr.microsoft.com/dotnet/sdk:10.0.403

# The containers run as the host user, so all caches live in /tmp.
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    HOME=/tmp/home \
    DOTNET_CLI_HOME=/tmp/home \
    NUGET_PACKAGES=/tmp/nuget
RUN mkdir -p /opt/code-analysis \
    && printf 'dotnet format\t.NET SDK %s, dotnet format %s\n' "$(dotnet --version)" "$(dotnet format --version | head -n 1)" \
        > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

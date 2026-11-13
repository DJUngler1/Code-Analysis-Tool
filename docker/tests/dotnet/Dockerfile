# Test image for C# (step 5): dotnet test from the .NET 10 SDK, plus the
# .NET and ASP.NET Core runtimes 8.0 and 9.0, so that test projects targeting
# net8.0 or net9.0 run on the runtime they target (a test host needs the
# runtime of its target framework). Definition: docs/testAnalysis/README.md
FROM mcr.microsoft.com/dotnet/aspnet:8.0.34 AS runtime8
FROM mcr.microsoft.com/dotnet/aspnet:9.0.23 AS runtime9
FROM mcr.microsoft.com/dotnet/sdk:10.0.403

COPY --from=runtime8 /usr/share/dotnet/shared /usr/share/dotnet/shared
COPY --from=runtime9 /usr/share/dotnet/shared /usr/share/dotnet/shared

# The containers run as the host user, so all caches live in /tmp or in the
# step folder (NUGET_PACKAGES is set per run).
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1 \
    DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 \
    HOME=/tmp/home \
    DOTNET_CLI_HOME=/tmp/home \
    NUGET_PACKAGES=/tmp/nuget
RUN mkdir -p /opt/code-analysis \
    && { \
        printf 'dotnet\t.NET SDK %s\n' "$(dotnet --version)"; \
        printf 'runtimes\t%s\n' "$(dotnet --list-runtimes | awk '{print $1 " " $2}' | paste -sd ',' - | sed 's/,/, /g')"; \
    } > /opt/code-analysis/versions.tsv

# Caches created while building belong to root, but the containers run as the
# host user: start every run with an empty, writable /tmp.
RUN find /tmp -mindepth 1 -maxdepth 1 -exec rm -rf {} + \
    && mkdir -m 1777 /tmp/home

# Dependency audit

This document defines step 4 of the workflow, "Dependencies" (see `docs/toolflow_en.png`): the project's dependencies are checked for known vulnerabilities in two stages.
- **Stage 1:** OSV-Scanner.
- **Stage 2:** the audit of each package manager step 1 found.

All checks run in parallel; a check that fails doesn't stop the others.

**Network:** the checks query online services: OSV.dev, deps.dev (Maven), and the registries and advisory databases of the package managers. Package names and versions of the project leave the machine.

## Project copy and lock files

The files git knows are copied into `step4-dependencies/workspace/` as step 2 left them. All tools work on the copy; the project is not changed. The copy is deleted afterwards.

OSV-Scanner reads lock files, not plain manifests. A missing lock file is generated in the copy with the package manager's own command, without installing packages or running project scripts where the tool allows it:

| Package manager | Lock file | Command if missing | Image |
|---|---|---|---|
| npm | `package-lock.json` (also in a parent folder) | `npm install --package-lock-only --ignore-scripts` | `lint-node` |
| Yarn, pnpm, Bun | `yarn.lock`, `pnpm-lock.yaml`, `bun.lock` | – (step 1 detects them by their lock file) | – |
| Cargo | `Cargo.lock` of the top-most `Cargo.toml` | `cargo generate-lockfile` | `lint-rust` |
| NuGet | `packages.lock.json` per project | `dotnet restore <target> --use-lock-file` (each solution, else each `.csproj`) | `lint-dotnet` |
| Maven | – | – (OSV-Scanner resolves `pom.xml` transitively) | – |
| Gradle | `gradle.lockfile` per module | `gradle codeAnalysisLock --write-locks` with an init script (`gradlew` if present), see below | `sonar-scanner` |
| Conan | `conan.lock` | `conan lock create .` | `lint-cpp` |
| Poetry | `poetry.lock` | `poetry lock` | `audit-python` |
| Pipenv | `Pipfile.lock` | `pipenv lock` | `audit-python` |
| uv, PDM | `uv.lock`, `pdm.lock` | – (step 1 detects them by their lock file) | – |
| pip | `requirements*.txt` | – (read directly); `pyproject.toml`/`setup.py` alone: not read by OSV-Scanner | – |
| vcpkg, Conda | – | – (not covered) | – |

Generated lock files are copied to `step4-dependencies/lockfiles/`.

**Gradle:** the init script locks all configurations of all modules (`dependencyLocking { lockAllConfigurations() }`) and resolves the dependency graph of each one, without downloading artifacts.
- Because only the graph is resolved, configurations whose files would need e.g. the Android SDK are locked too.
- A configuration with a dependency that cannot be resolved (e.g. from a private repository the container cannot reach) is not locked. It is listed in the notes and the lock file's state is `partial`; the other configurations are still locked.

## Stage 1: OSV-Scanner

- **Version:** OSV-Scanner 2.7.1, the official image `ghcr.io/google/osv-scanner`, pinned by digest (`docker/dependencies/osv-scanner/Dockerfile`).
- **Command:** `osv-scanner scan source --recursive --format json --output-file <output>/osv-scanner.json <copy>`.
- **Exit codes:** 0 and 1 (vulnerabilities found) are `ok`; 128 (no packages found) is `skipped`; all others are `failed`.
- **Findings:** one per vulnerability and package. OSV-Scanner groups the ids of the same vulnerability (e.g. `PYSEC-…`, `GHSA-…`, `CVE-…`).

## Stage 2: audits

| Package manager | Command | Image |
|---|---|---|
| npm | `npm audit --json` | `lint-node` |
| Yarn 1 / Yarn 2+ | `yarn audit --json` / `yarn npm audit --all --recursive --json` (version from the project's `packageManager` field via corepack, else corepack's pinned default: Yarn 1.22.22, pnpm 11.24.0) | `lint-node` |
| pnpm | `pnpm audit --json` | `lint-node` |
| Bun | `bun audit --json` | `lint-node` |
| Cargo | `cargo audit --json` (cargo-audit 0.23.0) | `lint-rust` |
| NuGet | `dotnet restore <target>`, then `dotnet package list --project <target> --vulnerable --include-transitive --format json` | `lint-dotnet` |
| pip | `pip-audit -r <requirements file> -f json`, or `pip-audit . -f json` for `pyproject.toml` | `audit-python` |
| Poetry, uv, PDM, Pipenv | export with `poetry export`, `uv export --frozen`, `pdm export` or `pipenv requirements`, then `pip-audit -r <export> --no-deps --disable-pip -f json` | `audit-python` |
| Conan | `conan audit scan . --format=json`, only with `CONAN_AUDIT_TOKEN` in `docker/.env` | `lint-cpp` |
| Maven, Gradle, vcpkg, Conda | – (no audit of their own) | – |

`audit-python` (`docker/dependencies/python/Dockerfile`) contains pip-audit 2.10.1, uv 0.13.7, Poetry 2.5.1 with poetry-plugin-export 1.10.1, PDM 2.29.7 and Pipenv 2026.11.0, each in its own virtual environment.

## Coverage

| Ecosystem | OSV-Scanner | Audit |
|---|---|---|
| JavaScript/TypeScript (npm, Yarn, pnpm, Bun) | yes (not `bun.lockb`) | yes |
| Python (pip, Poetry, uv, PDM, Pipenv) | yes; `requirements.txt` best effort | pip-audit |
| Rust (Cargo) | yes | cargo-audit |
| C# (NuGet) | yes | `dotnet package list --vulnerable` |
| Java (Maven, Gradle) | yes | – |
| C/C++ (Conan) | reads `conan.lock`, but the OSV database has no Conan data | `conan audit` with a token |
| C/C++ (vcpkg), Conda | – | – |

**Unpinned `requirements.txt`:** for an entry like `requests>=2.0`, OSV-Scanner picks a version itself; it may not be the version that would be installed. pip-audit resolves the version pip would install.

## Findings

Each finding has:
- **Source:** tool, package manager, ecosystem, manifest or lock file (relative to the project).
- **Package:** name and version.
- **Advisory:** id and aliases, summary, fixed versions, URL.
- **Severity:** `critical`, `high`, `medium`, `low` or `unknown`.
  - It is taken from the CVSS score with the CVSS v3.x rating scale (0.1–3.9 low, 4.0–6.9 medium, 7.0–8.9 high, 9.0–10.0 critical), or from the advisory's label (`moderate` → `medium`).
  - pip-audit and cargo-audit report no severity: an audit finding takes the severity of OSV-Scanner's finding of the same advisory and package (`severity_source`).
  - It also takes OSV-Scanner's aliases, e.g. the CVE of an npm advisory.
- **Gradle scope:** the configurations of the lock file that contain the package, and the most shipped of their scopes:
  - `runtime`: `*RuntimeClasspath`, e.g. `runtimeClasspath`, `releaseRuntimeClasspath`.
  - `compile`: `*CompileClasspath`.
  - `test`: every configuration with "test" in its name.
  - `tooling`: everything else, e.g. `annotationProcessor` and plugin configurations such as Gretty's server runners.

OSV-Scanner and the audits use largely the same advisory databases, so the same vulnerability usually appears more than once (once per tool). The aliases make the duplicates matchable; they are merged in step 6.

## Status

| Status | Meaning |
|---|---|
| `ok` | The check ran and its output was read |
| `skipped` | OSV-Scanner found no packages |
| `failed` | The check failed or its output could not be read (see the notes) |

A lock file that could not be generated is listed in the coverage as `failed`; a Gradle lock file without some configurations as `partial`. Both make step 4 incomplete (exit code 7): dependencies were not checked.

## Results

Step 4 writes into the run folder of step 2: `OUTPUT_DIR/<project>/<date_time>Z/step4-dependencies/`.

| Path | Content |
|---|---|
| `summary.json` | Coverage per manifest (lock file committed, generated, partial, failed or not supported; which tools covered it); per check: tool, package manager, manifest, version, commands, exit codes, status, duration, findings per severity, notes; Gradle findings per scope |
| `findings.json` | Findings of all checks in one format |
| `osv-scanner/` | OSV-Scanner's JSON report and logs |
| `audits/<nn>-<manager>/` | Raw output of each audit |
| `lockfiles/` | The generated lock files |
| `lockfile-logs/` | Logs of the lock commands |

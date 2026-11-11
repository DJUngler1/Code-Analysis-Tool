# Static code analysis

This document defines step 3 of the workflow, "Static code analysis" (see `docs/toolflow_en.png`): [OpenGrep](#opengrep) and [SonarQube Community Build](#sonarqube) run in parallel. If one of them fails, the other still runs.

As in step 2, the tools run with fixed settings and ignore the analysed project's own configuration, so that results of different projects are comparable. The analysis reads the working tree as step 2 left it (formatted, uncommitted), so line numbers match `git diff` and the findings of step 2.

## OpenGrep

OpenGrep matches rules (Semgrep rule format) against the code, including taint rules that follow data from a source (e.g. a request parameter) to a sink (e.g. a shell command).

- **Version:** OpenGrep 1.33.1 (release binary, checked against its SHA-256 digest).
- **Image:** `code-analysis/opengrep` (`docker/static-analysis/opengrep/Dockerfile`). It contains the binary and the active [rule sets](#rule-sets).
- **Container:** no network, the project mounted read-only, the output folder writable.

### Command

```bash
opengrep scan \
  --config <rule folder> [--config <rule folder> …] \
  --taint-intrafile \
  --disable-version-check \
  --json-output=<output>/opengrep.json \
  --sarif-output=<output>/opengrep.sarif \
  .
```

It runs in the project folder with `SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE=/opt/code-analysis/opengrep/default.semgrepignore`.

| Option | Purpose |
|---|---|
| `--config <rule folder>` | One per rule folder of the active rule sets that covers a language of the project (primary language and every other supported language of step 1) |
| `--taint-intrafile` | Follows taint across functions within a file (not across files) |
| `--disable-version-check` | No request to the internet |
| `--json-output`, `--sarif-output` | Raw reports; the JSON report is the source of `findings.json` |
| `SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE` | OpenGrep's own default ignore list instead of the project's `.semgrepignore` |

### What is scanned

- **Files known to git.** `.gitignore` applies.
- **Not scanned:** OpenGrep's default ignore list: `build/`, `vendor/`, `dist/`, `*.min.js`, `.env/`, `.tox/`, `node_modules/`, `.npm/`, `.yarn/`, `.venv/`, `_opam/`, `_build/`, `_cargo/`, `test/`, `tests/`, `testsuite/`, `*_test.go`.
- **Generated files:** findings in [generated files](../linterConventions/README.md#generated-files) are dropped, as in step 2.
- **Project configuration is ignored:** the project's `.semgrepignore` and rule files (e.g. `.semgrep.yml`) are not read.
- **Suppressions in the code** (`// nosemgrep`, `# nosem`) are respected.

### Status

| Status | Meaning |
|---|---|
| `ok` | OpenGrep finished and wrote its report |
| `partial` | As `ok`, but some files could not (fully) be analysed, e.g. syntax errors or timeouts (listed in the notes) |
| `failed` | Non-zero exit code or no report, or the image doesn't match `config/opengrep.toml` (rebuild the image) |
| `skipped` | No active rule set covers any language of the project |

## Rule sets

OpenGrep ships no rules. The rule sets are defined in [`config/opengrep.toml`](../../config/opengrep.toml): each is a git repository at a fixed commit, with the rule folders that cover each language of step 1. `rulesets` under `[opengrep]` lists the active ones.

| Rule set | Repository | Languages | License |
|---|---|---|---|
| `opengrep-rules` | [opengrep/opengrep-rules](https://github.com/opengrep/opengrep-rules), fork of semgrep-rules from 2024-12-13 | C, C#, Java, JavaScript, TypeScript, Python, Rust | LGPL 2.1 with Commons Clause |
| `semgrep-rules` | [semgrep/semgrep-rules](https://github.com/semgrep/semgrep-rules) | C, C#, Java, JavaScript, TypeScript, Python, Rust | Semgrep Rules License v1.0 |
| `0xdea-semgrep-rules` | [0xdea/semgrep-rules](https://github.com/0xdea/semgrep-rules) | C, C++ | MIT |

- **All rules of a folder run,** without a filter on the category. Every finding keeps the rule's category, subcategory, CWE, OWASP, confidence, likelihood and impact.
- **C++** is covered only by `0xdea-semgrep-rules`; the C rules of the other two sets apply only to `.c` and `.h` files.

### Switching and adding rule sets

Only the active rule sets are downloaded into the image, when it is built.

- **Switch:** change `rulesets` in `config/opengrep.toml`, then rebuild the image:
  ```bash
  docker compose -f docker/docker-compose.yml --profile tools build opengrep
  ```
- **Add:** add a `[rulesets.<name>]` table (`repository`, `commit`, `license`, `folders` per language, optional `exclude`), activate it and rebuild the image.
- **Build:** it fetches each active repository at its commit, keeps the rule files of the listed folders plus the license and readme files, and fails if OpenGrep rejects a rule. Rejected files can be listed in `exclude`.
- **Mismatch:** if an active rule set is missing in the image or at another commit, OpenGrep stops with a note to rebuild the image.

### Licenses

- The repository contains no rules, only repository URLs and commits. The rules are only in the local Docker image.
- An image built with `semgrep-rules` must not be pushed to a registry or passed on.
- Results contain parts of the rules: the findings carry each rule's message, and `opengrep.sarif` the texts of all loaded rules. Results produced with `semgrep-rules` must not be published or passed on.

## SonarQube

SonarQube Community Build finds bugs, code smells, vulnerabilities and duplications, and measures the code (lines of code, complexity). The server stores and evaluates the analyses; a scanner analyses the code and sends the report to the server.

| Component | Version | Image |
|---|---|---|
| Server | SonarQube Community Build 26.9.0.129388 with sonar-cxx 2.4.0-beta.1 (C, C++) | `code-analysis/sonarqube` (`docker/static-analysis/sonarqube/Dockerfile`) |
| Database | PostgreSQL 18.6 | `postgres:18.6` |
| SonarScanner CLI | 8.3.1.16491, with JDK 21, Maven 3.9.16, Gradle 9.9.1, Node.js 24.22.3 | `code-analysis/sonar-scanner` |
| SonarScanner for .NET | 12.1.0, with .NET SDK 10.0.403, Node.js 24.22.3 | `code-analysis/sonar-dotnet` |

**Languages:** C#, Java, JavaScript, TypeScript, Python and Rust use the analyzers of the Community Build; C and C++ use the community plugin sonar-cxx.

### Server

- `sonarqube` and `sonarqube-db` start with `docker compose … up -d` and keep running; their data stays in Docker volumes.
- Web interface: `http://localhost:9000` (only from this machine), user `admin`, password `SONARQUBE_ADMIN_PASSWORD` from `docker/.env`.
- On the first run the scheduler replaces SonarQube's default password with `SONARQUBE_ADMIN_PASSWORD`. Each analysis gets its own token, revoked at the end.

### How a project is analysed

1. **Project:** `code-analysis_<project folder>` in SonarQube, created on the first analysis; it keeps its history.
2. **Copy:** the files git knows are copied into `step3-static-analysis/sonarqube/workspace/`. Builds and scanners work only on the copy, so the project is not changed. The copy is deleted afterwards.
3. **Java** is compiled first in the `sonar-scanner` image. The project's wrapper (`mvnw`, `gradlew`) is used if present, else Maven or Gradle:
   - Maven: `mvn -B -q -DskipTests compile dependency:build-classpath`.
   - Gradle: `gradle --no-daemon -q classes`, plus an init script that writes the classpath.

   The bytecode and the classpath become `sonar.java.binaries` and `sonar.java.libraries`.
4. **Scanner:**
   - **Projects with C#:** SonarScanner for .NET runs `begin`, `dotnet build` (each solution, else each `.csproj`) and `end`. It also analyses the other languages.
   - **Other projects:** SonarScanner CLI.
5. **Results:** the scheduler waits until the server has processed the report, then fetches the results through the Web API.

### Parameters

The CLI scanner gets its parameters in the run's `sonarqube/sonar-project.properties` (`project.settings`); the .NET scanner gets the same values as switches. The project's own `sonar-project.properties` is ignored.

| Parameter | Value |
|---|---|
| `sonar.projectKey`, `sonar.projectName`, `sonar.projectVersion` | `code-analysis_<folder>`, folder name, commit |
| `sonar.sourceEncoding` | `UTF-8` |
| `sonar.scm.disabled` | `true` (the copy has no history) |
| `sonar.exclusions` | the folders step 1 skips (`vendor`, `third_party`, `build`, `node_modules`, …), hidden folders, generated files |
| `sonar.cxx.file.suffixes` | `.c,.h,.cc,.cpp,.cxx,.c++,.hh,.hpp,.hxx,.h++,.ipp` (switches on sonar-cxx) |
| `sonar.rust.clippy.enabled` | `false` (Clippy runs in step 2) |
| `sonar.cs.roslyn.ignoreIssues` | `true` for C# (compiler and .NET analyzer warnings are covered by step 2) |
| `sonar.python.version` | lower bound of `requires-python`, if set |
| `sonar.java.source` | Java version of the build file, if set |
| `sonar.java.binaries`, `sonar.java.libraries` | bytecode and classpath of the Java build |
| `sonar.working.directory` | `/tmp/scannerwork` in the container |
| `sonar.projectBaseDir`, `sonar.scanner.scanAll` | the copy, `true` (.NET scanner only) |

### Quality profiles

- **Default:** SonarQube's built-in "Sonar way" of each language, unchanged.
- **C and C++:** sonar-cxx's own "Sonar way" activates no rule. The scheduler therefore uses the profile **"Sonar way + sonar-cxx rules"**, a copy with all of sonar-cxx's own rules except rule templates and `cxx:FileHeader`: 22 rules on complexity, size, naming and layout. The profile is checked on every run.
- **Rules of other tools** (clang-tidy, cppcheck, …) stay off; no reports are imported.

### Status

| Status | Meaning |
|---|---|
| `ok` | Analysis processed by the server, results fetched |
| `partial` | As `ok`, but Java was analysed without bytecode (build failed) or C# was not analysed (`dotnet build` failed) |
| `failed` | The server is not up, the password is missing or wrong, the scanner failed, or the server could not process the report |

### Findings

- **Issues** come from `api/issues/search` (unresolved ones), security hotspots from `api/hotspots/search` (to review).
- **Severity:** highest impact severity: `BLOCKER`/`HIGH` → `error`, `MEDIUM` → `warning`, `LOW`/`INFO` → `note`.
- **Category:** the impact's software quality (`security`, `reliability`, `maintainability`).
- **Subcategory:** the issue type (`bug`, `vulnerability`, `code_smell`) or `hotspot`.
- **CWE and OWASP Top 10 2021** per rule; the rule set is the quality profile.

## Results

Step 3 writes into the run folder of step 2: `OUTPUT_DIR/<project>/<date_time>Z/step3-static-analysis/`.

| Path | Content |
|---|---|
| `summary.json` | One entry per tool: version, commands, exit codes, status, duration, rule sets or quality profiles, findings per severity and category, SonarQube's metrics and details (plugin and scanner versions, duplicated files, web interface link), notes |
| `findings.json` | Findings of both tools: tool, rule set, rule, severity (`error`/`warning`/`note`) and the tool's severity, message, language, file, start and end position, category, subcategory, CWE, OWASP, confidence, likelihood, impact, fingerprint |
| `opengrep/` | OpenGrep's JSON and SARIF reports, stdout/stderr |
| `sonarqube/sonar-project.properties` | The parameters of the CLI scanner |
| `sonarqube/*.stdout.txt`, `*.stderr.txt` | Logs of the Java build and the scanner |
| `sonarqube/api/` | Web API answers: issues, hotspots, project metrics, metrics and duplications per file, rule metadata, quality profiles, plugins, processing task |

OpenGrep's severities map as `ERROR` → `error`, `WARNING` → `warning`, `INFO` → `note`. Its rule ids are shortened by the image path, e.g. `python.flask.security.injection.subprocess-injection`.

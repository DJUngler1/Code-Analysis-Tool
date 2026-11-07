# Linter and formatter definitions

This document defines the tools for step 2 of the workflow, "Install linter / formatter: fixed rules per language" (see `docs/toolflow_en.png`).

Each language uses the default style of an established formatter and linter, with as little configuration as possible. The analysed project's own configuration files are always ignored, so that results of different projects are comparable.

## Overview

| Language | Tools | Commands, in this order |
|---|---|---|
| C# | dotnet format | `dotnet format` |
| Java | google-java-format, PMD | `java -jar google-java-format-<version>-all-deps.jar --replace <files>`, then `pmd check … --use-version java-<project version>` |
| JavaScript, TypeScript | Biome | `biome check --write --config-path=<project>/.code-analysis-biome.json --vcs-use-ignore-file=true …` |
| Python | Ruff | `ruff check --fix --isolated --target-version py3X`, then `ruff format --isolated --target-version py3X` |
| C, C++ | clang-format, clang-tidy | `clang-format -i --style=…`, then `run-clang-tidy … -config=…` |
| Rust | Clippy, rustfmt | `CLIPPY_CONF_DIR=<tool>/clippy cargo clippy --fix --allow-dirty --allow-no-vcs`, then `cargo fmt -- --config-path <tool>/rustfmt` |

Each tool runs in a fresh container of its language image (`docker/linters/<language>/Dockerfile`, versions in [Versions](#versions)). Raw output and reports go to the run's output folder; The main [README](../../README.md) describes the results layout.

**Which languages:** every supported language found in step 1, however small its share. A language's tools are skipped only if git knows none of its files (outside the folders listed below); Biome would otherwise format the project's JSON and CSS files without any JavaScript or TypeScript to check.

**Files the formatters work on:** tools that take a file list (google-java-format, clang-format, clang-tidy) get the project's files known to git (tracked, or untracked but not ignored). Files in the folders step 1 skips are left out: hidden folders and `vendor/`, `third_party/`, `build/`, `dist/`, `target/`, `out/`, `bin/`, `obj/`, `node_modules/`, virtual environments.

Placeholders:
- `<project>` is the folder of the analysed project.
- `<output>` is the tool's output folder, outside the project.
- `<tool>` is a folder in the tool's container that holds the tool's own, empty configuration files.

## C#

- **Tool:** `dotnet format`
- **Command:** `dotnet format <target> --report <output>`, once per target:
  - every solution file (`.sln`, `.slnx`) found in step 1;
  - without solution files, every project file (`.csproj`).
- **Configuration:** none. Microsoft's default .NET formatting and code-style preferences apply. The project's `.editorconfig` files are hidden during the run, including ones above the project folder inside the same git repository, because dotnet format reads those too.
- **Report:** `format-report.json` lists the fixes dotnet format applied. Analyzer and code-style fixes (e.g. `CA2200`) become findings marked as fixed; pure formatting entries (`WHITESPACE`, `FINALNEWLINE`, …) are only visible in the patch.

## Java

- **Tools:** google-java-format (formatter), then PMD (linter).
- **Commands:**
  ```bash
  java -jar google-java-format-<version>-all-deps.jar --replace <all .java files>

  pmd check -d <project> --exclude <every target/ and build/ folder> \
    -R rulesets/java/quickstart.xml --use-version java-<project version> \
    --no-fail-on-violation --no-cache --no-progress \
    -f sarif -r <output>/pmd.sarif
  ```
- **Configuration:** none.
  - google-java-format applies the Google Java Style.
  - PMD uses its built-in `quickstart` rule set.
- **Excluded files:** every `target/` and `build/` folder, including those of sub-modules (Maven/Gradle build output).
- **Report:** `pmd.sarif` lists the findings and the files PMD could not parse. Those files are listed in the tool's report, and the status is `partial`.

### Java version for PMD

The tool reads the Java version from the project's build file:

| Build file | Where the version comes from (first match wins) |
|---|---|
| `pom.xml` (Maven) | `maven.compiler.release` or `<release>` of the maven-compiler-plugin; otherwise `maven.compiler.source` or `<source>` |
| `build.gradle`, `build.gradle.kts` (Gradle) | `java { toolchain { languageVersion = JavaLanguageVersion.of(N) } }`, `options.release = N`, or `sourceCompatibility` |

- **Version `N` found:** the tool passes `--use-version java-N`.
- **Preview features enabled** (`--enable-preview`): the tool passes `java-N-preview`.
- **No version found:** `--use-version` is left out, and PMD uses its default version.
- **Version unknown to PMD:** the tool runs PMD again without `--use-version` and notes this in its report.

## JavaScript and TypeScript

- **Tool:** Biome
- **Command:**
  ```bash
  biome check --write \
    --config-path=<project>/.code-analysis-biome.json \
    --vcs-enabled=true --vcs-client-kind=git --vcs-use-ignore-file=true \
    --reporter=sarif --reporter-file=<output>/biome.sarif .
  ```
- **Installation:** Biome is installed in the image at the pinned version (`npm install -g @biomejs/biome@<version>`), so a run downloads nothing; this replaces `npx @biomejs/biome@<version>`.
- **Configuration:** none. Biome's defaults apply.
  - Before the run, the tool moves all `biome.json` and `biome.jsonc` files out of the project.
  - It then writes its own `.code-analysis-biome.json`, containing only `{}`, into the project root.
  - After the run, it deletes that file and moves the project's files back.
- **Excluded files:** files and folders listed in the project's `.gitignore` files (including nested ones) are not checked, for example build output.
- **Report:** `biome.sarif` lists every finding Biome could not fix.
  - Biome exits with code 1 when errors remain, so the exit code alone doesn't show whether the run succeeded.
  - The run counts as successful when the report file was written.

## Python

- **Tool:** Ruff
- **Commands:**
  ```bash
  ruff check --fix --isolated --target-version py3X --output-format sarif --output-file <output>/ruff.sarif .
  ruff format --isolated --target-version py3X .
  ```
- **Configuration:** none. Ruff's default rules and default style apply.
- **Python version:** `py3X` is the lower bound of `requires-python` in the project's `pyproject.toml` (or of Poetry's `python = "^3.x"`); for example, `>=3.8` becomes `py38`. Versions below 3.7 become `py37`, the lowest Ruff supports. If no version is found, or Ruff doesn't know the version, `--target-version` is left out and this is noted in the report.
- **Exit codes:** `ruff check` exits with 1 when findings remain after the fixes; that is the normal case. `ruff format` exits with 2 when a file has a syntax error; that file stays unformatted and the status is `partial`.

## C and C++

### Formatter: clang-format

The base is the LLVM style, with these changes:

| Option | Value |
|---|---|
| `ColumnLimit` (line length) | 100 |
| `IndentWidth` | 4 |
| `AccessModifierOffset` | -4 |
| `SortIncludes` | `false` |

```bash
clang-format -i --style="{BasedOnStyle: LLVM, ColumnLimit: 100, IndentWidth: 4, AccessModifierOffset: -4, SortIncludes: false}" <files>
```

The project's `.clang-format-ignore` files are hidden during the run.

### Linter: clang-tidy

clang-tidy runs after clang-format, without `--fix`. It only reports findings.

C, and C++ before C++11:

```bash
run-clang-tidy -p <database> \
  -config="{Checks: '-*,bugprone-*,cert-*,clang-analyzer-*,performance-*'}"
```

C++11 and later additionally enable `modernize-*`:

```bash
run-clang-tidy -p <database> \
  -config="{Checks: '-*,bugprone-*,cert-*,clang-analyzer-*,performance-*,modernize-*'}"
```

- **Language standard:** taken per file from the `-std=` flag in the compilation database. C files and C++ files with `c++98`/`c++03` get the first list; C++ files without a flag get the second, because clang's default is C++17. Each group gets its own database with only its entries, and `run-clang-tidy` (part of clang-tidy) runs it in parallel.
- **Files:** only the project's own source files from the database; findings in system and third-party headers are dropped, and a header's finding is counted once even if several files include it.
- **Output:** the tool stores stderr together with stdout, because they contain the counts of suppressed warnings (for example, `NOLINT`). The count goes into the report.

### Compilation database

clang-tidy needs the project's `compile_commands.json` (include paths, macros, language standard). The tool gets it in this order:

1. **Existing file:** `compile_commands.json` in the project folder, in `build/` or in `cmake-build-*/`, if the files it lists exist (a database from another machine is not usable).
2. **Generated:** if the project folder has a `CMakeLists.txt`, the tool runs CMake configure (no build) into `<output>/cpp-build/`, outside the project:
   - **Conan** (`conanfile.txt`/`conanfile.py`): `conan profile detect`, `conan install … --build=missing` (adding the generators `CMakeDeps`/`CMakeToolchain` if the conanfile doesn't declare them), then CMake with `conan_toolchain.cmake`. The Conan cache lies in the build folder, because clang-tidy runs in a new container and needs the dependencies' headers at the paths in the database.
   - **vcpkg** (`vcpkg.json`): CMake with the vcpkg toolchain file (manifest mode); vcpkg installs the dependencies into the build folder.
   - Otherwise plain CMake configure.
3. **None:** clang-tidy runs on each source file with default flags (`-std=c11` for C, `-std=c++17` for C++, `-I` for the project folder and every folder with header files). The report notes that the results are limited; missing headers show up as `clang-diagnostic-error` findings.

When clang-tidy reports compiler errors, the status is `partial`.

## Rust

- **Tools:** Clippy (linter), then rustfmt (formatter).
- **Command:**
  ```bash
  CLIPPY_CONF_DIR=<tool>/clippy cargo clippy --fix --allow-dirty --allow-no-vcs
  CLIPPY_CONF_DIR=<tool>/clippy cargo clippy --message-format=json
  cargo fmt -- --config-path <tool>/rustfmt
  ```
- **Configuration:** none. `<tool>/clippy/clippy.toml` and `<tool>/rustfmt/rustfmt.toml` are empty, so the defaults apply.
- **Where:** in the folder of every top-most `Cargo.toml` (a workspace covers its members).
- **Findings:** the second Clippy run collects what remains after the fixes; it reuses the build of the first run. `--allow-no-vcs` lets Clippy fix files when the container doesn't see the git repository (project below the repository's root outside `PROJECTS_DIR`).

## Project configuration

The analysed project's configuration files are always ignored:

| Tool | Project configuration | How it is ignored |
|---|---|---|
| dotnet format | `.editorconfig` | file hidden during the run |
| google-java-format | none | – |
| PMD | none | – |
| Biome | `biome.json`, `biome.jsonc` (including nested ones) | files hidden during the run; the tool's own file is passed with `--config-path` |
| Biome | `.editorconfig` | not read with the tool's configuration |
| Ruff | `pyproject.toml` (`[tool.ruff]`), `ruff.toml`, `.ruff.toml` | `--isolated` |
| clang-format | `.clang-format` | inline `--style` |
| clang-format | `.clang-format-ignore` | file hidden during the run |
| clang-tidy | `.clang-tidy` | `--config` |
| Clippy | `clippy.toml`, `.clippy.toml` | `CLIPPY_CONF_DIR=<tool>/clippy` |
| rustfmt | `rustfmt.toml` | `--config-path <tool>/rustfmt` |

"Hidden during the run" means the tool moves these files out of the project before the tools run and moves them back afterwards, also if a tool fails.

Exception: Biome reads the project's `.gitignore` files, so that folders git ignores, such as build output, are not formatted.

Some project settings can't be ignored without editing project files:
- analyzer settings in .NET project files (`<AnalysisMode>`, `<AnalysisLevel>`, `<EnforceCodeStyleInBuild>`) and analyzer packages (StyleCop, Roslynator, Sonar, Meziantou, NetAnalyzers) – noted in the report with the file;
- the `[lints]` or `[workspace.lints]` table in `Cargo.toml` – noted in the report;
- suppressions in the code itself, such as `// NOLINT`, `// NOPMD`, `# noqa` or `#[allow(...)]` – they belong to the code; only clang-tidy reports how many findings `NOLINT` suppressed.

## Versions

| Tool | Version | Image (`docker/linters/…`) |
|---|---|---|
| clang-format, clang-tidy | 22.1.8 | `cpp`: `debian:trixie-slim`, with CMake 3.31.6, Conan 2.34.0, vcpkg 2026.10.16 |
| .NET SDK (`dotnet format`) | 10.0.403 | `dotnet`: `mcr.microsoft.com/dotnet/sdk:10.0.403` |
| google-java-format | 2.0.0 | `java`: `eclipse-temurin:27-jdk` (the newest JDK) |
| PMD | 7.29.0 | `java` |
| Biome | 2.6.6 | `node`: `node:22.23.4-slim` |
| Ruff | 0.17.4 | `python`: `python:3.13.18-slim` |
| Rust toolchain (Clippy, rustfmt) | 1.99.1 | `rust`: `rust:1.99.1-slim` |

Each image records the exact versions in `/opt/code-analysis/versions.tsv`; the report shows them per tool.

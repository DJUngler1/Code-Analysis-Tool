# Linter and formatter definitions

This document defines the tools for step 2 of the workflow, "Install linter / formatter: fixed rules per language" (see `docs/toolflow_en.png`).

Each language uses the default style of an established formatter and linter, with as little configuration as possible. The analysed project's own configuration files are always ignored, so that results of different projects are comparable.

## Overview

| Language | Tools | Commands, in this order |
|---|---|---|
| C# | dotnet format | `dotnet format` |
| Java | google-java-format, PMD | `java -jar google-java-format-<version>-all-deps.jar --replace <files>`, then `pmd check … --use-version java-<project version>` |
| JavaScript, TypeScript | Biome | `npx @biomejs/biome@<version> check --write --config-path=<project>/.code-analysis-biome.json --vcs-use-ignore-file=true …` |
| Python | Ruff | `ruff check --fix --isolated --target-version py3X`, then `ruff format --isolated --target-version py3X` |
| C, C++ | clang-format, clang-tidy | `clang-format -i --style=…`, then `clang-tidy … --config=…` |
| Rust | Clippy, rustfmt | `CLIPPY_CONF_DIR=<tool>/clippy cargo clippy --fix --allow-dirty`, then `cargo fmt -- --config-path <tool>/rustfmt` |

Placeholders:
- `<project>` is the folder of the analysed project.
- `<output>` is the tool's output folder, outside the project.
- `<tool>` is a folder in the tool's container that holds the tool's own, empty configuration files.

## C#

- **Tool:** `dotnet format`
- **Command:** `dotnet format`, run in the folder that contains the solution or project file.
- **Configuration:** none. Microsoft's default .NET formatting and code-style preferences apply. The project's `.editorconfig` files are hidden during the run.

## Java

- **Tools:** google-java-format (formatter), then PMD (linter).
- **Commands:**
  ```bash
  java -jar google-java-format-<version>-all-deps.jar --replace <all .java files>

  pmd check -d <project> --exclude <project>/target --exclude <project>/build \
    -R rulesets/java/quickstart.xml --use-version java-<project version> \
    --no-fail-on-violation --no-cache --no-progress \
    -f sarif -r <output>/pmd.sarif
  ```
- **Configuration:** none.
  - google-java-format applies the Google Java Style.
  - PMD uses its built-in `quickstart` rule set.
- **Report:** `pmd.sarif` lists the findings and the files PMD could not parse. Those files are listed in the tool's report.

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
  npx @biomejs/biome@<version> check --write \
    --config-path=<project>/.code-analysis-biome.json \
    --vcs-enabled=true --vcs-client-kind=git --vcs-use-ignore-file=true \
    --reporter=sarif --reporter-file=<output>/biome.sarif .
  ```
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
  ruff check --fix --isolated --target-version py3X .
  ruff format --isolated --target-version py3X .
  ```
- **Configuration:** none. Ruff's default rules and default style apply.
- **Python version:** `py3X` is the lower bound of `requires-python` in the project's `pyproject.toml`; for example, `>=3.8` becomes `py38`. If no version is found, `--target-version` is left out.

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
clang-tidy file.c -p build/ \
  --config="{Checks: '-*,bugprone-*,cert-*,clang-analyzer-*,performance-*'}"
```

C++11 and later additionally enable `modernize-*`:

```bash
clang-tidy file.cpp -p build/ \
  --config="{Checks: '-*,bugprone-*,cert-*,clang-analyzer-*,performance-*,modernize-*'}"
```

The tool stores stderr together with stdout, because stderr contains the counts of suppressed warnings (for example, `NOLINT`).

## Rust

- **Tools:** Clippy (linter), then rustfmt (formatter).
- **Command:**
  ```bash
  CLIPPY_CONF_DIR=<tool>/clippy cargo clippy --fix --allow-dirty && cargo fmt -- --config-path <tool>/rustfmt
  ```
- **Configuration:** none. `<tool>/clippy/clippy.toml` and `<tool>/rustfmt/rustfmt.toml` are empty, so the defaults apply.

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

Some project settings can't be ignored without editing project files. The tool notes in its report when one of them is present:
- analyzer settings in .NET project files, such as `<AnalysisMode>`;
- the `[lints]` table in `Cargo.toml`;
- suppressions in the code itself, such as `// NOLINT`, `// NOPMD`, `# noqa` or `#[allow(...)]`.

## Versions

| Tool | Version |
|---|---|
| clang-format, clang-tidy | 22.1.8 |
| .NET SDK (`dotnet format`) | 10.0.403 |
| google-java-format | 2.0.0 (on the newest JDK) |
| PMD | 7.29.0 |
| Biome | 2.6.6 (on Node 22) |
| Ruff | 0.17.4 |
| Rust toolchain (Clippy, rustfmt) | 1.99.1 |

# Linter and formatter definitions

This document defines the tools for step 2 of the workflow, "Install linter / formatter: fixed rules per language" (see `docs/toolflow_en.png`). For every supported language, the tool runs one fixed formatter and linter on the accepted project.

The approach: each language uses the **default style of an established formatter or linter**, with as little configuration of our own as possible. The only exception is C/C++, where three clang-format options differ from the LLVM style.

The formatters change files in place. Step 1 therefore only accepts projects with a clean git working tree, so every change can be reviewed with `git diff` and reverted.

## Overview

| Language | Tools | Commands, in this order | Own configuration |
|---|---|---|---|
| C# | dotnet format | `dotnet format` | none |
| Java | google-java-format, PMD | `java -jar google-java-format-<version>-all-deps.jar --replace <files>`, then `pmd check … --use-version java-<project version>` | none (project's Java version is read from the build file) |
| JavaScript, TypeScript | Biome | `npx @biomejs/biome@<version> check --write --reporter=sarif --reporter-file=<output>/biome.sarif .` | none |
| Python | Ruff | `ruff check --fix`, then `ruff format` | none |
| C, C++ | clang-format, clang-tidy | `clang-format -i --style=…`, then `clang-tidy …` | 3 clang-format options, clang-tidy check list |
| Rust | Clippy, rustfmt | `cargo clippy --fix --allow-dirty`, then `cargo fmt` | none |

Each section below gives the definition first, followed by the behaviour verified with the tool versions listed in [Tested versions](#tested-versions) (tests run on 2026-11-07). References in brackets, such as [MS-format], point to [Sources](#sources).

## C#

- **Tool:** `dotnet format`, which ships with the .NET SDK.
- **Command:** `dotnet format`, run in the folder that contains the solution or project file (or `dotnet format <solution|project>`).
- **Configuration:** none. Without an `.editorconfig`, `dotnet format` applies Microsoft's default .NET formatting and code-style preferences.

`dotnet format` formats the code and directly applies the code fixes of the .NET analyzers.

Verified behaviour:
- **What it does.** The documentation describes it as "a code formatter that applies style preferences and static analysis recommendations to a project or solution. Preferences will be read from an *.editorconfig* file, if present, otherwise a default set of preferences will be used." [MS-format]
- **Which fixes it applies.** It only fixes diagnostics with severity *warning* or higher (`--severity`, default `warn`) [MS-format]. Without configuration, few rules reach that severity:
  - code-style rules (`IDExxxx`) are disabled on command-line builds by default;
  - in the default analysis mode, "only a small number" of code-quality rules (`CAxxxx`) are warnings or errors: 29 in .NET 10 [MS-analysis].
- **Test result.** The whitespace was reformatted: braces on their own lines, 4-space indentation, spaces around operators. `throw e;` was changed to `throw;` (CA2200). The unused `using` directives were *not* removed. Without configuration, `dotnet format` therefore works mainly as a formatter plus a small set of analyzer fixes. It doesn't fix everything a linter would report.
- **Check-only mode.** `dotnet format --verify-no-changes` only checks and returns a non-zero exit code if files would change [MS-format].
- **It builds the project.** "dotnet format may restore, compile, and run analyzers from the specified project or solution. Only invoke the tool against trusted code." [MS-format] It therefore runs inside the tool's container.

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
  - google-java-format applies the Google Java Style Guide [Google-style].
  - PMD uses its built-in `quickstart` rule set. The only project-specific value is the Java version.
- **Report:** `<output>` is the tool's output folder. The SARIF report lists PMD's findings and the files PMD could not parse.

### Java version for PMD

PMD has to parse the code with the Java version the project is written for. The tool reads that version from the build file that step 1 found:

| Build file | Where the version comes from (first match wins) |
|---|---|
| `pom.xml` (Maven) | `maven.compiler.release` or `<release>` of the maven-compiler-plugin; otherwise `maven.compiler.source` or `<source>` [Maven-compiler] |
| `build.gradle`, `build.gradle.kts` (Gradle) | `java { toolchain { languageVersion = JavaLanguageVersion.of(N) } }`, `options.release = N`, or `sourceCompatibility` [Gradle-toolchains] |

How the version is turned into `--use-version`:
- **Value found.** The version `N` becomes `--use-version java-N`. The old notation `1.8` can be passed as it is (`java-1.8`); PMD accepts both forms.
- **Preview features.** If the build enables preview features (`--enable-preview`), the tool uses `java-N-preview`. PMD only supports preview features "for the latest two java versions" [PMD-java].
- **No value found.** `--use-version` is left out, and PMD uses its default: "Usually the latest non-preview Java Version is the default version." [PMD-java] In PMD 7.29.0 that is Java 27. This also happens when the version is set indirectly, for example in a parent POM or through a variable.
- **Version newer than PMD knows.** PMD rejects it as a usage error and writes no report (tested with `java-28`). The tool then runs again without `--use-version` and notes this in its report.

### Verified behaviour

google-java-format:
- **What it does.** "google-java-format is a program that reformats Java source code to comply with Google Java Style." It is "an opinionated formatter with very little configuration" [GJF]. The only style alternative, `--aosp` (4-space indentation), is not used.
- **Imports.** By default it also sorts imports and removes unused imports (they can be switched off with `--skip-sorting-imports` and `--skip-removing-unused-imports`) [GJF].
- **Test result.** The tool applied 2-space indentation, one statement per line and braces, and removed the unused import `java.util.Map`. An unused local variable stayed.
- **Formatter only.** google-java-format reports no rule violations; Java's lint findings come from PMD.
- **Requirements.** It needs a JDK, not a JRE, of at least Java 21 and "equal to or newer than the Java language version of the files being formatted" [GJF]. The Java container therefore uses the newest JDK, not the JDK 21 used in these tests.

PMD:
- **Rule set.** `quickstart` is PMD's built-in rule set: "Quickstart configuration of PMD. Includes the rules that are most likely to apply everywhere." [PMD-rules]
- **Test result.** On the formatted sample file, PMD reported 7 findings, among them an unused private field, strings compared with `==`, and a `FileReader` that is never closed. Checkstyle with its built-in Google configuration found 6 findings on the same file, but none of these three. That is why Checkstyle was not chosen.
- **The Java version matters.**
  - Old Java 1.4 code that uses `enum` as a variable name could not be parsed with the default version, but was analysed with `--use-version java-1.4`.
  - A preview feature (primitive types in `instanceof`) could not be parsed without the matching preview version.
- **Files PMD can't parse.**
  - PMD skips them and still analyses the rest.
  - In the SARIF report they appear under `invocations[].toolExecutionNotifications`, with the file path, and `executionSuccessful` is `false`.
  - The tool must list these files in its report. Otherwise a skipped file looks like a file without findings.
- **Exit codes** [PMD-CLI], verified where marked:
  - **0:** PMD ran. Findings are in the report, because `--no-fail-on-violation` turns the exit code 4 for "violations found" into 0 (verified).
  - **5:** "At least one recoverable error has occurred", for example a file that couldn't be parsed (verified).
  - **1 and 2:** PMD failed, or the command-line parameters are invalid.
- **Type information.** PMD analyses source code without building the project. Without the project's dependencies on the "auxclasspath", PMD's documentation warns this "might result in false positives or negatives for some rules" [PMD-java]. The tool doesn't build Java projects in step 2, so this is a known limitation.
- **Build output.** `target/` and `build/` are excluded. In the test, a Java file under `target/generated-sources` was not analysed.
- **Lombok.** Private fields of a class annotated with Lombok's `@Getter` were not reported as unused, even though the Lombok library was not available to PMD.

## JavaScript and TypeScript

- **Tool:** Biome.
- **Command:** `npx @biomejs/biome@<version> check --write --reporter=sarif --reporter-file=<output>/biome.sarif .`
- **Configuration:** none.
- **Report:** `<output>` is the tool's output folder, outside the project. The report lists every finding that Biome could not fix.

Biome is a modern formatter and linter that works completely without configuration.

Verified behaviour:
- **What it does.** `biome check` "Checks the specified files for formatting, linting, and assist actions". `--write` "Applies formatting changes, safe lint fixes, and safe assist actions." [Biome-CLI]
- **Defaults.** "If no configuration is found, Biome's default configuration is used." [Biome-configure] The defaults are tab indentation, line width 80, double quotes and the recommended lint rules [Biome-ref].
- **Test result.** Indentation was changed to tabs, quotes to double quotes, semicolons were added and the imports were sorted.
- **Unsafe fixes.** Unsafe fixes are not applied. In the test, `x == 1` stayed (the fix to `===` is unsafe) and an unused variable stayed. Biome reported "Skipped 5 suggested fixes"; `--unsafe` would apply them [Biome-CLI].
- **Exit code and report.**
  - Biome first checks and fixes all files. Only at the end does it exit with code 1, and only if findings of severity *error* remain; warnings, such as an unused variable, don't change the exit code.
  - Biome 2.6.6 has no option to exit with 0 in that case. `check --help` only offers `--error-on-warnings`, which makes it stricter.
  - Exit code 1 also comes from a broken `biome.json`, an unknown option, or a folder without files. The exit code alone therefore can't tell "findings remain" from "Biome failed".
  - The tool decides by the report file instead. With `--reporter-file`, Biome writes every remaining finding (rule, severity, file, line) to the report. With a broken `biome.json`, no report file was written.
  - The report goes into the tool's output folder. When a report file was inside the project, Biome checked that file itself in the same run.
  - SARIF 2.1.0 is used rather than `--reporter=json`, because Biome prints "The `json` and `json-pretty` reporters are experimental and may change in patch releases."
- **`.gitignore`.** Biome doesn't read `.gitignore` by default (`vcs.useIgnoreFile` defaults to `false`) [Biome-ref].
  - In the test it reformatted `dist/out.js` although `dist/` was listed in `.gitignore`; `node_modules` was skipped.
  - With `--vcs-enabled=true --vcs-client-kind=git --vcs-use-ignore-file=true`, `dist/` was left untouched.
- **Pinned version.** The version is pinned in the command. Without it, `npx` uses the latest Biome release, so results would change over time.

## Python

- **Tool:** Ruff.
- **Commands:** `ruff check --fix`, then `ruff format`.
- **Configuration:** none. Ruff's default rules and default style are used.

`ruff format` formats the code. `ruff check --fix` applies the automatic fixes for lint findings, for example removing unused imports.

Verified behaviour:
- **Formatter.** `ruff format` is a replacement for Black: it "is intended to emit near-identical output when run over existing Black-formatted code" [Ruff-formatter]. The defaults are line length 88, 4-space indentation and double quotes [Ruff-settings].
- **Only safe fixes.** "Ruff only applies safe fixes by default." [Ruff-linter]
  - In the test, the unused import was removed (F401, safe fix).
  - The unused local variable was reported but *not* removed (F841): "This rule's fix is marked as unsafe because removing an unused variable assignment may delete comments" [Ruff-F841]. It would need `--unsafe-fixes`.
- **Default rules depend on the version.** Ruff 0.17.4 enables 407 rules by default (counted with `ruff check --show-settings`). They include Pyflakes (F), pyupgrade (UP), flake8-bugbear (B) and isort (I) [Ruff-defaults]. The default set changes between Ruff versions, so the version must be pinned.
- **Order.** The linter runs before the formatter, because lint fixes can leave code that needs formatting. Ruff's documentation gives this order for import sorting: "To sort imports and format, call the Ruff linter and then the formatter" [Ruff-formatter].

## C and C++

### Formatter: clang-format

The base is `clang-format -style=LLVM`, with these changes:

| Option | LLVM default | Used |
|---|---|---|
| `ColumnLimit` (line length) | 80 | 100 or 120 (not decided yet) |
| `SortIncludes` | enabled | `false` |
| `IndentWidth` | 2 | 4 |

```bash
clang-format -i --style="{BasedOnStyle: LLVM, ColumnLimit: 100, IndentWidth: 4, SortIncludes: false}" <files>
```

### Linter: clang-tidy

clang-tidy runs after clang-format. The check list depends on the language standard.

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

clang-tidy runs without `--fix`, so it only reports findings and doesn't change files.

### Verified behaviour

clang-format:
- **LLVM style.** The LLVM style is "A style complying with the LLVM coding standards" [ClangFormat], [LLVM-CS]. `--dump-config` shows the LLVM defaults `ColumnLimit: 80` and `IndentWidth: 2`.
- **`SortIncludes`.** The documentation now describes `SortIncludes` as a struct ("`Never` is deprecated by `Enabled: false`") [ClangFormat]. Version 22.1.8 still accepts `SortIncludes: false` and turns it into `SortIncludes: {Enabled: false}`.
- **Side effect of `IndentWidth: 4`.** The LLVM value `AccessModifierOffset: -2` stays, so `public:` and `private:` are indented by 2 columns (seen in the test). `AccessModifierOffset: -4` would align them with the `class` keyword. This is not part of the definition yet.
- **Inline style.** The style is passed inline with `--style="{…}"` instead of `--style=file`, so a `.clang-format` file in the analysed project doesn't change the rules.

clang-tidy:
- **Compilation database.** clang-tidy needs the project's compilation database: `-p` names the build directory that contains `compile_commands.json` [ClangTidy]. Without it, clang-tidy doesn't know the include paths, macros or language standard. The `-std=` flag in it decides which check list is used.
- **`--config` replaces the project's `.clang-tidy`.** clang-tidy only searches for `.clang-tidy` files when `--config` is empty [ClangTidy].
- **No `--quiet`.** `--quiet` only suppresses statistics, not findings [ClangTidy]. In a test, the findings on stdout were the same with and without it; only these stderr lines disappeared:
  ```
  6 warnings generated.
  Suppressed 4 warnings (3 in non-user code, 1 NOLINT).
  ```
  The NOLINT count shows how many findings the project's developers suppressed themselves, which is useful for the report. The command therefore runs without `--quiet`, and the tool stores stderr together with stdout.
- **The standard split matches what clang-tidy does anyway.** In a test file, `modernize-*` reported no findings with `-std=c++98` and 4 with `-std=c++17`. Splitting by language standard therefore matches what clang-tidy does by itself in this test.
- **`modernize-*` includes pure style checks.** `modernize-use-trailing-return-type` flagged both functions in the test file: 2 of the 4 findings (the other 2 were `modernize-use-nullptr`). On C++11 code, this check alone can produce a large share of all findings.

## Rust

- **Tools:** Clippy (linter) and rustfmt (formatter).
- **Command:** `cargo clippy --fix --allow-dirty && cargo fmt`
- **Configuration:** none.

`cargo clippy --fix` automatically fixes the errors Clippy can detect and fix, and `cargo fmt` formats the code.

Verified behaviour:
- **rustfmt.** "By default, Rustfmt uses a style which conforms to the Rust style guide" [rustfmt], [Rust-style].
- **Clippy lints.** Clippy runs the lint group `clippy::all` by default. "We try to keep the warn-by-default groups free from false positives (FP)." `--fix` applies the automatically applicable suggestions and "implies `--all-targets`" [Clippy-usage].
- **Why Clippy runs first.** In the test, the order `cargo fmt && cargo clippy --fix` failed:
  - `cargo fmt` changed a file, and Clippy then refused with "error: the working directory of this package has uncommitted changes, and `cargo fix` can potentially perform destructive changes; if you'd like to suppress this error pass `--allow-dirty`".
  - Clippy therefore runs first. `--allow-dirty` keeps it working when an earlier tool has already changed files; the clean state from step 1 is still in git [Cargo-fix].
  - rustfmt runs afterwards and formats the code that Clippy changed.
- **Test result.** Clippy replaced `vec![1, 2, 3]` with an array, `v.len() == 0` with `v.is_empty()`, and removed `.clone()` on a `Copy` value.
- **It runs project code.** Clippy compiles the project, and Cargo compiles and executes build scripts (`build.rs`) "just before building the package" [Cargo-build-scripts]. It therefore runs inside the tool's container and needs the crate's dependencies.

## Project configuration files

Most of these tools also read configuration files from the analysed project. When such a file exists, the project's own rules apply instead of the defaults defined here, and results of different projects are no longer comparable. The flowchart asks for "fixed rules per language".

| Tool | Reads from the project | How to ignore it |
|---|---|---|
| dotnet format | `.editorconfig` [MS-format] | open |
| google-java-format | nothing | not needed |
| PMD | no configuration file; the rule set is passed with `-R` | not needed |
| Biome | `biome.json` / `biome.jsonc` [Biome-configure] | open |
| Ruff | `pyproject.toml` (`[tool.ruff]`), `ruff.toml`, `.ruff.toml` [Ruff-config] | `--isolated` ("Ignore all configuration files") |
| clang-format | `.clang-format` | inline `--style`, used above |
| clang-tidy | `.clang-tidy` [ClangTidy] | `--config`, used above |
| rustfmt | `rustfmt.toml` [rustfmt] | open |
| Clippy | `clippy.toml` / `.clippy.toml` [Clippy-config] | open |

**Open decision:** should the tool ignore project configuration files and always use the defaults defined here, or respect them?

## Tested versions

| Tool | Version | Container image |
|---|---|---|
| clang-format, clang-tidy | 22.1.8 | Debian 13 with the LLVM 22 packages |
| .NET SDK (`dotnet format`) | 10.0.403 | `mcr.microsoft.com/dotnet/sdk:10.0` |
| google-java-format | 2.0.0 on OpenJDK 21.0.13 | `eclipse-temurin:21-jdk` |
| PMD | 7.29.0 on OpenJDK 21.0.13 | `eclipse-temurin:21-jdk` |
| Biome | 2.6.6 on Node 22 | `node:22-slim` |
| Ruff | 0.17.4 | `python:3.13-slim` |
| Rust toolchain (cargo, Clippy, rustfmt) | 1.99.1, Clippy 0.1.99 | `rust:1-slim` |

The step 2 containers should pin exactly these versions (or newer versions that have been tested the same way), because default rules and styles change between releases.

## Sources

- [MS-format] Microsoft Learn, "dotnet format command". https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-format
- [MS-analysis] Microsoft Learn, "Code analysis in .NET" (sections "Enabled rules", "Code-style analysis"). https://learn.microsoft.com/en-us/dotnet/fundamentals/code-analysis/overview
- [GJF] google-java-format, README. https://github.com/google/google-java-format
- [Google-style] Google Java Style Guide. https://google.github.io/styleguide/javaguide.html
- [PMD-java] PMD, "Java support" (language versions, preview features, auxclasspath). https://docs.pmd-code.org/latest/pmd_languages_java.html
- [PMD-CLI] PMD, "PMD CLI reference" (exit codes, `--no-fail-on-violation`). https://docs.pmd-code.org/latest/pmd_userdocs_cli_reference.html
- [PMD-rules] PMD, "Java Rules" (quickstart rule set). https://docs.pmd-code.org/latest/pmd_rules_java.html
- [Maven-compiler] Apache Maven Compiler Plugin, `compiler:compile` parameters `release` and `source`. https://maven.apache.org/plugins/maven-compiler-plugin/compile-mojo.html
- [Gradle-toolchains] Gradle User Manual, "Toolchains for JVM projects". https://docs.gradle.org/current/userguide/toolchains.html
- [Biome-CLI] Biome, CLI reference (`biome check`). https://biomejs.dev/reference/cli/
- [Biome-configure] Biome, "Configure Biome". https://biomejs.dev/guides/configure-biome/
- [Biome-ref] Biome, configuration reference (`formatter.*`, `vcs.useIgnoreFile`). https://biomejs.dev/reference/configuration/
- [Ruff-formatter] Ruff, "The Ruff Formatter". https://docs.astral.sh/ruff/formatter/
- [Ruff-linter] Ruff, "The Ruff Linter" (section "Fix safety"). https://docs.astral.sh/ruff/linter/
- [Ruff-settings] Ruff, settings (`line-length`, `indent-width`, `format.quote-style`, `format.indent-style`). https://docs.astral.sh/ruff/settings/
- [Ruff-defaults] Ruff, "Default Rules". https://docs.astral.sh/ruff/default-rules/
- [Ruff-F841] Ruff, rule "unused-variable (F841)". https://docs.astral.sh/ruff/rules/unused-variable/
- [Ruff-config] Ruff, "Configuring Ruff". https://docs.astral.sh/ruff/configuration/
- [ClangFormat] Clang, "Clang-Format Style Options". https://clang.llvm.org/docs/ClangFormatStyleOptions.html
- [LLVM-CS] LLVM Coding Standards. https://llvm.org/docs/CodingStandards.html
- [ClangTidy] clang-tidy documentation. https://clang.llvm.org/extra/clang-tidy/
- [rustfmt] rustfmt, README. https://github.com/rust-lang/rustfmt
- [Rust-style] The Rust Style Guide. https://doc.rust-lang.org/style-guide/
- [Clippy-usage] Clippy documentation, "Usage". https://doc.rust-lang.org/clippy/usage.html
- [Clippy-config] Clippy documentation, "Configuring Clippy". https://doc.rust-lang.org/clippy/configuration.html
- [Cargo-fix] The Cargo Book, "cargo fix". https://doc.rust-lang.org/cargo/commands/cargo-fix.html
- [Cargo-build-scripts] The Cargo Book, "Build Scripts". https://doc.rust-lang.org/cargo/reference/build-scripts.html

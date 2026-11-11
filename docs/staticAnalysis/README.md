# Static code analysis

This document defines step 3 of the workflow, "Static code analysis" (see `docs/toolflow_en.png`). For now it covers OpenGrep; SonarQube Community follows and will then run in parallel with it.

As in step 2, the tool runs with fixed settings and ignores the analysed project's own configuration, so that results of different projects are comparable.

## OpenGrep

[OpenGrep](https://github.com/opengrep/opengrep) is a fork of Semgrep 1.100.0 under the LGPL 2.1. It matches rules (Semgrep rule format) against the code, including taint rules that follow data from a source (e.g. a request parameter) to a sink (e.g. a shell command).

- **Version:** 1.33.1, release binary `opengrep_manylinux_<arch>`, checked against the SHA-256 digest of the GitHub release.
- **Image:** `code-analysis/opengrep` (`docker/static-analysis/opengrep/Dockerfile`), built from `python:3.13.18-slim`. The image contains the active rule sets, so a scan needs no network.
- **Languages:** the language support is part of the binary; nothing is installed per language. What each language needs is rules, see [Rule sets](#rule-sets).

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

It runs in the project folder with the environment variable `SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE=/opt/code-analysis/opengrep/default.semgrepignore`.

| Option | Why |
|---|---|
| `--config <rule folder>` | One per rule folder of the active rule sets that covers a language of the project (step 1: primary language and every other supported language). |
| `--taint-intrafile` | Follows taint across functions and methods of the same file. Without it, a source in one function and a sink in another are not connected. |
| `--disable-version-check` | No request to the internet; the container has no network anyway. |
| `--json-output`, `--sarif-output` | Raw reports. The JSON report is the source of `findings.json`; it contains the rule metadata (CWE, OWASP, confidence, …) and the files OpenGrep could not analyse. SARIF is kept for other tools. |
| `SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE` | Makes OpenGrep read its ignore patterns from the given file. The file is OpenGrep's own default list. Without it, a `.semgrepignore` in the project would *replace* the default list (see [What is scanned](#what-is-scanned)). |

Not used:
- `--validate`: it downloads the rule set `p/semgrep-rule-lints` from semgrep.dev. The image build checks the rules offline instead (see [Rule sets](#rule-sets)).
- `--error`, `--strict`: the exit code stays 0 when findings exist or single files can't be parsed; the status is taken from the report instead.

### Container

- **Project:** mounted read-only. OpenGrep cannot change the code.
- **Network:** none (`--network none`). The rules are in the image; nothing leaves the machine.
- **Output folder:** mounted writable, at the same path as on the host.

### What is scanned

- **The working tree after step 2:** OpenGrep reads the files as step 2 left them (formatted, uncommitted). Line numbers therefore match `git diff` and the findings of step 2.
- **Files known to git:** OpenGrep scans the files listed by `git ls-files`, so `.gitignore` applies.
- **OpenGrep's default ignore list:** left out are `build/`, `vendor/`, `dist/`, `*.min.js`, `.env/`, `.tox/`, `node_modules/`, `.npm/`, `.yarn/`, `.venv/`, `_opam/`, `_build/`, `_cargo/`, and the test folders `test/`, `tests/`, `testsuite/`, `*_test.go`. The file is `cli/src/semgrep/templates/.semgrepignore` of version 1.33.1.
- **Generated files:** findings in [generated files](../linterConventions/README.md#generated-files) are dropped, as in step 2. Syntax errors in them don't count either.

### Project configuration

| Project file | How it is ignored |
|---|---|
| `.semgrepignore` | not read: the default list is passed explicitly |
| rule files in the project (e.g. `.semgrep.yml`) | not read: OpenGrep loads only the rules given with `--config` (`get_config` in `cli/src/semgrep/config_resolver.py`, v1.33.1), all of them folders in the image |

Suppressions in the code itself (`// nosemgrep`, `# nosem`) belong to the code and are respected, as OpenGrep does by default.

### Analysis depth and limits

- **Within a file:** with `--taint-intrafile`, taint is followed across functions of the same file. For example, a request parameter read in `ping()` and passed to `run()`, which calls `subprocess.check_output(…, shell=True)`, is found (tested in `scheduler/tests/test_opengrep_docker.py`).
- **Not across files:** data flows from one file to another are not followed. Inter-file analysis exists only in the alpha versions of OpenGrep 2.0, which are marked "FOR TESTING ONLY".
- **Maturity:** OpenGrep's language list (`lang.json`) marks Java, JavaScript, TypeScript, Python and C# as `ga`, but C, C++ and Rust as `alpha`.
- **Coverage depends on the rules:** for Rust there are only 10 rules, for C 16 (plus 49 from 0xdea). For the number per language, see [Rule sets](#rule-sets).

### Status

| Status | Meaning |
|---|---|
| `ok` | OpenGrep finished (exit code 0) and wrote its report. Findings don't change the exit code. |
| `partial` | As `ok`, but some files could not (fully) be analysed, e.g. syntax errors or timeouts. They are listed in the notes. |
| `failed` | Non-zero exit code or no report, e.g. a rule OpenGrep rejects. Or the image doesn't match `config/opengrep.toml`: an active rule set is missing or at another commit (config changed, image not rebuilt). |
| `skipped` | No active rule set covers any language of the project. |

Languages without rules in the active rule sets are listed in the notes.

## Rule sets

OpenGrep ships no rules. The rule sets are defined in [`config/opengrep.toml`](../../config/opengrep.toml): each is a git repository at a fixed commit, with the rule folders that cover each language of step 1.

| Rule set | Repository, commit | License |
|---|---|---|
| `opengrep-rules` (active) | [opengrep/opengrep-rules](https://github.com/opengrep/opengrep-rules) `f1d2b562b414783763fd02a6ed2736eaed622efa` – fork of semgrep-rules from 2024-12-13, archived | LGPL 2.1 with Commons Clause |
| `semgrep-rules` (not active, not downloaded) | [semgrep/semgrep-rules](https://github.com/semgrep/semgrep-rules) `a5ad1714f00c9f7fde980cab0ea1d6dc6d414fd2` (2026-11-10) | Semgrep Rules License v1.0 |
| `0xdea-semgrep-rules` (active) | [0xdea/semgrep-rules](https://github.com/0xdea/semgrep-rules) `b9942606d3543e3145a95332e6543fb79a01af00` (2026-11-06) | MIT |

Rules per language. The numbers are rule entries counted by the image build, on 2026-11-11, with all three rule sets active; `rulesets.json` in the image lists them per folder:

| Language | Folders | opengrep-rules | semgrep-rules | 0xdea-semgrep-rules |
|---|---|---|---|---|
| C | `c` / `rules/c` | 16 | 16 | 49 |
| C++ | – / `rules/c` | 0 | 0 | 49 |
| C# | `csharp` | 52 | 52 | – |
| Java | `java` | 127 | 130 | – |
| JavaScript, TypeScript | `javascript`, `typescript` | 182 + 30 | 182 + 32 | – |
| Python | `python` | 375 | 378 | – |
| Rust | `rust` | 10 | 10 | – |

- **All rules of a folder run,** without a filter on the category. Besides `security`, the folders contain `correctness`, `best-practice`, `performance` and `maintainability` rules. Every finding keeps the rule's `category` and `subcategory` (`vuln`: likely exploitable, `audit`: needs a review), CWE, OWASP, `confidence`, `likelihood` and `impact`, so that step 6 can filter.
- **C++:** the rules in `c/` of opengrep-rules and semgrep-rules declare `languages: [c]`, so OpenGrep only applies them to `.c` and `.h` files. 0xdea's rules declare `c` and `cpp`. Its folder `noisy/` is not used.
- **JavaScript and TypeScript:** most rules in `javascript/` declare both languages; `typescript/` adds TypeScript-only rules. Both folders run for either language.

### Switching and adding rule sets

Only the **active** rule sets are downloaded into the image. A rule set that is only defined is never fetched: fetching it and letting OpenGrep load it already counts as using the rules, and the Semgrep Rules License says "By using the rules, you agree to all of the terms and conditions below". Activating a rule set is therefore a conscious step, followed by a rebuild.

- **Switch:** change `rulesets` under `[opengrep]` in `config/opengrep.toml`, e.g. to `["semgrep-rules", "0xdea-semgrep-rules"]`, then rebuild the image:
  ```bash
  docker compose -f docker/docker-compose.yml --profile tools build opengrep
  ```
  The next run uses the new list, and `summary.json` records it.
- **Add:** add a `[rulesets.<name>]` table (`repository`, `commit`, `license`, `folders` per language, optional `exclude`). Activate it and rebuild the image.
- **What the build does:** it fetches each active repository at its commit and keeps only the rule files (`*.yaml`, `*.yml`, without `*.test.yaml`) of the listed folders, plus the license and readme files. It then lets OpenGrep load all rules offline and fails if OpenGrep rejects one. Such files can be listed in `exclude`, with the reason noted here. Inactive rule sets are listed as "not downloaded".
- **Image doesn't match the config:** if an active rule set is not in the image, or is there at another commit, the scheduler stops OpenGrep with a note to rebuild the image.

### Licenses

- **Repository:** it contains only the repository URL and commit of each rule set, no rules. `.gitignore` additionally excludes local `semgrep-rules/` folders.
- **Image:** the rules of the active rule sets are stored in the local Docker image. Don't push an image built with semgrep-rules to a registry or pass it on.
- **Results contain parts of the rules:** `findings.json` and `opengrep.json` contain the message and metadata of every rule that matched. `opengrep.sarif` even contains the description and help text of *every* loaded rule, e.g. 375 for the Python folder of opengrep-rules. The Semgrep Rules License covers "any portion of those rules", so results produced with semgrep-rules must not be published or passed on, e.g. in the appendix of a thesis. Results produced with opengrep-rules and 0xdea are not restricted this way.
- **Semgrep Rules License v1.0:**
  - It allows the rules "only for your own internal business purposes", forbids distributing them or offering them "to others as a service", and requires keeping the license notices in every copy. The build does this: each rule set keeps its LICENSE file.
  - It doesn't define "internal business purposes". Whether, for example, a thesis at a university is covered is not clear from the text. That must be decided by the user of the tool (supervisor, university, or Semgrep).
  - It doesn't restrict which engine runs the rules.
- **opengrep-rules** keeps the original license of semgrep-rules from the time of the fork: LGPL 2.1 with the Commons Clause, which forbids selling the software. Its readme says: "These rules are intended for research, testing & benchmarking."

## Results

Step 3 writes into the run folder of step 2: `OUTPUT_DIR/<project>/<date_time>Z/step3-static-analysis/`.

| Path | Content |
|---|---|
| `summary.json` | One entry per tool: version, command, exit code, status, duration, rule sets (name, repository, commit, license, folders), rules run, files scanned, findings per severity and category, notes |
| `findings.json` | Findings of all tools: tool, rule set, rule, severity (`error`/`warning`/`note`) and the tool's severity, message, language, file, start and end position, category, subcategory, CWE, OWASP, confidence, likelihood, impact, fingerprint |
| `opengrep/opengrep.json` | OpenGrep's JSON report, including the taint trace (`dataflow_trace`) of each taint finding |
| `opengrep/opengrep.sarif` | The same findings as SARIF 2.1.0 |
| `opengrep/opengrep.stdout.txt`, `.stderr.txt` | Text report and scan summary |

Severity mapping: `ERROR` → `error`, `WARNING` → `warning`, `INFO` → `note`.

Rule ids are shortened by the image path: `python.flask.security.injection.subprocess-injection` instead of `opt.code-analysis.opengrep.rulesets.opengrep-rules.python.flask.…`. OpenGrep builds the id from the folder path of the rule file and the rule's `id`.

## Verified behaviour

Checked with OpenGrep 1.33.1 in the image (arm64), 2026-11-11; the cases are integration tests in `scheduler/tests/test_opengrep_docker.py`.

- **Project `.semgrepignore`:** a project file containing `src/` made plain OpenGrep skip `src/` and scan `tests/`. With the explicit default list, `src/` is scanned and `tests/` is skipped.
- **Taint across functions:** with `--taint-intrafile`, a command injection whose source and sink are in different functions of one file is found at the sink. Without the option it is not found.
- **One finding per language:** the active rule sets report at least one finding for each supported language: Java, C#, JavaScript, TypeScript, Python, Rust, C and C++.
- **Syntax error:** a C file with a syntax error leaves the exit code at 0. The report lists the file with "Syntax error", and the status is `partial`.
- **Invalid rule:** the exit code is 2, and the report lists a "Rule parse error".

## Sources

All quotes are from the files at the version used. They were checked on 2026-11-11.

1. **OpenGrep README, v1.33.1.** <https://github.com/opengrep/opengrep/blob/v1.33.1/README.md>
   - "Welcome to Opengrep, a fork of Semgrep, under the LGPL 2.1 license"
   - "Compatible with Semgrep rules - your existing rules and rulesets work unchanged"
   - "Self-contained binaries via Nuitka (no Python required)"
   - "Opengrep supports 30+ languages, including: … C · C++ · C# · … Java · JavaScript · … Python · … Rust · … TypeScript …"
2. **OPENGREP.md, v1.33.1.** <https://github.com/opengrep/opengrep/blob/v1.33.1/OPENGREP.md>
   - "Opengrep is a fork of Semgrep version 1.100.0."
   - "Added support for intrafile cross-function tainting, with the flag `--taint-intrafile`, supporting higher-order functions. This works similarly to Semgrep's `--pro-intrafile`."
3. **`opengrep scan --help`, v1.33.1** (output of the binary).
   - On `--taint-intrafile`: "Enable intra-file inter-procedural taint analysis. Supported languages: Apex, C, Clojure, C#, C++, Dart, Elixir, Go, Java, JavaScript, Julia, Kotlin, Lua, Python, Ruby, Rust, Scala, Swift, TypeScript, Visual Basic. Other languages will fall back to intraprocedural analysis only."
   - On `--validate`: "Validate configuration file(s). This will check YAML files for errors and run 'p/semgrep-rule-lints' on the YAML files. No search is performed."
4. **Release v2.0.0-nopython-interfile.alpha.4.** <https://github.com/opengrep/opengrep/releases/tag/v2.0.0-nopython-interfile.alpha.4>
   - "FOR TESTING ONLY. Alpha release of Opengrep 2.0.0 with python removed and interfile support."
5. **`cli/src/semgrep/run_scan.py`, v1.33.1, lines 118–131.** <https://github.com/opengrep/opengrep/blob/v1.33.1/cli/src/semgrep/run_scan.py#L118-L131>
   - If `SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE` is set, that file is used. Otherwise `.semgrepignore` in the working directory is used, and only "No .semgrepignore found. Using default .semgrepignore rules." falls back to `templates/.semgrepignore`.
6. **Default ignore list, v1.33.1.** <https://github.com/opengrep/opengrep/blob/v1.33.1/cli/src/semgrep/templates/.semgrepignore> (SHA-256 `e6acfd569980d21db340e98529dd632bc45d750c1b1f0c9784730e086f298bf5`).
7. **Language maturity.** `lang.json` of opengrep/semgrep-interfaces at `5e705660dc7381d4c4c247fa83c4c5860e61d555`, the submodule of v1.33.1. <https://github.com/opengrep/semgrep-interfaces/blob/5e705660dc7381d4c4c247fa83c4c5860e61d555/lang.json>
   - `"maturity"`: `ga` for csharp, java, js, python, ts.
   - `alpha` for c, cpp, rust.
   - File extensions: c `.c`, `.h`; cpp `.cc`, `.cpp`, `.cxx`, `.c++`, `.hpp`, … .
8. **opengrep-rules README and LICENSE**, commit `f1d2b562`. <https://github.com/opengrep/opengrep-rules>
   - "These rules are intended for research, testing & benchmarking."
   - LICENSE: "“Commons Clause” License Condition v1.0 … Software: semgrep-rules … License: LGPL 2.1 … Licensor: Semgrep, Inc."
9. **Semgrep Rules License v. 1.0** (last updated December 13th, 2024). <https://semgrep.dev/legal/rules-license>
   - "By using the rules, you agree to all of the terms and conditions below."
   - "You may use the rules only for your own internal business purposes."
   - "This license does not allow you to distribute the rules, or to make them available to others as a service."
   - "If you copy any of the rules, you must also include the notices of the licensor in that copy."
   - Definitions: "The rules are the rules for the Semgrep analysis tool that the licensor makes available under these terms, including any portion of those rules." and "Use means anything you do with the rules requiring one of your licenses."
10. **0xdea/semgrep-rules README.** <https://github.com/0xdea/semgrep-rules>
    - "Battle-tested C (and sometimes C++) ruleset."
    - License: MIT.
11. **Release assets of v1.33.1.** <https://github.com/opengrep/opengrep/releases/tag/v1.33.1>
    - SHA-256 digests: `opengrep_manylinux_x86` `46e8133cc490f9f50518061590ce2f6be5a08afaeda034471c0ceda4b3899808`, `opengrep_manylinux_aarch64` `d1433f63a90b8bc0b6ad4b498a13b372b856ce9a754a43484ba4920fe2d76dcf`.

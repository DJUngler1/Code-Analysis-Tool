# LLM evaluation and audit

This document defines step 6 of the workflow, "LLM evaluation" and "Final audit" (see `docs/toolflow_en.png`). It evaluates the results of steps 2 to 5 in three areas:

| Area | Results of | Question |
|---|---|---|
| Security | Step 4 (OSV-Scanner, package manager audits) and the findings of step 3 (OpenGrep, SonarQube) that their tool classifies as security issues or bugs (`security`, `correctness`, `reliability`), or not at all | Which findings are real security issues of the shipped product? |
| Code quality | Step 2 (linters and formatters) and the other findings of step 3 (maintainability, best practice, portability, performance, …) | Which findings matter, and what did the formatters change? |
| Test quality | Step 5 (tests, coverage, mutation testing) | Do the tests work, and what do they miss? |

Two local models run through [Ollama](https://ollama.com) on the host:
- a **decision model** labels every item (`pplx-decider-flash:latest` by default);
- a **reasoning model** writes the texts of the reports (`qwen3.8:latest` by default).

Everything that can be computed is done by the tool itself, not by a model: ids, duplicates, groups, counts, sorting, statistics, the example diff and the final audit. The models only label and write.

## Configuration

Five kinds of table in [`code-analysis.toml`](../../code-analysis.toml), read at every run. Each key has a comment there. A missing key takes the default below; an unknown key, or an area name that does not exist, is an error (exit code 9), so a typo is not ignored.

**`[llm]`**

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `http://host.docker.internal:11434` | Ollama's address from the container; `OLLAMA_BASE_URL` in `docker/.env` overrides it |
| `report_language` | `English` | Language of the texts the reasoning model writes, and of the audit's headings (`German` gives German headings) |
| `areas` | all three | Areas to evaluate: `security`, `code_quality`, `test_quality` |

**`[llm.input]`**: what the tool prepares for the models, and the excerpts in the reports. Smaller values make requests smaller and faster, but the models see less.

| Key | Default | Meaning |
|---|---|---|
| `message_chars` | 300 | Characters of a finding's message or an advisory's summary |
| `code_chars` | 400 | Characters of the code excerpt around a finding |
| `code_context_lines` | 2 | Lines of code before and after the finding |
| `group_samples` | 5 | Sample locations per group of findings (code quality and security groups) |
| `group_samples_with_code` | 3 | How many of these samples include a code excerpt (at most `group_samples`) |
| `group_messages` | 3 | Different messages shown per group |
| `security_single_severity` | `"high"` | `REAL` security findings of this severity or higher get an entry of their own (`"high"`: also `error` and `critical`); the other findings are grouped by rule (or package) and label. One of `critical`, `high`, `error`, `medium`, `warning`, `low`, `note`, `info` |
| `failed_tests_per_group` | 5 | Test names shown per group of failed tests |
| `failure_message_chars` | 600 | Characters of the failure message of a group of failed tests |
| `declaration_chars` | 300 | Characters of the declarations (classes, functions) of a weakly tested file |
| `weak_line_rate` | 0.5 | Source files below this line coverage are weakly tested |
| `diff_example_lines` | 10 | Lines of the formatter example diff in the report |
| `patch_sample_kib` | 32 | KiB of `changes.patch` the reasoning model reads to name the kinds of formatter change |

**`[llm.decision]`**: the decision model.

| Key | Default | Meaning |
|---|---|---|
| `model` | `pplx-decider-flash:latest` | The decision model |
| `timeout_seconds` | 300 | Time limit per request, including loading the model |
| `retries` | 2 | Repetitions of a failed request with one item; a request with several items is split in half instead |
| `keep_alive` | `10m` | How long Ollama keeps the model loaded between requests |
| `max_questions` | 8 | Items per request (the endpoint allows 64) |
| `max_input_kib` | 20 | Text per request: the shared input once per question, plus the questions |
| `review_confidence` | 0.6 | Decisions with a lower confidence are flagged for review |

The endpoint has no generation options ("generation controls are not supported").

**`[llm.report]`**: the reasoning model.

| Key | Default | Meaning |
|---|---|---|
| `model` | `qwen3.8:latest` | The reasoning model; it needs a build with structured output (GGUF, not MLX) |
| `timeout_seconds` | 900 | Time limit per request, including loading the model and its reasoning |
| `retries` | 2 | Repetitions of a failed request, and of an answer that is not valid (with another seed) |
| `keep_alive` | `10m` | How long Ollama keeps the model loaded between requests |
| `think` | `true` | Reasoning before the answer: `true`, `false` or a level of the model, e.g. `"low"`, `"medium"`, `"high"` for gpt-oss (the values `/api/show` lists under `thinking.values`). A model that cannot switch reasoning off uses its lowest level for `false`; an unknown level gives the model's default |
| `temperature`, `top_p`, `top_k`, `seed` | 0.6, 0.95, 20, 42 | Generation options |
| `min_p` | not set | Minimum probability of a token; not set: not sent, the model's default applies |
| `num_ctx`, `num_predict` | 16384, 6144 | Context window and the maximum number of tokens to generate (`num_predict` must be smaller) |
| `entries_per_request` | 10 | Report entries written per request |
| `chars_per_token` | 2.5 | Estimate the tool uses to fit entries into `num_ctx − num_predict`; lower is safer |

**`[llm.report.security]`, `[llm.report.code_quality]`, `[llm.report.test_quality]`** (optional): the reasoning model's settings for one area. Every key of `[llm.report]` except `model` is possible; keys that are not set come from `[llm.report]`. Example: `think = "high"` for security and `think = "low"` for code quality. The audit lists these settings under "Method and limits".

**`[llm.threat_model]`**: the project's threat model (see [Threat model](#threat-model)). A threat model committed to the project is always used as it is; `create` and `docs_kib` only apply when a project has no confirmed one yet.

| Key | Default | Meaning |
|---|---|---|
| `create` | `"generate"` | `"generate"`: three questions in the terminal, the reasoning model of `[llm.report]` drafts the threat model, the user corrects and confirms it. `"manual"`: the tool writes a template with the questions as hints, the user writes the threat model and confirms it (no model) |
| `docs_kib` | 24 | KiB of `README*`, `SECURITY*` and `docs/` the reasoning model reads for the draft; less if `num_ctx − num_predict` is smaller |
| `context_chars` | 2500 | Characters of the confirmed threat model the security decisions and reports see; longer texts are cut. The decision model reads it once per question, so a long threat model makes the requests smaller and more numerous |

Both models must be installed in Ollama (`ollama pull <model>`). The tool checks this at the start and names missing models.

## Threat model

Before step 2 (and before step 6 with `--evaluate`), the tool needs a confirmed threat model of the project: what kind of software it is, which data an attacker controls (the trust boundaries), and what is shipped. It can be written by the user (committed to the project as `.code-analysis/threat-model.md`, put into `<OUTPUT_DIR>/<project>/`, or from the tool's template with `create = "manual"`) or drafted by the reasoning model from three questions and the project's documentation (`create = "generate"`) and then corrected and confirmed by the user. The flow is described in the [README](../../README.md#threat-model-before-step-2); the code is in `scheduler/src/scheduler/threat_model.py`, the prompt of the draft in `threat_model.md`.

In step 6 only the security area uses it: `{{THREAT_MODEL}}` in `security_decision.md` (`## Context`, with rule 6) and in `security_report.md` (`## System`). It comes from the run folder (`threat-model.md`), without HTML comments and shortened to `context_chars`. Without a threat model the prompts get "No threat model was provided for this run.", and the notes of the area and the audit say so. Because the threat model is part of the shared input of the security decisions, `--resume` labels the security area again when it changed; the other areas are reused.

The rule and the paragraph that use the threat model in the two security prompts, and the prompt `threat_model.md`, are drafts (marked in their introductions) that the user still has to check.

## 1. Preparing the items

The tool reads the results of steps 2 to 5 from the run folder and builds the items the decision model labels:

| Area | Item | Id | Built from |
|---|---|---|---|
| Security | Static finding with the category `security`, `correctness`, `reliability` or none | `S-0001` | `step3-static-analysis/findings.json` |
| Security | Dependency finding | `D-0001` | `step4-dependencies/findings.json` |
| Code quality | Group of lint findings: same tool, rule and scope | `L-001` | `step2-linting/findings.json` (exact duplicates removed) |
| Code quality | Group of static findings with another category: same tool, rule and scope | `L-001` (numbered together with the lint groups) | `step3-static-analysis/findings.json` (exact duplicates removed) |
| Test quality | Group of failed tests of a suite with the same failure message (numbers, paths and quoted text left out) | `T-001` | `step5-tests/tests.json` |
| Test quality | Source file the tests did not load or cover to less than `weak_line_rate` (half) of its lines | `F-0001` | `step5-tests/coverage.json`, `mutants.json` |
| Test quality | Mutant the tests ran but did not detect (`Survived`) | `M-0001` | `step5-tests/mutants.json` |

**Findings of step 3** are divided by their tool's own classification, the `category` in `findings.json` (see [static analysis](../staticAnalysis/README.md)): OpenGrep takes it from the rule's metadata, SonarQube from the software quality of the issue's highest impact (security hotspots are always `security`).
- `security`: security.
- `correctness` (OpenGrep) and `reliability` (SonarQube): security as well. Both mean bugs, and bugs can have security consequences; a finding rated too high is better than one overlooked. Examples: OpenGrep's `double_goto` (the pattern of the "goto fail" bug in a TLS check) and `tempfile-insecure` (`tempfile.mktemp`) are `correctness` rules without a CWE; SonarQube's `S8786` (a regular expression with super-linear backtracking) is `reliability`.
- No category: security. Without a category the tool cannot tell, so the finding stays where it is checked more strictly.
- Every other category (`maintainability`, `best-practice`, `portability`, `performance`, …): code quality, grouped like the lint findings.

The ids of static findings count all findings of `findings.json` (`S-0005` is the fifth), so the security items have gaps. The reason for the split: in a run on a TypeScript project, only 15 of the 614 findings in the security area were security findings by their tool's classification. 494 came from one OpenGrep i18n rule. The reasoning model wrote a text for each of them, which took 66 of the 87 minutes of step 6. With the split, that run has 31 security items (12 `security`, 11 `reliability`, 5 `correctness`, 3 dependencies), and the 594 other findings are 31 code quality groups.

**Duplicates** (security only) are merged by the tool, not by a model:
- static findings in the same file at overlapping lines with the same rule or a shared CWE, also across tools;
- dependency findings of the same package and ecosystem with the same advisory (its id or an alias, e.g. a GHSA and its CVE) and the same or an unknown version.

One finding per group is kept: the most precise one (with CWE, smallest range; for dependencies the OSV-Scanner finding with version, CVSS and fixed versions). The others get the label `DUPLICATE`.

**Scope** of a file, from its path: `generated` (left out by step 2), `vendored` (`vendor/`, `third_party/`, `node_modules/`, …), `test` (test folders and test file names), `docs/examples`, `ci`, `scripts` or `main`.

**What a model sees:** the facts of each item in compact form, e.g. tool, rule, severity, CWE, message, file, line, scope and up to two lines of code around the finding (at most 400 characters), read from the project as step 2 left it. The limits are set in `[llm.input]`. For weakly tested files, the declarations in the file (classes, functions). Never the complete result files.

## 2. Decisions

The decision model is called through Ollama's decision endpoint `POST /v1/systemone`. Each request has a shared input (the rules of the area and short facts about the run) and one `choice` question per item, with the labels as options. The model returns the most likely label, the probability of each label and a confidence.

| Area | Labels (the first is the default) |
|---|---|
| Security | `REAL`, `FALSE_POSITIVE`, `NOISE` |
| Code quality (per group) | `REAL`, `FALSE_POSITIVE`, `NOISE` |
| Failed tests | `PROJECT` (a defect in the code or the tests), `ENVIRONMENT` (no network, a missing service, the time limit, …) |
| Weakly tested files | `CRITICAL` (security, data integrity, money, core logic, public API, parsing, …), `NON_CRITICAL` |
| Surviving mutants | `TEST_GAP`, `LOW_VALUE` (logging, `toString`, defensive code, …), `POSSIBLY_EQUIVALENT` |

- **Batches:** a request holds at most `max_questions` items and `max_input_kib` of text. Ollama scores every question with its own copy of the shared input, so the memory a request needs grows with the number of questions.
- **Failures:** a failed request with several items is split in half and sent again. An item without a valid answer gets the default label and is flagged for review.
- **Review:** every decision with a confidence below `review_confidence` is flagged and listed in the audit.

## 3. Reports

For each area, the tool builds the report and the reasoning model writes its texts. The model is called through `POST /api/chat` with a JSON schema for the answer (`format`), `think` and the generation options of `[llm.report]`, or of the area's own table.

**Built by the tool:**
- **Security:** `REAL` findings of at least `security_single_severity` (default: `high`, `error`, `critical`) get one entry each (`E-001`), with all merged ids in `source_ids`, tools, rule ids, CWE/CVE/other identifiers, location or package with the fixed versions, severity as the tools report it, label and confidence. All other findings are grouped: static findings by tool, rule and label, dependency findings by package and label; a group of one is an entry like a single finding. A group has `count`, `files`, `severities`, `scopes`, every location (`locations`), and every finding with its own decision (`findings`); the reasoning model sees the group with `group_samples` samples (`group_samples_with_code` with code) and writes one text for the whole group. Sorted: `REAL` first (by severity, then size), then `FALSE_POSITIVE`, then `NOISE`. Statistics per finding: findings per label, `REAL` per severity, low-confidence decisions; and the number of entries and groups. Every input id is in exactly one entry. Without the groups, the reasoning model wrote one text per finding: for libgdx (2527 security findings, 1791 of them `NOISE`) that took about 70 seconds per text with qwen3.8 27B; with the groups there are 160 entries.
- **Security**, in addition: the number of findings of step 3 that are in the code quality report.
- **Code quality:** one group per lint or static analysis group (`G-001`) with tool, rule, the tool's category (static analysis only), count, files, severity, every location (file and lines; the models only see the samples), sample locations and messages; the findings of step 2 and of step 3 counted separately; formatter changes from `changes.patch` (tools that changed files, files, added and removed lines, the first hunk as an example, at most 10 lines copied verbatim); excluded files grouped by reason.
- **Test quality:** one entry per item (`E-001`): failed tests, weakly tested files, surviving mutants; the test runs, coverage and mutation results of step 5.

**Written by the reasoning model:**
- per entry: a `title`; for the labels that call for action (`REAL`, `PROJECT`, `CRITICAL`, `TEST_GAP`) a `description` and a `recommendation`, for the others a short `reason`; a `review_hint` when it doubts a decision (it never changes a label). In batches of `entries_per_request` that fit the context window.
- per area: `summary_text` and `run_notes`; code quality also the kinds of formatter change and notes on unusual exclusions; test quality also assessments of the test runs, the coverage and the mutation testing.

**Checks:** every answer must be valid JSON, contain every requested entry once and the texts its label needs. An answer that is not valid is split in half and asked again; a single entry is asked again with another seed, `retries` times. Texts that are still missing stay empty and are counted (`texts_missing`).

## 4. Audit

The tool writes the final audit into the run folder, from the reports alone (no model):

- **`audit.md`:**
  1. Overview: project, commit, date, languages, models, the status of steps 2 to 6.
  2. Key figures: `REAL` security findings by severity, `REAL` lint groups, test results, coverage, mutation score, decisions to review.
  3. Security: summary, the `REAL` entries with their number of findings, location and recommendation, the filtered ones with their reason; every finding of a group in a collapsible list per group (files with their lines, or the advisories of a package with installed and fixed versions). In the review list, a group shows how many of its findings were decided with low confidence.
  4. Code quality: summary, the `REAL` groups with where they are (the first location and how many more in the table, every file with its lines in a collapsible list per group), the filtered groups with their first location, the formatter changes with the example, the excluded files.
  5. Test quality: summary, test runs, coverage, mutation testing, failed tests caused by the project, critical files with little coverage, test gaps.
  6. Review list: decisions with low confidence and entries with a review hint.
  7. Method and limits: the tools and versions of each area, the models with their settings, the threat model (source, confirmation, SHA-256, and its text in a collapsible block; or a note that there was none).
- **`audit.json`:** the same content as data, with the complete reports.

The texts of the reasoning model are used as written. Headings are German when `report_language` is German, English otherwise. The audit can be written again from the reports with `python -m scheduler.evaluation.audit <run folder>`.

## Models and memory

- The two models are **never loaded at the same time**. Before the decisions, a loaded reasoning model is unloaded; after the decisions, the decision model is unloaded, and the reports are only written once it is gone; at the end, the reasoning model is unloaded.
- After **3 failed requests in a row** (Ollama unreachable, or a server error such as a crashed model runner), step 6 stops instead of sending more requests. Requests Ollama rejects (4xx) do not count.

## Running

Step 6 runs after step 5 with every `bin/code-analysis` run. To run only step 6 and the audit on the results of an earlier run, e.g. after changing a prompt or a model:

```sh
bin/code-analysis --evaluate ~/code-analysis-results/<project>/<date_time>Z
```

The folder must be below `OUTPUT_DIR`. An earlier `step6-llm-evaluation/` folder and audit are overwritten. Exit code 9 means step 6 is incomplete: `[llm]` invalid, Ollama unreachable or a model missing, or an area got no labels or not all texts.

To continue where an evaluation stopped, e.g. after the reasoning model failed, add `--resume`:

```sh
bin/code-analysis --evaluate ~/code-analysis-results/<project>/<date_time>Z --resume
```

The decisions of the earlier evaluation are reused and only the reports are written again, also with another reasoning model. An area's decisions are reused only if nothing that shaped them changed: the decision model (name and digest), `max_questions`, `max_input_kib` and `review_confidence` of `[llm.decision]`, the items, and the question each item was asked (decision prompts, the items' facts, `[llm.input]`; checked against the logged requests). Otherwise the console names the reason and the area is labeled again. The kept requests stay in `requests/`, the new ones are numbered after them; the reports and their requests of the earlier evaluation are replaced. `summary.json` (`decisions_reused_from`) and the audit's method section name the evaluation the decisions come from.

## Results

| Path | Content |
|---|---|
| `audit.md`, `audit.json` | The final audit |
| `threat-model.md`, `threat-model.json` | The threat model the run used (written before step 2; `--evaluate` writes the project's current one): text, source (project or results folder), confirmation time, SHA-256 |
| `step6-llm-evaluation/summary.json` | Models (name, digest), the threat model (source, confirmation, SHA-256, characters, shortened or not), the settings of `[llm]`, per area: status (`ok`, `partial`, `failed`, `skipped`), items, duplicates, requests, durations, tokens, labels, low-confidence decisions, notes |
| `step6-llm-evaluation/<area>/items.json` | The prepared items: ids, facts, duplicates, code context; for code quality groups every location |
| `step6-llm-evaluation/<area>/decisions.json` | Per item: label, probabilities, confidence, review flag, who decided (model, default, tool); duplicates with `duplicate_of` |
| `step6-llm-evaluation/<area>/decisions_info.json` | The evaluation that made the decisions: start, decision model (name, digest), settings, statistics; used by `--resume` |
| `step6-llm-evaluation/<area>/report.json` | The area's report: structure by the tool, texts by the reasoning model |
| `step6-llm-evaluation/<area>/requests/` | Every request and answer (`NNN-decision.json`, `NNN-report.json`), including the reasoning of the reasoning model |

## Prompts

The prompts are Markdown files in [`scheduler/src/scheduler/evaluation/prompts/`](../../scheduler/src/scheduler/evaluation/prompts/), part of the scheduler image (changes need `up -d --build`):

| File | Model | Area |
|---|---|---|
| `security_decision.md`, `security_report.md` | decision, reasoning | Security |
| `lint_decision.md`, `lint_report.md` | decision, reasoning | Code quality |
| `test_failure_decision.md`, `test_file_decision.md`, `test_mutant_decision.md`, `test_report.md` | decision, reasoning | Test quality |
| `threat_model.md` | reasoning | Draft of the threat model (before step 2; `## System`, `## Request`) |

Each file starts with an introduction for humans that is not sent. The `## ` sections below it are the parts the tool uses:
- decision prompts: `## Context` (the shared input), `## Question` (per item), `## Labels` with one `### LABEL` section per label;
- report prompts: `## System` (system message), `## Entries` and `## Summary` (the two kinds of request).

The security and lint prompts are based on the first prompts of this step (`docs/LLMAudit/prompts/`, in the git history up to commit `911ecab`); counting and grouping were moved from the prompts into the tool.

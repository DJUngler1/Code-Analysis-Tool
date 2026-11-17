# LLM evaluation and audit

This document defines step 6 of the workflow, "LLM evaluation" and "Final audit" (see `docs/toolflow_en.png`). It evaluates the results of steps 2 to 5 in three areas:

| Area | Results of | Question |
|---|---|---|
| Security | Step 3 (OpenGrep, SonarQube) and step 4 (OSV-Scanner, package manager audits) | Which findings are real security issues of the shipped product? |
| Code quality | Step 2 (linters and formatters) | Which lint findings matter, and what did the formatters change? |
| Test quality | Step 5 (tests, coverage, mutation testing) | Do the tests work, and what do they miss? |

Two local models run through [Ollama](https://ollama.com) on the host:
- a **decision model** labels every item (`pplx-decider-flash:latest` by default);
- a **reasoning model** writes the texts of the reports (`qwen3.8:latest` by default).

Everything that can be computed is done by the tool itself, not by a model: ids, duplicates, groups, counts, sorting, statistics, the example diff and the final audit. The models only label and write.

## Configuration

`[llm]`, `[llm.decision]` and `[llm.report]` in [`code-analysis.toml`](../../code-analysis.toml), read at every run:

| Key | Default | Meaning |
|---|---|---|
| `llm.base_url` | `http://host.docker.internal:11434` | Ollama's address from the container; `OLLAMA_BASE_URL` in `docker/.env` overrides it |
| `llm.report_language` | `English` | Language of the texts the reasoning model writes, and of the audit's headings (`German` gives German headings) |
| `llm.areas` | all three | Areas to evaluate: `security`, `code_quality`, `test_quality` |
| `llm.timeout_seconds` | 900 | Time limit per request |
| `llm.retries` | 2 | Repetitions of a failed request, and of an answer that is not valid |
| `llm.keep_alive` | `10m` | How long Ollama keeps a model loaded between requests |
| `llm.decision.model` | `pplx-decider-flash:latest` | The decision model |
| `llm.decision.max_questions` | 8 | Items per request (the endpoint allows 64) |
| `llm.decision.max_input_kib` | 20 | Text per request: the shared input once per question, plus the questions |
| `llm.decision.review_confidence` | 0.6 | Decisions with a lower confidence are flagged for review |
| `llm.report.model` | `qwen3.8:latest` | The reasoning model |
| `llm.report.think` | `true` | Let the model reason before it answers |
| `llm.report.temperature`, `top_p`, `top_k`, `seed` | 0.6, 0.95, 20, 42 | Generation options |
| `llm.report.num_ctx`, `num_predict` | 16384, 6144 | Context window and the maximum number of tokens to generate |
| `llm.report.entries_per_request` | 10 | Report entries written per request |

Both models must be installed in Ollama (`ollama pull <model>`). The tool checks this at the start and names missing models.

## 1. Preparing the items

The tool reads the results of steps 2 to 5 from the run folder and builds the items the decision model labels:

| Area | Item | Id | Built from |
|---|---|---|---|
| Security | Static finding | `S-0001` | `step3-static-analysis/findings.json` |
| Security | Dependency finding | `D-0001` | `step4-dependencies/findings.json` |
| Code quality | Group of lint findings: same tool, rule and scope | `L-001` | `step2-linting/findings.json` (exact duplicates removed) |
| Test quality | Group of failed tests of a suite with the same failure message (numbers, paths and quoted text left out) | `T-001` | `step5-tests/tests.json` |
| Test quality | Source file the tests did not load or cover to less than half of its lines | `F-0001` | `step5-tests/coverage.json`, `mutants.json` |
| Test quality | Mutant the tests ran but did not detect (`Survived`) | `M-0001` | `step5-tests/mutants.json` |

**Duplicates** (security only) are merged by the tool, not by a model:
- static findings in the same file at overlapping lines with the same rule or a shared CWE, also across tools;
- dependency findings of the same package and ecosystem with the same advisory (its id or an alias, e.g. a GHSA and its CVE) and the same or an unknown version.

One finding per group is kept: the most precise one (with CWE, smallest range; for dependencies the OSV-Scanner finding with version, CVSS and fixed versions). The others get the label `DUPLICATE`.

**Scope** of a file, from its path: `generated` (left out by step 2), `vendored` (`vendor/`, `third_party/`, `node_modules/`, …), `test` (test folders and test file names), `docs/examples`, `ci`, `scripts` or `main`.

**What a model sees:** the facts of each item in compact form, e.g. tool, rule, severity, CWE, message, file, line, scope and up to two lines of code around the finding (at most 400 characters), read from the project as step 2 left it. For weakly tested files, the declarations in the file (classes, functions). Never the complete result files.

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

For each area, the tool builds the report and the reasoning model writes its texts. The model is called through `POST /api/chat` with a JSON schema for the answer (`format`), `think` and the generation options of `[llm.report]`.

**Built by the tool:**
- **Security:** one entry per finding that is not a duplicate (`E-001`), with all merged ids in `source_ids`, tools, rule ids, CWE/CVE/other identifiers, location or package with the fixed versions, severity as the tools report it, label and confidence. Sorted: `REAL` first (by severity), then `FALSE_POSITIVE`, then `NOISE`. Statistics: findings per label, `REAL` per severity, low-confidence decisions. Every input id is in exactly one entry.
- **Code quality:** one group per lint group (`G-001`) with count, files, severity, sample locations and messages; formatter changes from `changes.patch` (tools that changed files, files, added and removed lines, the first hunk as an example, at most 10 lines copied verbatim); excluded files grouped by reason.
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
  3. Security: summary, the `REAL` findings with their recommendation, the filtered ones with their reason.
  4. Code quality: summary, the `REAL` groups, the formatter changes with the example, the excluded files.
  5. Test quality: summary, test runs, coverage, mutation testing, failed tests caused by the project, critical files with little coverage, test gaps.
  6. Review list: decisions with low confidence and entries with a review hint.
  7. Method and limits: the tools and versions of each area, the models with their settings.
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

## Results

| Path | Content |
|---|---|
| `audit.md`, `audit.json` | The final audit |
| `step6-llm-evaluation/summary.json` | Models (name, digest), the settings of `[llm]`, per area: status (`ok`, `partial`, `failed`, `skipped`), items, duplicates, requests, durations, tokens, labels, low-confidence decisions, notes |
| `step6-llm-evaluation/<area>/items.json` | The prepared items: ids, facts, duplicates, code context |
| `step6-llm-evaluation/<area>/decisions.json` | Per item: label, probabilities, confidence, review flag, who decided (model, default, tool); duplicates with `duplicate_of` |
| `step6-llm-evaluation/<area>/report.json` | The area's report: structure by the tool, texts by the reasoning model |
| `step6-llm-evaluation/<area>/requests/` | Every request and answer (`NNN-decision.json`, `NNN-report.json`), including the reasoning of the reasoning model |

## Prompts

The prompts are Markdown files in [`scheduler/src/scheduler/evaluation/prompts/`](../../scheduler/src/scheduler/evaluation/prompts/), part of the scheduler image (changes need `up -d --build`):

| File | Model | Area |
|---|---|---|
| `security_decision.md`, `security_report.md` | decision, reasoning | Security |
| `lint_decision.md`, `lint_report.md` | decision, reasoning | Code quality |
| `test_failure_decision.md`, `test_file_decision.md`, `test_mutant_decision.md`, `test_report.md` | decision, reasoning | Test quality |

Each file starts with an introduction for humans that is not sent. The `## ` sections below it are the parts the tool uses:
- decision prompts: `## Context` (the shared input), `## Question` (per item), `## Labels` with one `### LABEL` section per label;
- report prompts: `## System` (system message), `## Entries` and `## Summary` (the two kinds of request).

The security and lint prompts are based on the first prompts of this step (`docs/LLMAudit/prompts/`, in the git history up to commit `911ecab`); counting and grouping were moved from the prompts into the tool.

"""Step 6, reports: Python builds each area's report, the reasoning model writes its texts.

Python merges the duplicates into entries, counts, sorts and numbers them
(E-001, G-001), and computes the formatter statistics and the example diff
from changes.patch. Every input id ends up in exactly one entry's
`source_ids`.

The reasoning model (/api/chat with a JSON schema) only writes texts:
- per entry: a title, and either a description and a recommendation (labels
  that call for action: REAL, PROJECT, CRITICAL, TEST_GAP) or a short reason
  (the other labels); a review hint when it doubts a decision. In batches of
  `entries_per_request` that fit the context window.
- per area: the summary, notes on the run and the area's own texts (code
  quality: kinds of formatter changes, unusual exclusions; test quality:
  assessments of the test runs, the coverage and the mutation testing).

An answer that is not valid (no JSON, an id missing, a required text empty)
is split in half and asked again; a single entry is asked again with another
seed, `retries` times. Texts that are still missing stay null and are counted
in the report's `texts_missing`.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from scheduler.evaluation.config import ReportConfig
from scheduler.evaluation.items import LINT_DIR, SEVERITY_RANK, AreaItems, read_json
from scheduler.evaluation.ollama import Exchange, OllamaClient
from scheduler.evaluation.prompts import render, report_prompt

# Labels that call for action: their entries get a description and a recommendation.
ACTION_LABELS = ("REAL", "PROJECT", "CRITICAL", "TEST_GAP")
LABEL_ORDER = ("REAL", "PROJECT", "CRITICAL", "TEST_GAP", "ENVIRONMENT", "NON_CRITICAL", "POSSIBLY_EQUIVALENT",
               "LOW_VALUE", "FALSE_POSITIVE", "NOISE")
PROMPTS = {"security": "security_report", "code_quality": "lint_report", "test_quality": "test_report"}
TEXT_FIELDS = ("title", "description", "recommendation", "reason", "review_hint")
# Estimate for the context budget: characters per token of JSON, code and English text (on the safe side).
CHARS_PER_TOKEN = 2.5
PATCH_SAMPLE_BYTES = 32 * 1024
PATCH_SAMPLE_LINES_PER_FILE = 30
EXAMPLE_LINES = 10
EXCLUDED_EXAMPLES = 5
SAMPLE_LOCATIONS = 5
SUMMARY_LIST_MIN = 5
PROGRESS_STEPS = 10

ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, **{name: {"type": "string"} for name in TEXT_FIELDS}},
                "required": ["id", *TEXT_FIELDS],
            },
        },
    },
    "required": ["entries"],
}


@dataclass
class ReportStats:
    requests: int = 0
    failed_requests: int = 0
    invalid_answers: int = 0
    split_requests: int = 0
    duration_seconds: float = 0.0
    prompt_tokens: int = 0
    output_tokens: int = 0
    entries: int = 0
    texts_missing: int = 0
    summary_missing: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.texts_missing and not self.summary_missing

    def to_json(self) -> dict:
        return {"requests": self.requests, "failed_requests": self.failed_requests,
                "invalid_answers": self.invalid_answers, "split_requests": self.split_requests,
                "duration_seconds": round(self.duration_seconds, 1), "prompt_tokens": self.prompt_tokens,
                "output_tokens": self.output_tokens, "entries": self.entries, "texts_missing": self.texts_missing,
                "summary_missing": self.summary_missing, "notes": self.notes}


@dataclass
class Draft:
    """A report before the texts: the structure, what the model sees per entry, and the summary facts."""

    report: dict
    inputs: dict[str, dict]  # entry id -> what the model sees of the entry
    facts: dict  # what the model sees for the summary
    summary_fields: dict[str, str]  # name -> "text" or "list"; filled into report["texts"]


# --- shared helpers ---


def label_rank(label: str) -> int:
    return LABEL_ORDER.index(label) if label in LABEL_ORDER else len(LABEL_ORDER)


def severity_rank(severity: str | None) -> int:
    return SEVERITY_RANK.get(severity or "", 0)


def by_severity(severities: list[str | None]) -> dict[str, int]:
    counts = Counter(severity or "unknown" for severity in severities)
    return dict(sorted(counts.items(), key=lambda entry: (-severity_rank(entry[0]), entry[0])))


def _decision(decisions: dict[str, dict], item_id: str, default: str) -> dict:
    return decisions.get(item_id) or {"label": default, "confidence": None, "review": True, "decided_by": "missing"}


def _decision_fields(decision: dict) -> dict:
    return {"confidence": decision.get("confidence"), "review": bool(decision.get("review")),
            "decided_by": decision.get("decided_by")}


def _label_counts(labels: list[str]) -> dict[str, int]:
    counts = Counter(labels)
    return dict(sorted(counts.items(), key=lambda entry: label_rank(entry[0])))


def _model_input(entry_id: str, decision: dict, data: dict) -> dict:
    shown = {"id": entry_id, "label": decision["label"]}
    if decision.get("review"):
        shown["low_confidence"] = True
    return shown | {key: value for key, value in data.items() if key != "kind"}


def _empty_texts(entry: dict) -> dict:
    return entry | {name: None for name in TEXT_FIELDS}


def _run_tools(tools: list[dict]) -> list[dict]:
    return [{"name": tool.get("name"), "version": tool.get("version"), "status": tool.get("status"),
             **({"manifest": tool["manifest"]} if tool.get("manifest") else {}),
             **({"language": tool["language"]} if tool.get("language") else {}),
             "notes": tool.get("notes") or []} for tool in tools]


# --- security ---


def build_security(area: AreaItems, decisions: dict[str, dict], run_dir: Path) -> Draft:
    rows: list[tuple[str, dict, dict, dict]] = []
    inputs: dict[str, dict] = {}
    labels: list[str] = []
    for item in area.items:
        decision = _decision(decisions, item["id"], "REAL")
        data = item["data"]
        labels += [decision["label"]] + ["DUPLICATE"] * len(item.get("duplicates", []))
        static = item["kind"] == "static"
        identifiers = data.get("advisories", [])
        also = data.get("also_reported_as", [])
        entry = {
            "label": decision["label"],
            "category": "static" if static else "dependency",
            "severity": data.get("severity"),
            "source_ids": [item["id"], *item.get("duplicates", [])],
            "tools": sorted({data["tool"], *(other.split(": ", 1)[0] for other in also)}) if static
            else data.get("tools", []),
            "identifiers": {
                "rule_ids": sorted({data.get("rule"), *(other.split(": ", 1)[-1] for other in also)} - {None})
                if static else [],
                "cwe": data.get("cwe", []),
                "cve": [identifier for identifier in identifiers if identifier.upper().startswith("CVE-")],
                "other": [identifier for identifier in identifiers if not identifier.upper().startswith("CVE-")],
            },
            "locations": [{"file": data.get("file"), "line": data.get("line")}] if static and data.get("file") else [],
            "scope": data.get("scope"),
            "package": None if static else {
                "name": data.get("package"), "ecosystem": data.get("ecosystem"),
                "installed_version": data.get("version"), "fixed_versions": data.get("fixed_versions", []),
                "manifests": data.get("manifests", []), "scope": data.get("scope"),
            },
            "message": data.get("message") if static else data.get("summary"),
            "url": data.get("url"),
            **_decision_fields(decision),
        }
        rows.append((item["id"], entry, decision, data))
    ordered = sorted(rows, key=lambda row: (label_rank(row[1]["label"]), -severity_rank(row[1]["severity"]), row[0]))
    report_entries = []
    for number, (_, entry, decision, data) in enumerate(ordered, start=1):
        entry_id = f"E-{number:03d}"
        report_entries.append(_empty_texts({"entry_id": entry_id, **entry}))
        inputs[entry_id] = _model_input(entry_id, decision, data)
    real = [entry for entry in report_entries if entry["label"] == "REAL"]
    statistics = {
        "input_findings": len(labels),
        "by_label": _label_counts(labels),
        "report_entries": len(report_entries),
        "real_by_severity": by_severity([entry["severity"] for entry in real]),
        "low_confidence": sum(1 for entry in report_entries if entry["review"]),
    }
    report = {
        "area": "security",
        "run": {
            "output_dir": str(run_dir),
            "scanned_target": area.facts.get("project"),
            "tools": _run_tools(area.facts.get("static_tools", []) + area.facts.get("dependency_tools", [])),
            "rulesets": area.facts.get("rulesets", []),
            "dependency_coverage": area.facts.get("coverage", []),
            "step_notes": area.notes,
        },
        "statistics": statistics,
        "entries": report_entries,
    }
    facts = {
        "statistics": statistics,
        "tools": report["run"]["tools"],
        "dependency_coverage": area.facts.get("coverage", []),
        "notes": area.notes,
        "real_entries": [{"id": entry["entry_id"], "severity": entry["severity"], "category": entry["category"]}
                         for entry in real],
        "filtered_entries": [{"id": entry["entry_id"], "label": entry["label"]}
                             for entry in report_entries if entry["label"] != "REAL"],
    }
    return Draft(report, inputs, facts, {"summary_text": "text", "run_notes": "text"})


# --- code quality ---


def patch_stats(patch: str) -> dict:
    """Files, added and removed lines of a unified diff (only lines inside hunks count)."""
    files: list[str] = []
    added = removed = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            match = re.match(r"diff --git a/(.*) b/(.*)$", line)
            files.append(match.group(2) if match else line[len("diff --git "):])
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return {"files_changed": len(dict.fromkeys(files)), "lines_added": added, "lines_removed": removed}


def _hunks(patch: str) -> list[tuple[str, list[str]]]:
    """(file, lines of one hunk including its @@ header), in patch order."""
    hunks: list[tuple[str, list[str]]] = []
    file = None
    current: list[str] | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(.*) b/(.*)$", line)
            file = match.group(2) if match else None
            current = None
        elif line.startswith("@@") and file is not None:
            current = [line]
            hunks.append((file, current))
        elif current is not None and line[:1] in ("+", "-", " ", "\\"):
            current.append(line)
        elif current is not None and line == "":
            current.append(line)
    return hunks


def patch_example(patch: str, limit: int = EXAMPLE_LINES) -> dict | None:
    """The first hunk, at most `limit` lines, copied verbatim."""
    hunks = _hunks(patch)
    if not hunks:
        return None
    file, lines = hunks[0]
    return {"file": file, "diff": "\n".join(lines[:limit])}


def patch_sample(patch: str, limit: int) -> str:
    """The first hunk of as many files as fit `limit` bytes, for the kinds of change."""
    parts: list[str] = []
    size = 0
    seen: set[str] = set()
    for file, lines in _hunks(patch):
        if file in seen:
            continue
        seen.add(file)
        part = f"--- {file}\n" + "\n".join(lines[:PATCH_SAMPLE_LINES_PER_FILE])
        if size + len(part.encode()) > limit:
            break
        parts.append(part)
        size += len(part.encode()) + 1
    return "\n".join(parts)


def excluded_groups(excluded: list[dict]) -> list[dict]:
    groups: dict[str, list[str]] = defaultdict(list)
    for entry in excluded:
        if isinstance(entry, dict):
            groups[str(entry.get("reason") or "no reason given")].append(str(entry.get("file")))
    return [{"reason_or_pattern": reason, "count": len(files), "examples": files[:EXCLUDED_EXAMPLES]}
            for reason, files in sorted(groups.items(), key=lambda entry: (-len(entry[1]), entry[0]))]


def build_lint(area: AreaItems, decisions: dict[str, dict], run_dir: Path) -> Draft:
    groups = []
    for item in area.items:
        decision = _decision(decisions, item["id"], "REAL")
        data = item["data"]
        groups.append((item, decision, {
            "label": decision["label"],
            "tool": data.get("tool"),
            "rule_id": data.get("rule"),
            "language": data.get("language"),
            "scope": data.get("scope"),
            "severity": data.get("severity"),
            "count": data.get("count", 0),
            "files": data.get("files", 0),
            "fixed_by_the_tool": data.get("fixed_by_the_tool", 0),
            "source_ids": [item["id"]],
            "sample_locations": [{"file": sample.get("file"), "line": sample.get("line")}
                                 for sample in data.get("samples", [])][:SAMPLE_LOCATIONS],
            "messages": data.get("messages", []),
            **_decision_fields(decision),
        }))
    ordered = sorted(groups, key=lambda entry: (label_rank(entry[2]["label"]), -severity_rank(entry[2]["severity"]),
                                                -entry[2]["count"], entry[0]["id"]))
    report_groups = []
    inputs: dict[str, dict] = {}
    for number, (item, decision, group) in enumerate(ordered, start=1):
        group_id = f"G-{number:03d}"
        report_groups.append(_empty_texts({"group_id": group_id, **group}))
        inputs[group_id] = _model_input(group_id, decision, item["data"])

    patch_path = run_dir / LINT_DIR / "changes.patch"
    patch = patch_path.read_text(encoding="utf-8", errors="replace") if patch_path.is_file() else ""
    step_summary = read_json(run_dir / LINT_DIR / "summary.json", {})
    changed_by = sorted({str(tool.get("tool")) for tool in step_summary.get("tools", []) if tool.get("changed_files")})
    excluded = read_json(run_dir / LINT_DIR / "excluded-files.json", [])
    findings_by_label: Counter = Counter()
    real_by_severity: Counter = Counter()
    for group in report_groups:
        findings_by_label[group["label"]] += group["count"]
        if group["label"] == "REAL":
            real_by_severity[group["severity"] or "unknown"] += group["count"]
    statistics = {
        "input_findings": area.facts.get("input_findings", 0),
        "exact_duplicates": area.facts.get("exact_duplicates", 0),
        "groups": len(report_groups),
        "groups_by_label": _label_counts([group["label"] for group in report_groups]),
        "findings_by_label": dict(sorted(findings_by_label.items(), key=lambda entry: label_rank(entry[0]))),
        "real_findings_by_severity": dict(sorted(real_by_severity.items(),
                                                 key=lambda entry: (-severity_rank(entry[0]), entry[0]))),
        "low_confidence": sum(1 for group in report_groups if group["review"]),
    }
    real = [group for group in report_groups if group["label"] == "REAL"]
    report = {
        "area": "code_quality",
        "run": {
            "output_dir": str(run_dir),
            "scanned_target": area.facts.get("project"),
            "tools": _run_tools(area.facts.get("tools", [])),
            "step_notes": area.notes,
        },
        "statistics": statistics,
        "groups": report_groups,
        "formatter_changes": {
            "tools": changed_by,
            **patch_stats(patch),
            "files_changed_by_the_tools": len(area.facts.get("changed_files", [])),
            "files_created_by_the_tools": len(area.facts.get("created_files", [])),
            "example": patch_example(patch),
        },
        "excluded_files": {"total": len(excluded), "groups": excluded_groups(excluded)},
    }
    facts = {
        "statistics": statistics,
        "tools": report["run"]["tools"],
        "notes": area.notes,
        "real_groups": [{"id": group["group_id"], "tool": group["tool"], "rule": group["rule_id"],
                         "scope": group["scope"], "count": group["count"]} for group in real],
        "filtered_groups": [{"id": group["group_id"], "label": group["label"], "rule": group["rule_id"],
                             "count": group["count"]} for group in report_groups if group["label"] != "REAL"],
        "formatter_changes": {key: value for key, value in report["formatter_changes"].items() if key != "example"},
        "excluded_files": report["excluded_files"],
        "patch_sample": patch_sample(patch, PATCH_SAMPLE_BYTES),
    }
    return Draft(report, inputs, facts, {"summary_text": "text", "run_notes": "text", "change_types": "list",
                                         "excluded_notes": "text"})


# --- test quality ---

TEST_KINDS = {"test_failure": "failed_tests", "weak_file": "weakly_tested_file", "mutant": "surviving_mutant"}
TEST_KIND_ORDER = ("failed_tests", "weakly_tested_file", "surviving_mutant")


def build_tests(area: AreaItems, decisions: dict[str, dict], run_dir: Path) -> Draft:
    defaults = {"test_failure": "PROJECT", "weak_file": "CRITICAL", "mutant": "TEST_GAP"}
    rows = []
    for item in area.items:
        decision = _decision(decisions, item["id"], defaults[item["kind"]])
        data = {key: value for key, value in item["data"].items() if key not in ("kind", "declarations")}
        rows.append((item, decision, {
            "label": decision["label"],
            "kind": TEST_KINDS[item["kind"]],
            "suite": data.get("suite"),
            "file": data.get("file"),
            "source_ids": [item["id"]],
            "details": {key: value for key, value in data.items() if key not in ("suite", "file")},
            **_decision_fields(decision),
        }))
    ordered = sorted(rows, key=lambda row: (TEST_KIND_ORDER.index(row[2]["kind"]), label_rank(row[2]["label"]),
                                            row[0]["id"]))
    entries = []
    inputs: dict[str, dict] = {}
    for number, (item, decision, entry) in enumerate(ordered, start=1):
        entry_id = f"E-{number:03d}"
        entries.append(_empty_texts({"entry_id": entry_id, **entry}))
        inputs[entry_id] = _model_input(entry_id, decision, item["data"])

    facts_in = area.facts
    runs = facts_in.get("runs", [])
    totals = Counter()
    for run in runs:
        totals.update(run.get("counts") or {})
    statistics = {
        "suites": len(facts_in.get("suites", [])),
        "suites_with_tests": sum(1 for suite in facts_in.get("suites", []) if suite.get("tests_found")),
        "tests": dict(totals),
        "failed_test_groups": _label_counts([entry["label"] for entry in entries if entry["kind"] == "failed_tests"]),
        "weakly_tested_files": _label_counts([entry["label"] for entry in entries
                                              if entry["kind"] == "weakly_tested_file"]),
        "surviving_mutants": _label_counts([entry["label"] for entry in entries
                                            if entry["kind"] == "surviving_mutant"]),
        "coverage": [{key: run.get(key) for key in ("suite", "tool", "status", "line_rate", "branch_rate",
                                                     "files", "files_not_loaded")}
                     for run in facts_in.get("coverage", [])],
        "mutation": [{key: run.get(key) for key in ("suite", "tool", "status", "score", "counts", "sampled", "files",
                                                     "candidate_files")}
                     for run in (facts_in.get("mutation") or {}).get("runs", [])],
        "low_confidence": sum(1 for entry in entries if entry["review"]),
    }
    report = {
        "area": "test_quality",
        "run": {
            "output_dir": str(run_dir),
            "scanned_target": facts_in.get("project"),
            "suites": facts_in.get("suites", []),
            "network": facts_in.get("network"),
            "runs": runs,
            "differences_to_head": facts_in.get("differences_to_head", []),
            "coverage": facts_in.get("coverage", []),
            "mutation": facts_in.get("mutation"),
            "errors": facts_in.get("errors", []),
            "step_notes": area.notes,
        },
        "statistics": statistics,
        "entries": entries,
    }
    facts = {
        "statistics": statistics,
        "suites": facts_in.get("suites", []),
        "detection": facts_in.get("detection", {}),
        "tests_ran_with_network": facts_in.get("network"),
        "runs": [{key: run.get(key) for key in ("suite", "tool", "status", "counts", "notes")} for run in runs],
        "differences_to_head": [{key: value for key, value in difference.items() if key != "tests"}
                                | {"tests": len(difference.get("tests") or [])}
                                for difference in facts_in.get("differences_to_head", [])],
        "mutation_settings": {key: value for key, value in (facts_in.get("mutation") or {}).items() if key != "runs"},
        "errors": facts_in.get("errors", []),
        "notes": area.notes,
        "entries": [{"id": entry["entry_id"], "kind": entry["kind"], "label": entry["label"],
                     "file": entry.get("file")} for entry in entries],
    }
    return Draft(report, inputs, facts, {"summary_text": "text", "run_notes": "text", "tests_assessment": "text",
                                         "coverage_assessment": "text", "mutation_assessment": "text"})


BUILDERS: dict[str, Callable[[AreaItems, dict, Path], Draft]] = {
    "security": build_security, "code_quality": build_lint, "test_quality": build_tests}


def build_report(area: AreaItems, decisions: dict[str, dict], run_dir: Path) -> Draft:
    return BUILDERS[area.area](area, decisions, run_dir)


# --- texts by the reasoning model ---


def entry_list(report: dict) -> list[dict]:
    return report.get("entries") if "entries" in report else report.get("groups", [])


def entry_id(entry: dict) -> str:
    return entry.get("entry_id") or entry["group_id"]


class TextWriter:
    """Asks the reasoning model for the texts of one area and validates the answers."""

    def __init__(self, client: OllamaClient, config: ReportConfig, *, area: str, language: str, retries: int,
                 log: Callable[[Exchange], None] = lambda exchange: None, out: Callable[[str], None] = print):
        self.client = client
        self.config = config
        self.area = area
        self.retries = retries
        self.log = log
        self.out = out
        self.prompt = report_prompt(PROMPTS[area])
        self.system = render(self.prompt["System"], REPORT_LANGUAGE=language)
        self.stats = ReportStats()
        # What fits into one request: the context window minus the answer, in characters.
        self.budget = int((config.num_ctx - config.num_predict) * CHARS_PER_TOKEN) - len(self.system)

    # entries

    def write_entries(self, draft: Draft) -> None:
        entries = entry_list(draft.report)
        self.stats.entries = len(entries)
        texts: dict[str, dict] = {}
        batches = self._pack([draft.inputs[entry_id(entry)] for entry in entries])
        done = 0
        next_report = 1
        for batch in batches:
            self._write_batch(batch, texts, attempt=0)
            done += len(batch)
            if entries and done * PROGRESS_STEPS >= next_report * len(entries) and done < len(entries):
                self.out(f"    {self.area}: texts for {done} of {len(entries)} entries ({self.stats.requests} requests)")
                next_report = done * PROGRESS_STEPS // len(entries) + 1
            for entry in entries:  # after every batch: kept if a later request stops the step
                if entry_id(entry) in texts and entry.get("title") is None:
                    entry.update(texts[entry_id(entry)])

    def _pack(self, inputs: list[dict]) -> list[list[dict]]:
        template = len(render(self.prompt["Entries"], ENTRIES=""))
        limit = self.budget - template
        batches: list[list[dict]] = []
        current: list[dict] = []
        size = 0
        for shown in inputs:
            length = len(json.dumps(shown, ensure_ascii=False)) + 2
            if current and (len(current) >= self.config.entries_per_request or size + length > limit):
                batches.append(current)
                current, size = [], 0
            current.append(shown)
            size += length
        if current:
            batches.append(current)
        return batches

    def _write_batch(self, batch: list[dict], texts: dict[str, dict], attempt: int) -> None:
        user = render(self.prompt["Entries"], ENTRIES=json.dumps(batch, ensure_ascii=False, indent=1))
        answer = self._ask(user, ENTRY_SCHEMA, attempt)
        labels = {shown["id"]: shown["label"] for shown in batch}
        if answer is not None:
            for written in answer.get("entries") or []:
                if isinstance(written, dict) and written.get("id") in labels and written["id"] not in texts:
                    valid = validate_texts(written, labels[written["id"]])
                    if valid is not None:
                        texts[written["id"]] = valid
        missing = [shown for shown in batch if shown["id"] not in texts]
        if not missing:
            return
        if answer is not None:  # valid JSON, but entries missing or incomplete
            self.stats.invalid_answers += 1
        if len(missing) > 1:  # a smaller request gives a shorter answer: split and ask again
            self.stats.split_requests += 1
            half = len(missing) // 2
            for part in (missing[:half], missing[half:]):
                self._write_batch(part, texts, attempt)
        elif attempt < self.retries:
            self._write_batch(missing, texts, attempt + 1)

    # summary

    def write_summary(self, draft: Draft) -> None:
        facts = self._fit(draft.facts)
        schema = {"type": "object",
                  "properties": {name: ({"type": "array", "items": {"type": "string"}} if kind == "list"
                                        else {"type": "string"}) for name, kind in draft.summary_fields.items()},
                  "required": list(draft.summary_fields)}
        user = render(self.prompt["Summary"], FACTS=json.dumps(facts, ensure_ascii=False, indent=1))
        texts = None
        for attempt in range(self.retries + 1):
            answer = self._ask(user, schema, attempt)
            texts = validate_summary(answer, draft.summary_fields)
            if texts is not None:
                break
            if answer is not None:  # valid JSON, but the summary is empty
                self.stats.invalid_answers += 1
        if texts is not None:
            draft.report["texts"] = texts

    def finish(self, draft: Draft) -> None:
        """Count what is missing; also after Ollama stopped answering in the middle."""
        entries = entry_list(draft.report)
        self.stats.entries = len(entries)
        self.stats.texts_missing = sum(1 for entry in entries if entry.get("title") is None)
        if "texts" not in draft.report:
            self.stats.summary_missing = True
            draft.report["texts"] = {name: None for name in draft.summary_fields}
        draft.report["texts_missing"] = self.stats.texts_missing

    def _fit(self, facts: dict) -> dict:
        """Shorten the longest lists (and the patch sample) until the summary request fits the context."""
        facts = json.loads(json.dumps(facts))
        template = len(render(self.prompt["Summary"], FACTS=""))
        while len(json.dumps(facts, ensure_ascii=False, indent=1)) + template > self.budget:
            sample = facts.get("patch_sample") or ""
            lists = [(len(value), key) for key, value in facts.items()
                     if isinstance(value, list) and len(value) > SUMMARY_LIST_MIN]
            if len(sample) > 2000:
                facts["patch_sample"] = sample[: len(sample) // 2]
            elif lists:
                _, key = max(lists)
                kept = len(facts[key]) // 2
                facts[f"{key}_left_out"] = facts.get(f"{key}_left_out", 0) + len(facts[key]) - kept
                facts[key] = facts[key][:kept]
            else:
                self.stats.notes.append("the summary facts do not fit the context window; sent anyway")
                break
        return facts

    # request

    def _ask(self, user: str, schema: dict, attempt: int) -> dict | None:
        options = self.config.options()
        if attempt:  # the same seed gives the same answer again
            options["seed"] = self.config.seed + attempt
        exchange = self.client.chat(self.config.model, self.system, user, schema, options=options,
                                    think=self.config.think)
        self.log(exchange)
        self.stats.requests += 1
        self.stats.duration_seconds += exchange.duration_seconds
        if exchange.response is None:
            self.stats.failed_requests += 1
            return None
        self.stats.prompt_tokens += int(exchange.response.get("prompt_eval_count") or 0)
        self.stats.output_tokens += int(exchange.response.get("eval_count") or 0)
        content = (exchange.response.get("message") or {}).get("content") or ""
        answer = parse_json(content)
        if answer is None:  # no JSON, e.g. cut off at num_predict
            self.stats.invalid_answers += 1
        return answer


def parse_json(content: str) -> dict | None:
    try:
        data = json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(content[start: end + 1])
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return None if value.lower() in ("", "null", "none", "n/a", "-") else value


def validate_texts(written: dict, label: str) -> dict | None:
    """The texts of one entry, or None if a required text is missing.

    Action labels need a title, a description and a recommendation; the
    others a title and a reason. Texts that do not belong to the label are
    dropped.
    """
    texts = {name: _text(written.get(name)) for name in TEXT_FIELDS}
    if texts["title"] is None:
        return None
    if label in ACTION_LABELS:
        if texts["description"] is None or texts["recommendation"] is None:
            return None
        texts["reason"] = None
    else:
        if texts["reason"] is None:
            return None
        texts["description"] = texts["recommendation"] = None
    return texts


def validate_summary(answer: dict | None, fields: dict[str, str]) -> dict | None:
    if answer is None:
        return None
    texts: dict[str, object] = {}
    for name, kind in fields.items():
        if kind == "list":
            value = answer.get(name)
            texts[name] = [text for text in (_text(entry) for entry in value) if text] \
                if isinstance(value, list) else []
        else:
            texts[name] = _text(answer.get(name))
    return texts if texts.get("summary_text") else None


def write_texts(draft: Draft, writer: TextWriter) -> ReportStats:
    try:
        writer.write_entries(draft)
        writer.write_summary(draft)
    finally:
        writer.finish(draft)
    return writer.stats

"""Step 6, preparation: the items the decision model labels, built by Python.

Every item has an id, a kind and the facts the models see (`data`). Python
decides what is deterministic:
- security: the findings of step 4 and those of step 3 whose tool category
  is security, correctness (bugs, OpenGrep) or reliability (bugs, SonarQube),
  or that have no category, get an id (S-0001 = the first finding of step 3's
  findings.json, D-0001). Bugs can have security consequences: better rated
  too high than overlooked.
  Duplicates are grouped: static findings at overlapping lines of the same
  file with the same rule or a shared CWE (also across tools); dependency
  findings of the same package with the same advisory (its id or an alias)
  and the same or an unknown version. One finding per group is the canonical
  one; the others get the label DUPLICATE without asking the model.
- code quality: the lint findings of step 2 and the other findings of step 3
  (maintainability, best-practice, portability, performance, ...). They repeat, so they are grouped by tool, rule and scope (L-001),
  after removing exact duplicates. The model labels groups.
- test quality: groups of failed tests with the same cause (T-001), weakly
  tested files (F-0001) and surviving mutants (M-0001).
The scope of a file (main, test, generated, vendored, docs/examples, ci,
scripts) comes from its path and the generated files step 2 left out.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from scheduler.dependencies.model import SEVERITIES as DEPENDENCY_SEVERITIES
from scheduler.evaluation.config import InputConfig
from scheduler.testing.sources import OTHER_FOLDERS, TEST_FOLDERS

LINT_DIR = "step2-linting"
STATIC_DIR = "step3-static-analysis"
DEPENDENCIES_DIR = "step4-dependencies"
TESTS_DIR = "step5-tests"
EVALUATION_DIR = "step6-llm-evaluation"

# The limits of what the models see are in InputConfig ([llm.input] in code-analysis.toml).
MAX_SOURCE_BYTES = 2_000_000  # larger source files are not read for code excerpts
GROUP_MESSAGE_CHARS = 200  # each message of a group of findings
TEST_NAME_CHARS = 200  # each test name of a group of failed tests

VENDORED_FOLDERS = {"vendor", "vendored", "third_party", "thirdparty", "third-party", "external", "extern",
                    "node_modules", "bower_components"}
CI_FOLDERS = {".github", ".gitlab", ".circleci", ".buildkite", ".azure-pipelines"}
SCRIPT_FOLDERS = {"scripts", "script", "tools", "tooling", "buildsrc"}
_TEST_FOLDERS = {folder.lower() for folder in TEST_FOLDERS}
_TEST_NAME = re.compile(r"(\.(test|spec|tests|specs)\.[^.]+$)|(^test_.*\.py$)|(_test\.(py|go|rs|c|cc|cpp)$)"
                        r"|((tests?|spec)\.(java|kt|cs|scala|groovy)$)", re.IGNORECASE)
_DECLARATION = re.compile(r"^\s*(export\s+|public\s+|protected\s+|private\s+|internal\s+|static\s+|abstract\s+"
                          r"|final\s+|async\s+|pub(\([^)]*\))?\s+|default\s+)*"
                          r"(class|interface|record|enum|struct|trait|impl|def|fn|function|module|namespace)\b")
# Categories of step 3 evaluated as security; a finding without a category is, too.
# correctness (OpenGrep) and reliability (SonarQube) are bugs, which can have security consequences.
SECURITY_CATEGORIES = ("security", "correctness", "reliability")
SEVERITY_RANK = {"critical": 5, "high": 4, "error": 4, "medium": 3, "warning": 3, "low": 2, "note": 2,
                 "info": 1, "unknown": 1}


@dataclass
class AreaItems:
    """The items of one area, with the facts the models and the reports need."""

    area: str
    items: list[dict] = field(default_factory=list)
    # Findings Python already labeled as DUPLICATE: id -> id of the canonical item.
    duplicates: dict[str, str] = field(default_factory=dict)
    context: dict = field(default_factory=dict)  # short run facts for the decision model
    facts: dict = field(default_factory=dict)  # everything the report needs besides the items
    notes: list[str] = field(default_factory=list)
    available: bool = True  # False if the steps this area needs did not run

    def to_json(self) -> dict:
        return {"area": self.area, "available": self.available, "notes": self.notes, "context": self.context,
                "items": self.items, "duplicates": self.duplicates}


# --- reading the run ---


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def project_of(run_dir: Path) -> Path | None:
    """The analysed project: from run.json, else from a step's summary.json."""
    for path in [run_dir / "run.json"] + [run_dir / folder / "summary.json"
                                          for folder in (LINT_DIR, STATIC_DIR, DEPENDENCIES_DIR, TESTS_DIR)]:
        data = read_json(path, {})
        if isinstance(data, dict) and data.get("project"):
            return Path(data["project"])
    return None


class SourceReader:
    """Short excerpts of the project's files, for the models to see the code."""

    def __init__(self, project: Path | None, limits: InputConfig = InputConfig()):
        self.project = project.resolve() if project and project.is_dir() else None
        self.limits = limits
        self._lines: dict[str, list[str] | None] = {}

    def lines(self, file: str | None) -> list[str] | None:
        if self.project is None or not file:
            return None
        if file not in self._lines:
            self._lines[file] = None
            path = (self.project / file).resolve()
            try:
                if path.is_relative_to(self.project) and path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES:
                    self._lines[file] = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                pass
        return self._lines[file]

    def snippet(self, file: str | None, line: int | None, end_line: int | None = None) -> str | None:
        lines = self.lines(file)
        if not lines or not line or line < 1 or line > len(lines):
            return None
        last = min(max(end_line or line, line), line + 4)
        context = self.limits.code_context_lines
        start, stop = max(1, line - context), min(len(lines), last + context)
        text = "\n".join(f"{number}: {lines[number - 1].rstrip()}" for number in range(start, stop + 1))
        return clip(text, self.limits.code_chars)

    def declarations(self, file: str) -> str | None:
        """The file's classes and functions, so the model sees what it does."""
        lines = self.lines(file)
        if not lines:
            return None
        found = [line.strip() for line in lines if _DECLARATION.match(line)]
        return clip("\n".join(found), self.limits.declaration_chars) if found else None


def clip(text: str | None, limit: int) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def path_scope(file: str | None, generated: set[str]) -> str:
    if not file:
        return "unknown"
    if file in generated:
        return "generated"
    path = PurePosixPath(file)
    folders = {part.lower() for part in path.parts[:-1]}
    if folders & VENDORED_FOLDERS:
        return "vendored"
    if folders & _TEST_FOLDERS or "src/test/" in file.lower() or _TEST_NAME.search(path.name):
        return "test"
    if folders & OTHER_FOLDERS:
        return "docs/examples"
    if path.parts and path.parts[0].lower() in CI_FOLDERS:
        return "ci"
    if folders & SCRIPT_FOLDERS:
        return "scripts"
    return "main"


def generated_files(run_dir: Path) -> set[str]:
    excluded = read_json(run_dir / LINT_DIR / "excluded-files.json", [])
    return {entry["file"] for entry in excluded if isinstance(entry, dict) and entry.get("file")}


def highest(severities: Iterable[str | None]) -> str | None:
    known = [severity for severity in severities if severity]
    return max(known, key=lambda severity: SEVERITY_RANK.get(severity, 0)) if known else None


def is_security(finding: dict) -> bool:
    """A finding of step 3 is evaluated as security if its tool's category is in SECURITY_CATEGORIES or missing."""
    category = str(finding.get("category") or "").strip().lower()
    return category in ("", *SECURITY_CATEGORIES)


def tool_facts(summary: dict) -> list[dict]:
    facts = []
    for tool in summary.get("tools", []):
        entry = {"name": tool.get("tool"), "version": tool.get("version"), "status": tool.get("status")}
        if tool.get("manifest"):
            entry["manifest"] = tool["manifest"]
        if tool.get("language"):
            entry["language"] = tool["language"]
        if tool.get("notes"):
            entry["notes"] = [clip(note, 300) for note in tool["notes"][:3]]
        facts.append(entry)
    return facts


# --- security: steps 3 and 4 ---


def security_items(run_dir: Path, reader: SourceReader, limits: InputConfig = InputConfig()) -> AreaItems:
    area = AreaItems("security")
    static_summary = read_json(run_dir / STATIC_DIR / "summary.json", None)
    dependency_summary = read_json(run_dir / DEPENDENCIES_DIR / "summary.json", None)
    if static_summary is None and dependency_summary is None:
        area.available = False
        area.notes.append("Steps 3 and 4 did not run: no results to evaluate.")
        return area
    generated = generated_files(run_dir)
    if static_summary is None:
        area.notes.append("Step 3 (static code analysis) did not run.")
    else:
        findings = read_json(run_dir / STATIC_DIR / "findings.json", [])
        _static_items(area, findings, reader, generated, limits)
        area.facts["static_tools"] = tool_facts(static_summary)
        area.facts["static_complete"] = static_summary.get("complete")
        area.facts["static_findings"] = len(findings)
        area.facts["static_in_code_quality"] = sum(1 for finding in findings if not is_security(finding))
        area.facts["rulesets"] = sorted({f"{ruleset.get('name')} ({str(ruleset.get('commit', ''))[:7]})"
                                         for tool in static_summary.get("tools", [])
                                         for ruleset in tool.get("rulesets", []) if ruleset.get("name")})
    if dependency_summary is None:
        area.notes.append("Step 4 (dependencies) did not run.")
    else:
        findings = read_json(run_dir / DEPENDENCIES_DIR / "findings.json", [])
        _dependency_items(area, findings, limits)
        area.facts["dependency_tools"] = tool_facts(dependency_summary)
        area.facts["dependency_complete"] = dependency_summary.get("complete")
        area.facts["dependency_findings"] = len(findings)
        area.facts["coverage"] = [
            {key: entry.get(key) for key in ("manager", "manifest", "lockfile", "lockfile_state", "osv", "audit")}
            for entry in dependency_summary.get("coverage", [])]
    area.context = {
        "static_tools": [f"{tool['name']} {tool.get('version') or ''} ({tool['status']})".strip()
                         for tool in area.facts.get("static_tools", [])],
        "dependency_tools": [f"{tool['name']} ({tool['status']})" for tool in area.facts.get("dependency_tools", [])],
        "package_managers": sorted({f"{entry['manager']} ({entry['manifest']})"
                                    for entry in area.facts.get("coverage", [])})[:10],
    }
    return area


def _cwe_ids(cwe: Iterable[str]) -> set[str]:
    return {match.group(0).upper() for text in cwe for match in [re.match(r"CWE-\d+", text, re.IGNORECASE)] if match}


def _static_items(area: AreaItems, findings: list[dict], reader: SourceReader, generated: set[str],
                  limits: InputConfig) -> None:
    """The security findings of step 3; the ids count all findings, so S-0005 is the fifth of findings.json."""
    ids = [f"S-{index + 1:04d}" for index in range(len(findings))]
    chosen = [index for index, finding in enumerate(findings) if is_security(finding)]
    for group in _union_static([findings[index] for index in chosen]):
        members = [chosen[position] for position in group]
        canonical = max(members, key=lambda index: _static_precision(findings[index], index))
        finding = findings[canonical]
        duplicates = [index for index in members if index != canonical]
        for index in duplicates:
            area.duplicates[ids[index]] = ids[canonical]
        data = {
            "kind": "static finding",
            "tool": finding.get("tool"),
            "ruleset": finding.get("ruleset"),
            "rule": finding.get("rule"),
            "severity": highest(findings[index].get("severity") for index in members),
            "tool_severity": finding.get("tool_severity"),
            "category": finding.get("category"),
            "subcategory": finding.get("subcategory") or None,
            "cwe": sorted(_cwe_ids(finding.get("cwe") or [])) or None,
            "confidence": finding.get("confidence"),
            "message": clip(finding.get("message"), limits.message_chars),
            "file": finding.get("file"),
            "line": finding.get("line"),
            "scope": path_scope(finding.get("file"), generated),
            "code": reader.snippet(finding.get("file"), finding.get("line"), finding.get("end_line")),
        }
        if duplicates:
            data["also_reported_as"] = sorted({f"{findings[index].get('tool')}: {findings[index].get('rule')}"
                                               for index in duplicates})
        area.items.append({"id": ids[canonical], "kind": "static", "data": _compact(data),
                           "duplicates": [ids[index] for index in duplicates],
                           "fingerprints": [findings[index].get("fingerprint") for index in members]})


def _union_static(findings: list[dict]) -> list[list[int]]:
    """Groups of findings that describe the same issue (each finding in exactly one group)."""
    parent = list(range(len(findings)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    by_file: dict[str, list[int]] = defaultdict(list)
    for index, finding in enumerate(findings):
        if finding.get("file") and finding.get("line"):
            by_file[finding["file"]].append(index)
    for indices in by_file.values():
        spans = sorted(indices, key=lambda index: findings[index]["line"])
        for position, first in enumerate(spans):
            a = findings[first]
            a_end = max(a.get("end_line") or a["line"], a["line"])
            a_cwe = _cwe_ids(a.get("cwe") or [])
            for second in spans[position + 1:]:
                b = findings[second]
                if b["line"] > a_end:
                    break
                if a.get("rule") == b.get("rule") or a_cwe & _cwe_ids(b.get("cwe") or []):
                    parent[find(second)] = find(first)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(findings)):
        groups[find(index)].append(index)
    return sorted(groups.values(), key=lambda members: min(members))


def _static_precision(finding: dict, index: int) -> tuple:
    span = max((finding.get("end_line") or finding.get("line") or 0) - (finding.get("line") or 0), 0)
    return (bool(finding.get("cwe")), "vuln" in (finding.get("subcategory") or []), -span, -index)


def _dependency_items(area: AreaItems, findings: list[dict], limits: InputConfig) -> None:
    ids = [f"D-{index + 1:04d}" for index in range(len(findings))]
    for members in _union_dependencies(findings):
        canonical = max(members, key=lambda index: _dependency_precision(findings[index], index))
        finding = findings[canonical]
        duplicates = [index for index in members if index != canonical]
        for index in duplicates:
            area.duplicates[ids[index]] = ids[canonical]
        advisories = sorted({identifier for index in members
                             for identifier in [findings[index].get("id"), *(findings[index].get("aliases") or [])]
                             if identifier})
        severities = [findings[index].get("severity") or "unknown" for index in members]
        data = {
            "kind": "dependency finding",
            "tools": sorted({findings[index].get("tool") for index in members}),
            "ecosystem": finding.get("ecosystem"),
            "package": finding.get("package"),
            "version": finding.get("version"),
            "advisories": advisories,
            "severity": min(severities, key=lambda severity: DEPENDENCY_SEVERITIES.index(severity)
                            if severity in DEPENDENCY_SEVERITIES else len(DEPENDENCY_SEVERITIES)),
            "cvss_score": finding.get("cvss_score"),
            "summary": clip(finding.get("summary"), limits.message_chars),
            "fixed_versions": sorted({version for index in members
                                      for version in findings[index].get("fixed_versions") or []}),
            "manifests": sorted({findings[index].get("manifest") for index in members if findings[index].get("manifest")}),
            "scope": finding.get("scope"),
            "configurations": (finding.get("configurations") or [])[:5] or None,
            "url": finding.get("url"),
        }
        area.items.append({"id": ids[canonical], "kind": "dependency", "data": _compact(data),
                           "duplicates": [ids[index] for index in duplicates]})


def _union_dependencies(findings: list[dict]) -> list[list[int]]:
    groups: list[list[int]] = []
    by_package: dict[tuple, list[list[int]]] = defaultdict(list)
    for index, finding in enumerate(findings):
        key = (str(finding.get("ecosystem", "")).lower(), finding.get("package"))
        identifiers = {finding.get("id"), *(finding.get("aliases") or [])} - {None}
        version = finding.get("version")
        match = None
        for group in by_package[key]:
            for other in group:
                other_finding = findings[other]
                other_ids = {other_finding.get("id"), *(other_finding.get("aliases") or [])} - {None}
                other_version = other_finding.get("version")
                if identifiers & other_ids and (version == other_version or version is None or other_version is None):
                    match = group
                    break
            if match:
                break
        if match is None:
            match = []
            by_package[key].append(match)
            groups.append(match)
        match.append(index)
    return groups


def _dependency_precision(finding: dict, index: int) -> tuple:
    return (finding.get("tool") == "osv-scanner", finding.get("version") is not None,
            finding.get("cvss_score") is not None, len(finding.get("fixed_versions") or []), -index)


# --- code quality: step 2 and the other findings of step 3 ---


def code_quality_items(run_dir: Path, reader: SourceReader, limits: InputConfig = InputConfig()) -> AreaItems:
    area = AreaItems("code_quality")
    lint_summary = read_json(run_dir / LINT_DIR / "summary.json", None)
    static_summary = read_json(run_dir / STATIC_DIR / "summary.json", None)
    if lint_summary is None and static_summary is None:
        area.available = False
        area.notes.append("Steps 2 and 3 did not run: no results to evaluate.")
        return area
    generated = generated_files(run_dir)
    lint_findings: list[dict] = []
    static_findings: list[dict] = []
    if lint_summary is None:
        area.notes.append("Step 2 (linters and formatters) did not run.")
    else:
        lint_findings = read_json(run_dir / LINT_DIR / "findings.json", [])
    if static_summary is None:
        area.notes.append("Step 3 (static code analysis) did not run.")
    else:
        static_findings = [finding for finding in read_json(run_dir / STATIC_DIR / "findings.json", [])
                           if not is_security(finding)]
    unique: dict[tuple, tuple[str, dict]] = {}
    for source, findings in (("lint", lint_findings), ("static", static_findings)):
        for finding in findings:
            key = (source, *(finding.get(name) for name in ("tool", "rule", "file", "line", "column", "message")))
            unique.setdefault(key, (source, finding))
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for source, finding in unique.values():
        scope = path_scope(finding.get("file"), generated)
        groups[(source, finding.get("tool"), finding.get("rule"), scope)].append(finding)
    scope_order = ["main", "scripts", "ci", "test", "docs/examples", "vendored", "generated", "unknown"]
    ordered = sorted(groups.items(), key=lambda entry: (scope_order.index(entry[0][3]), -len(entry[1]),
                                                        str(entry[0][1]), str(entry[0][2])))
    for number, ((source, tool, rule, scope), members) in enumerate(ordered, start=1):
        samples = _spread_samples(members, limits.group_samples)
        messages = list(dict.fromkeys(clip(member.get("message"), GROUP_MESSAGE_CHARS) for member in members))
        static = source == "static"
        data = {
            "kind": "group of static analysis findings" if static else "group of lint findings",
            "language": members[0].get("language"),
            "tool": tool,
            "rule": rule,
            # The tool's own classification (step 3 only): maintainability, reliability, correctness, ...
            "category": members[0].get("category") if static else None,
            "subcategory": (members[0].get("subcategory") or None) if static else None,
            "scope": scope,
            "count": len(members),
            "files": len({member.get("file") for member in members}),
            "severity": highest(member.get("severity") for member in members),
            "fixed_by_the_tool": None if static else sum(1 for member in members if member.get("fixed")),
            "messages": messages[:limits.group_messages],
            "samples": [_compact({"file": sample.get("file"), "line": sample.get("line"),
                                  "code": reader.snippet(sample.get("file"), sample.get("line"))
                                  if position < limits.group_samples_with_code else None})
                        for position, sample in enumerate(samples)],
        }
        # Every location of the group, for the report and the audit; the models only see the samples.
        area.items.append({"id": f"L-{number:03d}", "kind": "static_group" if static else "lint_group",
                           "data": _compact(data), "locations": _locations(members)})
    lint_tools = tool_facts(lint_summary) if lint_summary else []
    static_tools = tool_facts(static_summary) if static_summary else []
    area.facts = {
        "tools": lint_tools + static_tools,
        "complete": all(summary.get("complete") for summary in (lint_summary, static_summary) if summary),
        "input_findings": len(lint_findings) + len(static_findings),
        "lint_findings": len(lint_findings),
        "static_findings": len(static_findings),
        "exact_duplicates": len(lint_findings) + len(static_findings) - len(unique),
        "changed_files": (lint_summary or {}).get("changed_files", []),
        "created_files": (lint_summary or {}).get("created_files", []),
    }
    area.context = {
        "linters_and_formatters": [f"{tool['name']} {tool.get('version') or ''} for {tool.get('language')} "
                                   f"({tool['status']})" for tool in lint_tools],
        "static_analysis": [f"{tool['name']} {tool.get('version') or ''} ({tool['status']})".replace("  ", " ")
                            for tool in static_tools],
        "files_changed_by_the_tools": len(area.facts["changed_files"]),
        "generated_files_left_out": len(generated),
    }
    return area


def _locations(members: list[dict]) -> list[dict]:
    """File and line of every finding, sorted by file and line."""
    found = {(str(member.get("file") or ""), member.get("line")) for member in members}
    return [{"file": file or None, "line": line}
            for file, line in sorted(found, key=lambda entry: (entry[0], entry[1] if entry[1] is not None else -1))]


def _spread_samples(members: list[dict], count: int) -> list[dict]:
    """Samples from different files first."""
    seen: set[str | None] = set()
    first = []
    for member in members:
        if member.get("file") not in seen:
            seen.add(member.get("file"))
            first.append(member)
    rest = [member for member in members if member not in first]
    return (first + rest)[:count]


# --- test quality: step 5 ---


def test_items(run_dir: Path, reader: SourceReader, limits: InputConfig = InputConfig()) -> AreaItems:
    area = AreaItems("test_quality")
    summary = read_json(run_dir / TESTS_DIR / "summary.json", None)
    if summary is None:
        area.available = False
        area.notes.append("Step 5 did not run: no results to evaluate.")
        return area
    tests = read_json(run_dir / TESTS_DIR / "tests.json", [])
    coverage = read_json(run_dir / TESTS_DIR / "coverage.json", [])
    mutants = read_json(run_dir / TESTS_DIR / "mutants.json", [])
    _failure_items(area, tests, limits)
    _weak_file_items(area, coverage, mutants, reader, limits)
    _mutant_items(area, mutants)
    tests_section = summary.get("tests", {})
    area.facts = {
        "complete": summary.get("complete"),
        "errors": summary.get("errors", []),
        "detection": {key: value for key, value in summary.get("detection", {}).items() if key != "suites"},
        "suites": [{key: suite.get(key) for key in ("language", "root", "manager", "manifest", "tests_found",
                                                     "frameworks")} | {"test_files": len(suite.get("test_files", []))}
                   for suite in summary.get("detection", {}).get("suites", [])],
        "network": tests_section.get("network"),
        "runs": [{key: run.get(key) for key in ("suite", "language", "tool", "version", "status", "tests", "counts",
                                                "duration_seconds", "notes")}
                 for run in tests_section.get("runs", [])],
        "differences_to_head": tests_section.get("differences_to_head") or [],
        "coverage": [{key: run.get(key) for key in ("suite", "tool", "version", "status", "files", "files_not_loaded",
                                                    "lines_total", "lines_covered", "branches_total",
                                                    "branches_covered", "line_rate", "branch_rate", "notes")}
                     for run in summary.get("coverage", [])],
        "mutation": ({key: value for key, value in summary["mutation"].items() if key != "runs"}
                     | {"runs": [{key: run.get(key) for key in ("suite", "tool", "version", "status", "sampled",
                                                                "candidate_files", "mutants", "counts", "score",
                                                                "notes")} | {"files": len(run.get("files", []))}
                                 for run in summary["mutation"].get("runs", [])]})
        if summary.get("mutation") else None,
        "tests_total": len(tests),
    }
    area.context = {
        "suites": [f"{suite['language']} in {suite['root']} ({suite['manager']}, "
                   f"{', '.join(suite.get('frameworks') or []) or 'no framework found'})"
                   for suite in area.facts["suites"]][:10],
        "tests_ran_with_network": bool(area.facts["network"]),
        "run_notes": [note for run in area.facts["runs"] for note in (run.get("notes") or [])][:5],
    }
    return area


test_items.__test__ = False  # not a pytest test when imported into a test module


def failure_cause(message: str) -> str:
    """The first line of a failure message without numbers, quoted text and paths: the cause."""
    first = next((line.strip() for line in (message or "").splitlines() if line.strip()), "")
    first = re.sub(r"(['\"`]).*?\1", "S", first)
    first = re.sub(r"(/[\w.@-]+)+", "PATH", first)
    first = re.sub(r"\d+", "N", first)
    return first[:160]


def _failure_items(area: AreaItems, tests: list[dict], limits: InputConfig) -> None:
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for test in tests:
        if test.get("outcome") in ("failed", "error"):
            groups[(test.get("suite"), failure_cause(test.get("message", "")))].append(test)
    ordered = sorted(groups.items(), key=lambda entry: (-len(entry[1]), str(entry[0])))
    for number, ((suite, cause), members) in enumerate(ordered, start=1):
        data = {
            "kind": "group of failed tests with the same failure message",
            "suite": suite,
            "count": len(members),
            "outcomes": dict(sorted({outcome: sum(1 for m in members if m.get("outcome") == outcome)
                                     for outcome in ("failed", "error")}.items())),
            "tests": [clip(member.get("id"), TEST_NAME_CHARS) for member in members[:limits.failed_tests_per_group]],
            "message": clip(members[0].get("message"), limits.failure_message_chars),
        }
        area.items.append({"id": f"T-{number:03d}", "kind": "test_failure", "data": _compact(data),
                           "cause": cause, "tests": [member.get("id") for member in members]})


def _weak_file_items(area: AreaItems, coverage: list[dict], mutants: list[dict], reader: SourceReader,
                     limits: InputConfig) -> None:
    undetected: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for mutant in mutants:
        if mutant.get("status") in ("Survived", "NoCoverage"):
            undetected[(mutant.get("suite"), mutant.get("file"))][mutant["status"]] += 1
    number = 0
    for suite in coverage:
        weak: list[dict] = []
        for file in suite.get("files", []):
            total = file.get("lines_total") or 0
            rate = (file.get("lines_covered") or 0) / total if total else None
            if total and rate is not None and rate < limits.weak_line_rate:
                weak.append({"file": file["file"], "lines_total": total, "lines_covered": file.get("lines_covered"),
                             "line_rate": round(rate, 3), "branches_total": file.get("branches_total"),
                             "branches_covered": file.get("branches_covered"), "loaded_by_tests": True})
        weak += [{"file": file, "loaded_by_tests": False} for file in suite.get("not_loaded", [])]
        for entry in sorted(weak, key=lambda entry: entry["file"]):
            number += 1
            counts = undetected.get((suite.get("suite"), entry["file"]), {})
            data = {"kind": "source file with little or no test coverage", "suite": suite.get("suite"), **entry,
                    "mutants_not_detected": dict(counts) or None, "scope": path_scope(entry["file"], set()),
                    "declarations": reader.declarations(entry["file"])}
            area.items.append({"id": f"F-{number:04d}", "kind": "weak_file", "data": _compact(data)})


def _mutant_items(area: AreaItems, mutants: list[dict]) -> None:
    survived = [mutant for mutant in mutants if mutant.get("status") == "Survived"]
    for number, mutant in enumerate(survived, start=1):
        data = {"kind": "mutant the tests did not detect (survived)", "suite": mutant.get("suite"),
                "file": mutant.get("file"), "line": mutant.get("line"), "mutator": mutant.get("mutator"),
                "change": clip(mutant.get("replacement"), 200), "original_line": clip(mutant.get("original"), 200)}
        area.items.append({"id": f"M-{number:04d}", "kind": "mutant", "data": _compact(data)})


def _compact(data: dict) -> dict:
    """Leave out empty values: they cost space in every request."""
    return {key: value for key, value in data.items() if value not in (None, "", [], {})}

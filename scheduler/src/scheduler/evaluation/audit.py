"""The final audit: audit.md and audit.json in the run folder.

Built by Python alone from run.json, the summary.json of steps 2 to 6 and the
report.json of each area of step 6. Tables, numbers and order come from the
reports; the texts the reasoning model wrote are used as written. Headings
are German when [llm] report_language is German, English otherwise.

Runs at the end of step 6, and on its own for an evaluated run folder:
    python -m scheduler.evaluation.audit <run folder>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scheduler.evaluation.items import DEPENDENCIES_DIR, EVALUATION_DIR, LINT_DIR, STATIC_DIR, TESTS_DIR, read_json

STEP_DIR = EVALUATION_DIR
AREAS = ("security", "code_quality", "test_quality")
STEPS = ((2, LINT_DIR), (3, STATIC_DIR), (4, DEPENDENCIES_DIR), (5, TESTS_DIR), (6, STEP_DIR))
AUDIT_MD = "audit.md"
AUDIT_JSON = "audit.json"

TEXTS = {
    "en": {
        "title": "Code analysis audit", "overview": "1. Overview", "key_figures": "2. Key figures",
        "security": "3. Security", "code_quality": "4. Code quality", "test_quality": "5. Test quality",
        "review": "6. Review list", "method": "7. Method and limits",
        "project": "Project", "commit": "Commit", "analysed": "Analysed", "languages": "Languages",
        "models": "Models", "step": "Step", "status": "Status", "complete": "complete", "incomplete": "incomplete",
        "not_run": "not run", "steps": {2: "2 – linters and formatters", 3: "3 – static code analysis",
                                        4: "4 – dependencies", 5: "5 – tests", 6: "6 – LLM evaluation"},
        "decision_model": "decisions", "report_model": "reports",
        "not_evaluated": "Not evaluated.", "limits": "Limits of this run", "id": "Id", "severity": "Severity",
        "title_col": "Title", "location": "Location / package", "recommendation": "Recommendation",
        "label": "Label", "reason": "Reason", "real_findings": "REAL findings", "filtered": "Filtered",
        "real_groups": "REAL groups", "tool_rule": "Tool / rule", "scope": "Scope", "findings": "Findings",
        "formatter_changes": "Formatter changes", "excluded": "Excluded files", "tools": "Tools",
        "files_changed": "files changed", "kinds": "Kinds of change", "example": "Example",
        "count": "Count", "examples": "Examples", "suite": "Suite", "tool": "Tool",
        "tests": "Test runs", "coverage": "Coverage", "mutation": "Mutation testing", "lines": "Lines",
        "branches": "Branches", "score": "Score", "files": "Files", "sample": "sample", "all_files": "all files",
        "critical_files": "Critical files with little or no test coverage", "file": "File",
        "project_failures": "Failed tests, cause in the project", "test_gaps": "Changes no test detects, TEST_GAP",
        "other_entries": "Other entries", "area": "Area", "confidence": "Confidence", "hint": "Review hint",
        "low_confidence": "low confidence", "no_review": "Nothing to review.",
        "security_line": "Security: {real} REAL findings{severity} of {total} findings; {filtered} filtered, "
                         "{duplicates} duplicates merged.",
        "lint_line": "Code quality: {real_groups} REAL groups with {real} findings of {total} findings "
                     "({groups} groups); the formatters changed {files} files (+{added} / −{removed} lines).",
        "tests_line": "Tests: {counts}.",
        "coverage_line": "Line coverage: {values}.", "mutation_line": "Mutation score: {values}.",
        "review_line": "To review: {low} decisions with low confidence, {hints} review hints.",
        "no_tests": "no test ran",
        "method_labels": "The labels come from a local language model and can be wrong. Decisions with low "
                         "confidence and entries with a review hint are in the review list.",
        "texts_missing": "{count} entries have no texts (the reasoning model gave no valid answer).",
        "skipped": "skipped", "settings": "Settings",
    },
    "de": {
        "title": "Code-Analyse-Audit", "overview": "1. Überblick", "key_figures": "2. Kennzahlen",
        "security": "3. Sicherheit", "code_quality": "4. Codequalität", "test_quality": "5. Testqualität",
        "review": "6. Zu prüfen", "method": "7. Methode und Grenzen",
        "project": "Projekt", "commit": "Commit", "analysed": "Analysiert", "languages": "Sprachen",
        "models": "Modelle", "step": "Schritt", "status": "Status", "complete": "vollständig",
        "incomplete": "unvollständig", "not_run": "nicht gelaufen",
        "steps": {2: "2 – Linter und Formatter", 3: "3 – statische Codeanalyse", 4: "4 – Abhängigkeiten",
                  5: "5 – Tests", 6: "6 – LLM-Auswertung"},
        "decision_model": "Entscheidungen", "report_model": "Berichte",
        "not_evaluated": "Nicht ausgewertet.", "limits": "Grenzen dieses Laufs", "id": "Id",
        "severity": "Schwere", "title_col": "Titel", "location": "Ort / Paket", "recommendation": "Empfehlung",
        "label": "Label", "reason": "Begründung", "real_findings": "REAL-Befunde", "filtered": "Herausgefiltert",
        "real_groups": "REAL-Gruppen", "tool_rule": "Werkzeug / Regel", "scope": "Bereich", "findings": "Befunde",
        "formatter_changes": "Änderungen der Formatter", "excluded": "Ausgeschlossene Dateien",
        "tools": "Werkzeuge", "files_changed": "Dateien geändert", "kinds": "Art der Änderungen",
        "example": "Beispiel", "count": "Anzahl", "examples": "Beispiele", "suite": "Suite", "tool": "Werkzeug",
        "tests": "Testläufe", "coverage": "Testabdeckung", "mutation": "Mutationstests", "lines": "Zeilen",
        "branches": "Zweige", "score": "Score", "files": "Dateien", "sample": "Stichprobe",
        "all_files": "alle Dateien",
        "critical_files": "Kritische Dateien mit wenig oder keiner Testabdeckung", "file": "Datei",
        "project_failures": "Fehlgeschlagene Tests, Ursache im Projekt",
        "test_gaps": "Änderungen, die kein Test erkennt, TEST_GAP", "other_entries": "Weitere Einträge",
        "area": "Bereich", "confidence": "Konfidenz", "hint": "Prüfhinweis", "low_confidence": "niedrige Konfidenz",
        "no_review": "Nichts zu prüfen.",
        "security_line": "Sicherheit: {real} REAL-Befunde{severity} von {total} Befunden; {filtered} "
                         "herausgefiltert, {duplicates} Duplikate zusammengeführt.",
        "lint_line": "Codequalität: {real_groups} REAL-Gruppen mit {real} von {total} Befunden ({groups} Gruppen); "
                     "die Formatter änderten {files} Dateien (+{added} / −{removed} Zeilen).",
        "tests_line": "Tests: {counts}.",
        "coverage_line": "Zeilenabdeckung: {values}.", "mutation_line": "Mutations-Score: {values}.",
        "review_line": "Zu prüfen: {low} Entscheidungen mit niedriger Konfidenz, {hints} Prüfhinweise.",
        "no_tests": "kein Test lief",
        "method_labels": "Die Labels stammen von einem lokalen Sprachmodell und können falsch sein. "
                         "Entscheidungen mit niedriger Konfidenz und Einträge mit Prüfhinweis stehen in der "
                         "Liste „Zu prüfen“.",
        "texts_missing": "{count} Einträge haben keine Texte (das Reasoning-Modell gab keine gültige Antwort).",
        "skipped": "übersprungen", "settings": "Einstellungen",
    },
}


def language_of(report_language: str | None) -> str:
    name = (report_language or "").strip().lower()
    return "de" if name.startswith(("german", "deutsch")) or name in ("de", "de-de") else "en"


def write_audit(run_dir: Path, evaluation: dict | None = None) -> Path:
    """Write audit.md and audit.json; returns the path of audit.md."""
    evaluation = evaluation if evaluation is not None else read_json(run_dir / STEP_DIR / "summary.json", {})
    audit = build_audit(run_dir, evaluation)
    (run_dir / AUDIT_JSON).write_text(json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (run_dir / AUDIT_MD).write_text(render_markdown(audit), encoding="utf-8")
    return run_dir / AUDIT_MD


# --- data ---


def build_audit(run_dir: Path, evaluation: dict) -> dict:
    run = read_json(run_dir / "run.json", {})
    settings = evaluation.get("settings") or {}
    reports = {area: read_json(run_dir / STEP_DIR / area / "report.json", None) for area in AREAS}
    status = {area["area"]: area for area in evaluation.get("areas", [])}
    steps = []
    for number, folder in STEPS:
        summary = evaluation if number == 6 and evaluation else read_json(run_dir / folder / "summary.json", None)
        state = "not_run" if summary is None else ("complete" if summary.get("complete") else "incomplete")
        steps.append({"step": number, "status": state, "errors": (summary or {}).get("errors", [])})
    return {
        "project": run.get("project") or evaluation.get("project"),
        "commit": run.get("commit"),
        "started": run.get("started"),
        "languages": (run.get("languages") or {}).get("shares", {}),
        "language": language_of(settings.get("report_language")),
        "steps": steps,
        "models": evaluation.get("models", {}),
        "key_figures": key_figures(reports),
        "areas": {area: {"status": (status.get(area) or {}).get("status", "skipped"),
                         "notes": (status.get(area) or {}).get("notes", []),
                         "report": reports[area]} for area in AREAS},
        "review": review_list(reports),
        "method": {
            "settings": {key: settings.get(key) for key in ("report_language", "areas")} if settings else {},
            "decision": settings.get("decision"),
            "report": settings.get("report"),
            "errors": evaluation.get("errors", []),
        },
    }


def key_figures(reports: dict[str, dict | None]) -> dict:
    figures: dict[str, object] = {}
    security = reports.get("security")
    if security:
        stats = security["statistics"]
        labels = stats.get("by_label", {})
        figures["security"] = {
            "findings": stats.get("input_findings", 0),
            "real": labels.get("REAL", 0),
            "real_by_severity": stats.get("real_by_severity", {}),
            "filtered": labels.get("FALSE_POSITIVE", 0) + labels.get("NOISE", 0),
            "duplicates": labels.get("DUPLICATE", 0),
        }
    lint = reports.get("code_quality")
    if lint:
        stats = lint["statistics"]
        changes = lint.get("formatter_changes", {})
        figures["code_quality"] = {
            "findings": stats.get("input_findings", 0),
            "groups": stats.get("groups", 0),
            "real_groups": stats.get("groups_by_label", {}).get("REAL", 0),
            "real_findings": stats.get("findings_by_label", {}).get("REAL", 0),
            "files_changed": changes.get("files_changed", 0),
            "lines_added": changes.get("lines_added", 0),
            "lines_removed": changes.get("lines_removed", 0),
        }
    tests = reports.get("test_quality")
    if tests:
        stats = tests["statistics"]
        figures["test_quality"] = {
            "tests": stats.get("tests", {}),
            "coverage": {run["suite"]: run.get("line_rate") for run in stats.get("coverage", [])},
            "mutation_score": {run["suite"]: run.get("score") for run in stats.get("mutation", [])},
        }
    review = review_list(reports)
    figures["review"] = {"low_confidence": sum(1 for entry in review if entry["low_confidence"]),
                         "review_hints": sum(1 for entry in review if entry["review_hint"])}
    return figures


def entries_of(report: dict | None) -> list[dict]:
    if not report:
        return []
    return report.get("entries") if "entries" in report else report.get("groups", [])


def entry_id(entry: dict) -> str:
    return entry.get("entry_id") or entry.get("group_id") or "?"


def review_list(reports: dict[str, dict | None]) -> list[dict]:
    review = []
    for area in AREAS:
        for entry in entries_of(reports.get(area)):
            if entry.get("review") or entry.get("review_hint"):
                review.append({"area": area, "id": entry_id(entry), "label": entry.get("label"),
                               "confidence": entry.get("confidence"), "low_confidence": bool(entry.get("review")),
                               "title": entry.get("title"), "review_hint": entry.get("review_hint")})
    return review


# --- Markdown ---


def cell(value: object) -> str:
    if value is None or value == "":
        return "–"
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(cell(value) for value in row) + " |" for row in rows]
    return lines


def details(summary: str, lines: list[str]) -> list[str]:
    return ["<details>", f"<summary>{summary}</summary>", "", *lines, "", "</details>"]


def percent(value: float | None) -> str:
    return "–" if value is None else f"{value:.1%}"


def location(entry: dict) -> str:
    package = entry.get("package")
    if package:
        version = package.get("installed_version")
        return f"{package.get('name')}@{version}" if version else str(package.get("name"))
    locations = entry.get("locations") or ([{"file": entry.get("file")}] if entry.get("file") else [])
    if not locations:
        return "–"
    first = locations[0]
    return f"{first.get('file')}:{first['line']}" if first.get("line") else str(first.get("file"))


def render_markdown(audit: dict) -> str:
    t = TEXTS[audit["language"]]
    project = audit.get("project") or "?"
    lines = [f"# {t['title']}: {Path(project).name}", ""]
    lines += _overview(audit, t)
    lines += _key_figures(audit, t)
    lines += _security(audit["areas"]["security"], t)
    lines += _code_quality(audit["areas"]["code_quality"], t)
    lines += _test_quality(audit["areas"]["test_quality"], t)
    lines += _review(audit, t)
    lines += _method(audit, t)
    return "\n".join(lines).rstrip() + "\n"


def _overview(audit: dict, t: dict) -> list[str]:
    models = audit.get("models", {})
    model_text = ", ".join(f"{t[role + '_model']}: {models[role]['name']}" for role in ("decision", "report")
                           if role in models)
    languages = ", ".join(f"{language} {share:.0%}" for language, share in audit.get("languages", {}).items())
    lines = [f"## {t['overview']}", ""]
    lines += table(["", ""], [[t["project"], audit.get("project")], [t["commit"], (audit.get("commit") or "")[:12]],
                              [t["analysed"], audit.get("started")], [t["languages"], languages],
                              [t["models"], model_text]])
    lines.append("")
    lines += table([t["step"], t["status"]], [[t["steps"][step["step"]], t[step["status"]]]
                                              for step in audit["steps"]])
    return lines + [""]


def _key_figures(audit: dict, t: dict) -> list[str]:
    figures = audit["key_figures"]
    lines = [f"## {t['key_figures']}", ""]
    security = figures.get("security")
    if security:
        severity = ", ".join(f"{count} {name}" for name, count in security["real_by_severity"].items())
        lines.append("- " + t["security_line"].format(real=security["real"], total=security["findings"],
                                                      severity=f" ({severity})" if severity else "",
                                                      filtered=security["filtered"],
                                                      duplicates=security["duplicates"]))
    lint = figures.get("code_quality")
    if lint:
        lines.append("- " + t["lint_line"].format(real_groups=lint["real_groups"], real=lint["real_findings"],
                                                  total=lint["findings"], groups=lint["groups"],
                                                  files=lint["files_changed"], added=lint["lines_added"],
                                                  removed=lint["lines_removed"]))
    tests = figures.get("test_quality")
    if tests:
        counts = ", ".join(f"{count} {outcome}" for outcome, count in tests["tests"].items() if count)
        lines.append("- " + t["tests_line"].format(counts=counts or t["no_tests"]))
        if tests["coverage"]:
            values = ", ".join(f"{suite} {percent(rate)}" for suite, rate in tests["coverage"].items())
            lines.append("- " + t["coverage_line"].format(values=values))
        if tests["mutation_score"]:
            values = ", ".join(f"{suite} {percent(score)}" for suite, score in tests["mutation_score"].items())
            lines.append("- " + t["mutation_line"].format(values=values))
    review = figures["review"]
    lines.append("- " + t["review_line"].format(low=review["low_confidence"], hints=review["review_hints"]))
    return lines + [""]


def _area_intro(area: dict, t: dict, heading: str) -> tuple[list[str], dict | None]:
    lines = [f"## {heading}", ""]
    report = area.get("report")
    if not report:
        notes = " ".join(area.get("notes") or [])
        return lines + [f"{t['not_evaluated']} {notes}".strip(), ""], None
    texts = report.get("texts") or {}
    if texts.get("summary_text"):
        lines += [texts["summary_text"], ""]
    if texts.get("run_notes"):
        lines += [f"**{t['limits']}:** {texts['run_notes']}", ""]
    if report.get("texts_missing"):
        lines += [f"_{t['texts_missing'].format(count=report['texts_missing'])}_", ""]
    return lines, report


def _security(area: dict, t: dict) -> list[str]:
    lines, report = _area_intro(area, t, t["security"])
    if report is None:
        return lines
    entries = report.get("entries", [])
    real = [entry for entry in entries if entry["label"] == "REAL"]
    other = [entry for entry in entries if entry["label"] != "REAL"]
    lines += [f"### {t['real_findings']} ({len(real)})", ""]
    if real:
        lines += table([t["id"], t["severity"], t["title_col"], t["location"], t["recommendation"]],
                       [[entry["entry_id"], entry.get("severity"), entry.get("title"), location(entry),
                         entry.get("recommendation")] for entry in real])
        lines.append("")
    if other:
        lines += details(f"{t['filtered']} ({len(other)})",
                         table([t["id"], t["label"], t["title_col"], t["location"], t["reason"]],
                               [[entry["entry_id"], entry["label"], entry.get("title"), location(entry),
                                 entry.get("reason")] for entry in other]))
        lines.append("")
    return lines


def _code_quality(area: dict, t: dict) -> list[str]:
    lines, report = _area_intro(area, t, t["code_quality"])
    if report is None:
        return lines
    groups = report.get("groups", [])
    real = [group for group in groups if group["label"] == "REAL"]
    other = [group for group in groups if group["label"] != "REAL"]
    lines += [f"### {t['real_groups']} ({len(real)})", ""]
    if real:
        lines += table([t["id"], t["severity"], t["tool_rule"], t["scope"], t["findings"], t["title_col"],
                        t["recommendation"]],
                       [[group["group_id"], group.get("severity"), f"{group.get('tool')} / {group.get('rule_id')}",
                         group.get("scope"), group.get("count"), group.get("title"), group.get("recommendation")]
                        for group in real])
        lines.append("")
    if other:
        lines += details(f"{t['filtered']} ({len(other)})",
                         table([t["id"], t["label"], t["tool_rule"], t["scope"], t["findings"], t["reason"]],
                               [[group["group_id"], group["label"], f"{group.get('tool')} / {group.get('rule_id')}",
                                 group.get("scope"), group.get("count"), group.get("reason")] for group in other]))
        lines.append("")
    changes = report.get("formatter_changes") or {}
    texts = report.get("texts") or {}
    lines += [f"### {t['formatter_changes']}", ""]
    lines.append(f"- {t['tools']}: {', '.join(changes.get('tools') or []) or '–'}; "
                 f"{changes.get('files_changed', 0)} {t['files_changed']} "
                 f"(+{changes.get('lines_added', 0)} / −{changes.get('lines_removed', 0)})")
    if texts.get("change_types"):
        lines.append(f"- {t['kinds']}: {', '.join(texts['change_types'])}")
    example = changes.get("example")
    if example:
        lines += ["", f"{t['example']} (`{example['file']}`):", "", "```diff", example["diff"], "```"]
    lines.append("")
    excluded = report.get("excluded_files") or {}
    if excluded.get("total"):
        lines += [f"### {t['excluded']} ({excluded['total']})", ""]
        lines += table([t["reason"], t["count"], t["examples"]],
                       [[group["reason_or_pattern"], group["count"], ", ".join(group["examples"])]
                        for group in excluded.get("groups", [])])
        lines.append("")
        if texts.get("excluded_notes"):
            lines += [texts["excluded_notes"], ""]
    return lines


def _test_quality(area: dict, t: dict) -> list[str]:
    lines, report = _area_intro(area, t, t["test_quality"])
    if report is None:
        return lines
    texts = report.get("texts") or {}
    run = report.get("run", {})
    lines += [f"### {t['tests']}", ""]
    if texts.get("tests_assessment"):
        lines += [texts["tests_assessment"], ""]
    if run.get("runs"):
        lines += table([t["suite"], t["tool"], t["status"], "passed", "failed", "error", "skipped"],
                       [[entry.get("suite"), entry.get("tool"), entry.get("status"),
                         *((entry.get("counts") or {}).get(outcome, 0)
                           for outcome in ("passed", "failed", "error", "skipped"))] for entry in run["runs"]])
        lines.append("")
    if run.get("coverage"):
        lines += [f"### {t['coverage']}", ""]
        if texts.get("coverage_assessment"):
            lines += [texts["coverage_assessment"], ""]
        lines += table([t["suite"], t["tool"], t["status"], t["lines"], t["branches"]],
                       [[entry.get("suite"), entry.get("tool"), entry.get("status"), percent(entry.get("line_rate")),
                         percent(entry.get("branch_rate"))] for entry in run["coverage"]])
        lines.append("")
    mutation = run.get("mutation") or {}
    if mutation.get("runs"):
        lines += [f"### {t['mutation']}", ""]
        if texts.get("mutation_assessment"):
            lines += [texts["mutation_assessment"], ""]
        lines += table([t["suite"], t["tool"], t["status"], t["score"], t["files"]],
                       [[entry.get("suite"), entry.get("tool"), entry.get("status"), percent(entry.get("score")),
                         f"{entry.get('files', 0)} ({t['sample'] if entry.get('sampled') else t['all_files']})"]
                        for entry in mutation["runs"]])
        lines.append("")
    entries = report.get("entries", [])
    sections = (("project_failures", "failed_tests", "PROJECT"), ("critical_files", "weakly_tested_file", "CRITICAL"),
                ("test_gaps", "surviving_mutant", "TEST_GAP"))
    shown = set()
    for heading, kind, label in sections:
        chosen = [entry for entry in entries if entry["kind"] == kind and entry["label"] == label]
        if not chosen:
            continue
        shown |= {entry["entry_id"] for entry in chosen}
        lines += [f"### {t[heading]} ({len(chosen)})", ""]
        lines += table([t["id"], t["file"] if kind != "failed_tests" else t["suite"], t["title_col"],
                        t["recommendation"]],
                       [[entry["entry_id"], _test_location(entry), entry.get("title"), entry.get("recommendation")]
                        for entry in chosen])
        lines.append("")
    other = [entry for entry in entries if entry["entry_id"] not in shown]
    if other:
        lines += details(f"{t['other_entries']} ({len(other)})",
                         table([t["id"], t["label"], t["file"], t["title_col"], t["reason"]],
                               [[entry["entry_id"], entry["label"], _test_location(entry), entry.get("title"),
                                 entry.get("reason") or entry.get("recommendation")] for entry in other]))
        lines.append("")
    return lines


def _test_location(entry: dict) -> str:
    if entry.get("file"):
        line = (entry.get("details") or {}).get("line")
        return f"{entry['file']}:{line}" if line else entry["file"]
    return str(entry.get("suite") or "–")


def _review(audit: dict, t: dict) -> list[str]:
    lines = [f"## {t['review']}", ""]
    review = audit["review"]
    if not review:
        return lines + [t["no_review"], ""]
    lines += table([t["area"], t["id"], t["label"], t["confidence"], t["title_col"], t["hint"]],
                   [[TEXTS[audit["language"]][entry["area"]].split(". ", 1)[-1], entry["id"], entry["label"],
                     (f"{entry['confidence']:.2f}" if isinstance(entry["confidence"], (int, float)) else "–")
                     + (f" ({t['low_confidence']})" if entry["low_confidence"] else ""),
                     entry.get("title"), entry.get("review_hint")] for entry in review])
    return lines + [""]


def _method(audit: dict, t: dict) -> list[str]:
    lines = [f"## {t['method']}", ""]
    for area in AREAS:
        report = audit["areas"][area]["report"]
        name = t[area].split(". ", 1)[-1]
        if report is None:
            lines.append(f"- {name}: {t['skipped']}. {' '.join(audit['areas'][area]['notes'])}".rstrip())
            continue
        tools = [f"{tool['name']} {tool.get('version') or ''}".strip() + (f" ({tool['status']})"
                                                                          if tool.get("status") != "ok" else "")
                 for tool in report.get("run", {}).get("tools", [])]
        if area == "test_quality":
            runs = report.get("run", {})
            tools = sorted({f"{entry.get('tool')}" for key in ("runs", "coverage") for entry in runs.get(key) or []}
                           | {f"{entry.get('tool')}" for entry in (runs.get("mutation") or {}).get("runs", [])})
        lines.append(f"- {name}: {', '.join(dict.fromkeys(tools)) or '–'}")
    models = audit.get("models", {})
    method = audit.get("method", {})
    for role in ("decision", "report"):
        if role in models:
            settings = method.get(role) or {}
            options = ", ".join(f"{key} {value}" for key, value in settings.items() if key != "model")
            digest = (models[role].get("digest") or "").split(":")[-1][:12]
            lines.append(f"- {t['models']} ({t[role + '_model']}): {models[role]['name']}"
                         + (f" ({digest})" if digest else "") + (f"; {options}" if options else ""))
    lines.append(f"- {t['method_labels']}")
    for error in method.get("errors", []):
        lines.append(f"- {error}")
    return lines + [""]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m scheduler.evaluation.audit <run folder>")
        return 2
    run_dir = Path(args[0]).expanduser().resolve()
    if not (run_dir / STEP_DIR / "summary.json").is_file():
        print(f"{run_dir} has no {STEP_DIR}/summary.json: step 6 did not run there.")
        return 1
    print(write_audit(run_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())

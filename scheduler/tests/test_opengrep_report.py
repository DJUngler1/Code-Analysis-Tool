"""OpenGrep's JSON report -> AnalysisFinding (fixture: real output of OpenGrep 1.33.1).

The fixture is the report of a project with
- src/app.py: command injection, source and sink in different functions;
- native/copy.cpp: strcpy;
- native/broken.c: a syntax error.
"""

import json
from pathlib import Path

from conftest import opengrep_config

from scheduler.static_analysis.opengrep import parse_report
from scheduler.static_analysis.rulesets import load_config

FIXTURE = Path(__file__).parent / "fixtures" / "opengrep" / "opengrep.json"
PROJECT = Path("/projects/app")


def rulesets(tmp_path):
    return load_config(opengrep_config(tmp_path)).active  # the rule sets the fixture was made with


def report(tmp_path, excluded=None):
    data = json.loads(FIXTURE.read_text())
    return parse_report(data, project=PROJECT, rulesets=rulesets(tmp_path), excluded=excluded or {})


def test_findings(tmp_path):
    parsed = report(tmp_path)
    by_rule = {finding.rule: finding for finding in parsed.findings}
    injection = by_rule["python.flask.security.injection.subprocess-injection"]
    assert injection.ruleset == "opengrep-rules"
    assert (injection.file, injection.line, injection.language) == ("src/app.py", 8, "Python")
    assert (injection.severity, injection.tool_severity) == ("error", "ERROR")
    assert injection.category == "security" and injection.subcategory == ("vuln",)
    assert injection.cwe[0].startswith("CWE-78") and "A03:2021 - Injection" in injection.owasp
    assert (injection.confidence, injection.likelihood, injection.impact) == ("HIGH", "HIGH", "MEDIUM")
    assert injection.end_line == 8 and injection.fingerprint

    strcpy = by_rule["rules.c.raptor-insecure-api-strcpy-strcat"]
    assert strcpy.ruleset == "0xdea-semgrep-rules"
    assert (strcpy.file, strcpy.language) == ("native/copy.cpp", "C++")

    assert parsed.problems == ["native/broken.c (Syntax error)"]
    assert parsed.files_scanned == 5 and parsed.dropped == 0


def test_findings_and_errors_in_generated_files_are_left_out(tmp_path):
    parsed = report(tmp_path, {"src/app.py": "generated: @generated", "native/broken.c": "generated: @generated"})
    assert {finding.file for finding in parsed.findings} == {"native/copy.cpp"}
    assert parsed.dropped == 4
    assert parsed.problems == []


def test_rule_ids_of_unknown_rule_sets_stay_as_they_are(tmp_path):
    data = {"results": [{"check_id": "other.rule", "path": "a.py", "start": {"line": 1}, "end": {"line": 1},
                         "extra": {"severity": "INFO", "message": " m ", "metadata": {}}}],
            "errors": [{"type": ["PartialParsing", []], "message": "x"}, {"type": "Timeout", "path": "a.py"}]}
    parsed = parse_report(data, project=PROJECT, rulesets=rulesets(tmp_path), excluded={})
    finding = parsed.findings[0]
    assert (finding.ruleset, finding.rule, finding.severity, finding.message) == (None, "other.rule", "note", "m")
    assert finding.cwe == () and finding.category is None
    assert parsed.problems == ["PartialParsing: x", "a.py (Timeout)"]
    assert parsed.files_scanned is None

"""A small run folder (results of steps 2 to 5) and a fake Ollama for the tests of step 6."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scheduler.evaluation.ollama import OllamaClient, OllamaError

DECISION_MODEL = "pplx-decider-flash:latest"
REPORT_MODEL = "qwen3.8:latest"

PATCH = """diff --git a/src/util.js b/src/util.js
index 1111111..2222222 100644
--- a/src/util.js
+++ b/src/util.js
@@ -1,3 +1,3 @@
-const a = 'x' + b
+const a = `x${b}`;
 export default a
-
+// end
"""


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def make_project(root: Path) -> Path:
    files = {
        "src/app.py": "import os\n\ndef find(user):\n    return db.execute('SELECT * FROM t WHERE name=' + user)\n",
        "src/other.py": "import sys\n",
        "src/util.js": "const a = `x${b}`;\nexport default a\n// end\n",
        "tests/test_app.py": "API_KEY = 'changeme'\n\ndef test_find():\n    assert find('a')\n",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def make_run(tmp_path: Path, *, steps: tuple[int, ...] = (2, 3, 4, 5)) -> Path:
    """A run folder with the given steps; the project is tmp_path/project."""
    project = make_project(tmp_path / "project")
    run = tmp_path / "results" / "project" / "2026-01-31_120000Z"
    write_json(run / "run.json", {"project": str(project), "commit": "0123456789abcdef", "started":
                                  "2026-01-31T12:00:00+00:00",
                                  "languages": {"primary": "Python", "shares": {"Python": 0.8, "JavaScript": 0.2}}})
    if 2 in steps:
        lint = run / "step2-linting"
        write_json(lint / "summary.json", {
            "step": "2", "project": str(project), "complete": True, "errors": [],
            "changed_files": ["src/util.js"], "created_files": [],
            "tools": [{"language": "Python", "tool": "ruff check", "version": "0.14.0", "status": "ok",
                       "changed_files": []},
                      {"language": "JavaScript/TypeScript", "tool": "biome", "version": "2.6.6", "status": "ok",
                       "changed_files": ["src/util.js"]}]})
        unused = {"language": "Python", "tool": "ruff check", "rule": "F401", "severity": "error",
                  "message": "`os` imported but unused", "file": "src/app.py", "line": 1, "column": 8, "fixed": False}
        write_json(lint / "findings.json", [
            unused, dict(unused),  # an exact duplicate
            unused | {"file": "src/other.py", "message": "`sys` imported but unused"},
            {"language": "Python", "tool": "ruff check", "rule": "E501", "severity": "warning",
             "message": "Line too long", "file": "tests/test_app.py", "line": 1, "column": 1, "fixed": False},
            {"language": "JavaScript/TypeScript", "tool": "biome", "rule": "lint/style/useTemplate",
             "severity": "note", "message": "Template literals are preferred", "file": "src/util.js", "line": 1,
             "column": 11, "fixed": True},
        ])
        write_json(lint / "excluded-files.json", [{"file": "src/gen.py", "reason": "generated: header comment"}])
        (lint / "changes.patch").write_text(PATCH)
    if 3 in steps:
        static = run / "step3-static-analysis"
        write_json(static / "summary.json", {
            "step": "3", "project": str(project), "complete": True, "errors": [],
            "tools": [{"tool": "opengrep", "version": "1.33.1", "status": "ok",
                       "rulesets": [{"name": "opengrep-rules", "commit": "abcdef1234"}]},
                      {"tool": "sonarqube", "version": "26.9", "status": "ok", "rulesets": []}]})
        write_json(static / "findings.json", [
            {"tool": "opengrep", "ruleset": "opengrep-rules", "rule": "python.sql-injection", "severity": "error",
             "tool_severity": "ERROR", "message": "SQL built from user input", "file": "src/app.py", "line": 4,
             "end_line": 4, "category": "security", "cwe": ["CWE-89: SQL Injection"], "fingerprint": "f1"},
            {"tool": "sonarqube", "rule": "pythonsecurity:S3649", "severity": "error", "tool_severity": "HIGH",
             "message": "Change this code to not construct SQL queries from user data", "file": "src/app.py",
             "line": 4, "end_line": 4, "category": "security", "cwe": ["CWE-89"], "fingerprint": "f2"},
            {"tool": "opengrep", "ruleset": "opengrep-rules", "rule": "generic.secrets.api-key", "severity": "warning",
             "tool_severity": "WARNING", "message": "Hard-coded API key", "file": "tests/test_app.py", "line": 1,
             "end_line": 1, "category": "security", "cwe": ["CWE-798: Hard-coded Credentials"], "fingerprint": "f3"},
        ])
    if 4 in steps:
        dependencies = run / "step4-dependencies"
        write_json(dependencies / "summary.json", {
            "step": "4", "project": str(project), "complete": True, "errors": [],
            "coverage": [{"manager": "npm", "manifest": "package.json", "lockfile": "package-lock.json",
                          "lockfile_state": "committed", "osv": True, "audit": "npm audit"}],
            "tools": [{"tool": "osv-scanner", "version": "2.7.1", "status": "ok"},
                      {"tool": "npm audit", "manager": "npm", "manifest": "package.json", "version": "npm 10.9.9",
                       "status": "ok"}]})
        lodash = {"ecosystem": "npm", "package": "lodash", "version": "4.17.20", "severity": "high",
                  "summary": "Command injection in lodash", "fixed_versions": ["4.17.21"], "manifest": "package-lock.json"}
        write_json(dependencies / "findings.json", [
            {"tool": "osv-scanner", **lodash, "id": "GHSA-35jh-r3h4-6jhm", "aliases": ["CVE-2021-23337"],
             "cvss_score": 7.2, "url": "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm"},
            {"tool": "npm audit", **lodash, "id": "CVE-2021-23337", "aliases": []},
            {"tool": "osv-scanner", "ecosystem": "PyPI", "package": "requests", "version": "2.0.0", "id": "PYSEC-1",
             "aliases": [], "severity": "medium", "summary": "Leak of credentials", "fixed_versions": ["2.3.0"],
             "manifest": "requirements.txt"},
        ])
    if 5 in steps:
        tests = run / "step5-tests"
        suite = "Python (requirements.txt)"
        write_json(tests / "summary.json", {
            "step": "5", "project": str(project), "complete": True, "errors": [],
            "detection": {"languages_without_tests": [], "languages_not_checked": {},
                          "suites": [{"language": "Python", "root": ".", "manager": "pip",
                                      "manifest": "requirements.txt", "tests_found": True, "frameworks": ["pytest"],
                                      "test_files": ["tests/test_app.py"]}]},
            "tests": {"network": False, "time_limit_minutes": 60,
                      "runs": [{"suite": suite, "language": "Python", "tool": "pytest", "version": "8.4",
                                "status": "ok", "tests": 3, "counts": {"passed": 1, "failed": 2, "error": 0,
                                                                       "skipped": 0}, "notes": []}]},
            "coverage": [{"suite": suite, "tool": "coverage.py", "version": "7.17.3", "status": "ok",
                          "files": 3, "files_not_loaded": 1, "line_rate": 0.6, "branch_rate": None}],
            "mutation": {"mode": "sample", "sample_files": 20, "seed": 1, "time_limit_minutes": 60,
                         "runs": [{"suite": suite, "tool": "mutmut", "version": "3.8.1", "status": "ok",
                                   "sampled": False, "files": ["src/app.py"], "candidate_files": 1, "mutants": 2,
                                   "counts": {"Killed": 1, "Survived": 1}, "score": 0.5}]}})
        write_json(tests / "tests.json", [
            {"suite": suite, "id": "tests/test_app.py::test_find", "outcome": "failed",
             "message": "AssertionError: assert None\n where None = find('a')"},
            {"suite": suite, "id": "tests/test_app.py::test_find_b", "outcome": "failed",
             "message": "AssertionError: assert None\n where None = find('b')"},
            {"suite": suite, "id": "tests/test_app.py::test_ok", "outcome": "passed", "message": ""},
        ])
        write_json(tests / "coverage.json", [{"suite": suite, "not_loaded": ["src/other.py"], "files": [
            {"file": "src/app.py", "lines_total": 10, "lines_covered": 2, "branches_total": 0, "branches_covered": 0},
            {"file": "src/util.py", "lines_total": 10, "lines_covered": 10}]}])
        write_json(tests / "mutants.json", [
            {"suite": suite, "file": "src/app.py", "line": 4, "mutator": "string", "replacement": "'XXSELECTXX'",
             "status": "Survived", "original": "return db.execute('SELECT ...')"},
            {"suite": suite, "file": "src/app.py", "line": 4, "mutator": "return", "replacement": "return None",
             "status": "Killed"},
        ])
    return run


class FakeOllama:
    """Answers /v1/systemone and /api/chat like Ollama; records which models are loaded at the same time."""

    def __init__(self, installed: tuple[str, ...] = (DECISION_MODEL, REPORT_MODEL)):
        self.installed = list(installed)
        self.loaded: list[str] = []
        self.most_loaded = 0
        self.requests: list[tuple[str, dict]] = []
        self.fail: set[str] = set()  # endpoints that answer with HTTP 500
        self.low_confidence: tuple[str, ...] = ("tests/",)  # questions mentioning this get confidence 0.4
        self.noise: tuple[str, ...] = ("tests/",)  # questions mentioning this get the label NOISE if possible
        self.drop_ids: set[str] = set()  # entry ids left out of the answers
        self.empty_summary = False

    def _load(self, model: str) -> None:
        if model not in self.loaded:
            self.loaded.append(model)
        self.most_loaded = max(self.most_loaded, len(self.loaded))

    def post(self, endpoint: str, body: dict, timeout: float) -> dict:
        self.requests.append((endpoint, body))
        if endpoint in self.fail:
            raise OllamaError(f"{endpoint}: HTTP 500: llama runner process has terminated", 500)
        if endpoint == "/api/generate" and body.get("keep_alive") == 0:
            if body["model"] in self.loaded:
                self.loaded.remove(body["model"])
            return {"done": True}
        self._load(body["model"])
        if endpoint == "/v1/systemone":
            return self._decide(body)
        if endpoint == "/api/chat":
            return self._chat(body)
        raise OllamaError(f"{endpoint}: HTTP 404", 404)

    def _decide(self, body: dict) -> dict:
        answers = {}
        for question_id, question in body["questions"].items():
            labels = list(question["criteria"])
            text = question["instructions"]
            label = "NOISE" if "NOISE" in labels and any(mark in text for mark in self.noise) else labels[0]
            confidence = 0.4 if any(mark in text for mark in self.low_confidence) else 0.9
            answers[question_id] = {"choice": label, "confidence": confidence,
                                    "probabilities": {name: (confidence if name == label else
                                                             round((1 - confidence) / (len(labels) - 1), 4))
                                                      for name in labels}}
        return {"answers": answers, "usage": {"input_tokens": 100 * len(answers)}}

    def _chat(self, body: dict) -> dict:
        user = body["messages"][1]["content"]
        properties = body["format"]["properties"]
        if "entries" in properties:
            shown = re.findall(r'"id": "([A-Z]-\d+)",\s*"label": "([A-Z_]+)"', user)
            entries = []
            for entry_id, label in shown:
                if entry_id in self.drop_ids:
                    continue
                action = label in ("REAL", "PROJECT", "CRITICAL", "TEST_GAP")
                entries.append({"id": entry_id, "title": f"Title of {entry_id}",
                                "description": f"Description of {entry_id}." if action else "",
                                "recommendation": f"Fix {entry_id}." if action else "",
                                "reason": "" if action else f"Reason for {entry_id}.", "review_hint": ""})
            content = json.dumps({"entries": entries})
        else:
            answer = {name: (["indentation"] if spec.get("type") == "array" else f"{name} text.")
                      for name, spec in properties.items()}
            if self.empty_summary:
                answer["summary_text"] = ""
            content = json.dumps(answer)
        return {"message": {"role": "assistant", "content": content, "thinking": "..."},
                "prompt_eval_count": len(user) // 3, "eval_count": len(content) // 3, "done_reason": "stop"}


class FakeClient(OllamaClient):
    """OllamaClient with the fake behind it, also for GET /api/tags and /api/ps."""

    def __init__(self, fake: FakeOllama, *, retries: int = 1, reachable: bool = True, max_failures: int = 3):
        super().__init__("http://ollama.invalid", post=fake.post, pause=0.0, retries=retries,
                         max_failures=max_failures)
        self.fake = fake
        self.reachable = reachable

    def _get(self, path: str) -> dict:
        if not self.reachable:
            raise OllamaError(f"Ollama is not reachable at {self.base_url}: Connection refused")
        if path == "/api/tags":
            return {"models": [{"name": name, "digest": f"sha256:{index:064d}"}
                               for index, name in enumerate(self.fake.installed)]}
        if path == "/api/ps":
            return {"models": [{"name": name} for name in self.fake.loaded]}
        raise OllamaError(path)

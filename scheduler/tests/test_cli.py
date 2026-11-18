from pathlib import Path

import pytest
from conftest import commit_all, requires_git
from test_ollama import unused_port

from scheduler.cli import describe_ollama, main
from scheduler.threat_model import ThreatModel, ThreatModelError
from scheduler.linting import LintingResult, Status, ToolRun
from scheduler.dependencies import Coverage, DependencyFinding, DependencyResult, DependencyRun
from scheduler.evaluation import AreaRun, EvaluationResult
from scheduler.ollama import OllamaStatus
from scheduler.static_analysis import AnalysisResult, AnalysisRun
from scheduler.testing import Suite, TestingResult


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{unused_port()}")
    monkeypatch.delenv("HOST_HOME", raising=False)


class FakeAnalysis(list):
    """Stands in for step 3 (Docker): records the run folders it gets, returns `result`."""

    def __init__(self) -> None:
        super().__init__()
        self.result = AnalysisResult(None, [AnalysisRun("opengrep", Status.OK)])

    def __call__(self, acceptance, settings, run_dir):
        self.append(run_dir)
        return self.result


@pytest.fixture(autouse=True)
def analysis_calls(monkeypatch) -> FakeAnalysis:
    fake = FakeAnalysis()
    monkeypatch.setattr("scheduler.cli.run_static_analysis", fake)
    return fake


def fake_analysis(calls: FakeAnalysis, result: AnalysisResult) -> None:
    calls.result = result


class FakeDependencies(list):
    """Stands in for step 4 (Docker, network): records the run folders, returns `result`."""

    def __init__(self) -> None:
        super().__init__()
        self.result = DependencyResult(None, [DependencyRun("osv-scanner", Status.OK)])

    def __call__(self, acceptance, settings, run_dir):
        self.append(run_dir)
        return self.result


@pytest.fixture(autouse=True)
def dependency_calls(monkeypatch) -> FakeDependencies:
    fake = FakeDependencies()
    monkeypatch.setattr("scheduler.cli.run_dependencies", fake)
    return fake


class FakeTests(list):
    """Stands in for step 5: records the run folders, returns `result`."""

    def __init__(self) -> None:
        super().__init__()
        self.result = TestingResult(None)

    def __call__(self, acceptance, settings, run_dir):
        self.append(run_dir)
        return self.result


@pytest.fixture(autouse=True)
def step5_calls(monkeypatch) -> FakeTests:
    fake = FakeTests()
    monkeypatch.setattr("scheduler.cli.run_tests", fake)
    return fake


class FakeEvaluation(list):
    """Stands in for step 6 (Ollama): records the run folders, returns `result`."""

    def __init__(self) -> None:
        super().__init__()
        self.result = EvaluationResult(None)
        self.resumed: list[bool] = []

    def __call__(self, run_dir, settings, *, resume=False):
        self.append(run_dir)
        self.resumed.append(resume)
        return self.result


@pytest.fixture(autouse=True)
def step6_calls(monkeypatch) -> FakeEvaluation:
    fake = FakeEvaluation()
    monkeypatch.setattr("scheduler.cli.run_evaluation", fake)
    return fake


class FakeThreatModel:
    """Stands in for the threat model (questions, Ollama): records the calls and the copies into run folders."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.copies: list = []
        self.error: str | None = None

    def ensure(self, project, settings, **kwargs):
        self.calls.append({"project": project, **kwargs})
        if self.error:
            raise ThreatModelError(self.error)
        return ThreatModel("# Threat model", "results", Path("/results/threat-model.md"), "2026-11-18", "abc")

    def copy(self, run_dir, model):
        self.copies.append(run_dir)


@pytest.fixture(autouse=True)
def threat_model_calls(monkeypatch) -> FakeThreatModel:
    fake = FakeThreatModel()
    monkeypatch.setattr("scheduler.cli.ensure_threat_model", fake.ensure)
    monkeypatch.setattr("scheduler.cli.copy_to_run", fake.copy)
    return fake


def fake_linting(monkeypatch, result: LintingResult) -> list:
    """Replace step 2 (Docker) and record its calls."""
    calls = []

    def fake(acceptance, settings):
        calls.append(acceptance.project)
        return result

    monkeypatch.setattr("scheduler.cli.run_linting", fake)
    return calls


def feed_input(monkeypatch, *answers):
    remaining = list(answers)

    def fake_input(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


@requires_git
def test_accepted_project_via_prompt(monkeypatch, make_project, capsys, tmp_path):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    feed_input(monkeypatch, "/definitely/outside", str(root))
    run = ToolRun("Python", "ruff check", Status.OK, changed_files=["main.py"])
    calls = fake_linting(monkeypatch, LintingResult(tmp_path / "run", [run], changed_files=["main.py"]))

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Warning: Ollama is not reachable" in out
    assert "outside the projects folder" in out  # first answer was rejected, then asked again
    assert "Languages:        Python 100% (primary: Python)" in out
    assert "Package managers: Python: pip (requirements.txt)" in out
    assert "Project accepted." in out
    assert calls == [root]
    assert f"Results: {tmp_path / 'run'}" in out
    assert "git diff" in out
    assert "Step 2 complete." in out
    assert "Step 3 complete." in out


@requires_git
def test_check_only_skips_step_2(monkeypatch, make_project, capsys, analysis_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    calls = fake_linting(monkeypatch, LintingResult(None))
    assert main(["--path", str(root), "--check-only"]) == 0
    assert calls == [] and analysis_calls == []
    assert "Step 2" not in capsys.readouterr().out


@requires_git
def test_threat_model_before_step_2_and_copied_into_the_run(monkeypatch, make_project, capsys, tmp_path,
                                                          threat_model_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    assert main(["--path", str(root)]) == 0
    (call,) = threat_model_calls.calls
    assert call["project"] == root and call["folder"] == (tmp_path / "results").resolve() / root.name
    assert call["facts"]["languages"]["primary"] == "Python" and "top_level_folders" in call["facts"]
    assert call["interactive"] is False  # pytest has no terminal on stdin
    assert threat_model_calls.copies == [tmp_path / "run"]
    assert "Threat model – what an attacker controls and what is shipped" in capsys.readouterr().out


@requires_git
def test_without_threat_model_the_run_stops_before_step_2(monkeypatch, make_project, capsys, threat_model_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    calls = fake_linting(monkeypatch, LintingResult(None))
    threat_model_calls.error = "no confirmed threat model: nobody can answer the questions."
    assert main(["--path", str(root)]) == 10
    assert calls == []
    assert "Error: no confirmed threat model" in capsys.readouterr().out


@requires_git
def test_check_only_needs_no_threat_model(make_project, threat_model_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    assert main(["--path", str(root), "--check-only"]) == 0
    assert threat_model_calls.calls == []


@requires_git
def test_real_threat_model_check_without_terminal(monkeypatch, make_project, capsys, tmp_path):
    from scheduler import threat_model  # the real lookup: nothing confirmed, and no terminal for the questions

    monkeypatch.setattr("scheduler.cli.ensure_threat_model", threat_model.ensure_threat_model)
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    calls = fake_linting(monkeypatch, LintingResult(None))
    assert main(["--path", str(root)]) == 10
    out = capsys.readouterr().out
    assert calls == [] and "without a terminal nobody can answer the questions" in out
    assert ".code-analysis/threat-model.md" in out


@requires_git
def test_incomplete_step_2_exits_with_5(monkeypatch, make_project, capsys, tmp_path, analysis_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    failed = ToolRun("Python", "ruff check", Status.FAILED, notes=["ruff check failed"])
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [failed]))
    fake_analysis(analysis_calls, AnalysisResult(tmp_path / "run", errors=["image missing"]))
    assert main(["--path", str(root)]) == 5  # step 2 comes first
    out = capsys.readouterr().out
    assert "Failed: Python ruff check" in out
    assert "Step 2 incomplete." in out
    assert analysis_calls == [tmp_path / "run"]  # step 3 still ran
    assert "Step 3 incomplete." in out


@requires_git
def test_incomplete_step_3_exits_with_6(monkeypatch, make_project, capsys, tmp_path, analysis_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    failed = AnalysisRun("opengrep", Status.FAILED, notes=["opengrep failed"],
                         rulesets=[{"name": "opengrep-rules", "commit": "f1d2b562b414"}])
    fake_analysis(analysis_calls, AnalysisResult(tmp_path / "run", [failed]))
    assert main(["--path", str(root)]) == 6
    out = capsys.readouterr().out
    assert "Rule sets of opengrep: opengrep-rules (f1d2b56)" in out
    assert "Failed: opengrep" in out
    assert f"Results: {tmp_path / 'run' / 'step3-static-analysis'}" in out
    assert "Step 3 incomplete." in out


@requires_git
def test_step_3_needs_the_results_folder_of_step_2(monkeypatch, make_project, capsys, analysis_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(None, errors=["OUTPUT_DIR is not set"]))
    assert main(["--path", str(root)]) == 5
    assert analysis_calls == []
    assert "Step 3 skipped" in capsys.readouterr().out


def test_rejected_project_does_not_run_step_2(monkeypatch, make_project, capsys):
    root = make_project({"main.go": "package main\n"})
    calls = fake_linting(monkeypatch, LintingResult(None))
    assert main(["--path", str(root)]) == 2
    assert calls == []


def test_rejected_project_via_path_argument(make_project, capsys):
    root = make_project({"main.go": "package main\n"})
    assert main(["--path", str(root)]) == 2
    assert "Abort – language not supported: Go" in capsys.readouterr().out


def test_invalid_path_argument(capsys):
    assert main(["--path", "relative"]) == 1
    assert "not an absolute path" in capsys.readouterr().out


def test_empty_input_exits(monkeypatch, capsys):
    feed_input(monkeypatch, "")
    assert main([]) == 1
    assert "No project selected" in capsys.readouterr().out


def test_percent():
    from scheduler.cli import percent

    assert percent(0.6515) == "65%"
    assert percent(0.0013) == "<1%"
    assert percent(0.0) == "0%"


def test_files_not_counted_are_listed(make_project, capsys):
    project = make_project({
        "requirements.txt": "",
        "app.py": "x = 1\n",
        "api/message_pb2.py": "# Generated by the protocol buffer compiler.  DO NOT EDIT!\nimport x\n",
    })
    assert main(["--check-only", "--path", str(project)]) != 1
    assert "Not counted:      1 files (1 generated), not linted either" in capsys.readouterr().out


def test_format_analysis_names_rule_sets_and_the_web_interface(tmp_path):
    from scheduler.cli import format_analysis

    opengrep = AnalysisRun("opengrep", Status.OK, rulesets=[{"name": "opengrep-rules", "commit": "f1d2b562b414"}])
    sonar = AnalysisRun("sonarqube", Status.PARTIAL,
                        rulesets=[{"name": "Sonar way", "language": "py"}, {"name": "Sonar way", "language": "java"}],
                        details={"web_interface": "http://localhost:9000/dashboard?id=code-analysis_x"})
    text = format_analysis(AnalysisResult(tmp_path, [opengrep, sonar]))
    assert "Rule sets of opengrep: opengrep-rules (f1d2b56)" in text
    assert "Rule sets of sonarqube: Sonar way (py), Sonar way (java)" in text
    assert "SonarQube web interface: http://localhost:9000/dashboard?id=code-analysis_x" in text
    assert "Step 3 complete." in text  # partial is not failed


@requires_git
def test_incomplete_step_4_exits_with_7(monkeypatch, make_project, capsys, tmp_path, dependency_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    finding = DependencyFinding("osv-scanner", "PyPI", "django", "2.0", "PYSEC-2018-2", severity="medium")
    dependency_calls.result = DependencyResult(
        tmp_path / "run",
        [DependencyRun("osv-scanner", Status.OK, findings=[finding]),
         DependencyRun("pip-audit", Status.FAILED, manifest="requirements.txt")],
        coverage=[Coverage("Cargo", "Cargo.toml", "Cargo.lock", "generated", True, "cargo audit"),
                  Coverage("vcpkg", "vcpkg.json", None, "not supported", False, None)],
    )
    assert main(["--path", str(root)]) == 7
    out = capsys.readouterr().out
    assert dependency_calls == [tmp_path / "run"]
    assert "Lock files generated for: Cargo.toml" in out
    assert "Not checked by any tool: vcpkg (vcpkg.json)" in out
    assert "1 findings (1 medium), 1 distinct package/advisory pairs." in out
    assert "Failed: pip-audit (requirements.txt)" in out
    assert "Step 4 incomplete." in out


@requires_git
def test_step_3_failure_takes_precedence_over_step_4(monkeypatch, make_project, tmp_path, analysis_calls,
                                                    dependency_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    analysis_calls.result = AnalysisResult(tmp_path / "run", errors=["image missing"])
    dependency_calls.result = DependencyResult(tmp_path / "run", errors=["Docker is not reachable"])
    assert main(["--path", str(root)]) == 6


@requires_git
def test_incomplete_step_5_exits_with_8(monkeypatch, make_project, capsys, tmp_path, step5_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    step5_calls.result = TestingResult(
        tmp_path / "run",
        [Suite("Python", ".", "pip", "requirements.txt", tests_found=True, frameworks=["pytest"]),
         Suite("Rust", ".", "Cargo", "Cargo.toml")],
        not_checked={"C/C++": "no CMakeLists.txt in the project folder"},
        errors=["test detection failed: boom"],
    )
    assert main(["--path", str(root)]) == 8
    out = capsys.readouterr().out
    assert step5_calls == [tmp_path / "run"]
    assert "Tests found in 1 of 2 checked build units." in out
    assert "No tests found for Rust – noted in the report." in out
    assert "Not checked: C/C++ (no CMakeLists.txt in the project folder)." in out
    assert f"Results: {tmp_path / 'run' / 'step5-tests'}" in out
    assert "Step 5 incomplete." in out


@requires_git
def test_step_4_failure_takes_precedence_over_step_5(monkeypatch, make_project, tmp_path, dependency_calls,
                                                    step5_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    dependency_calls.result = DependencyResult(tmp_path / "run", errors=["Docker is not reachable"])
    step5_calls.result = TestingResult(tmp_path / "run", errors=["boom"])
    assert main(["--path", str(root)]) == 7


@requires_git
def test_incomplete_step_6_exits_with_9(monkeypatch, make_project, capsys, tmp_path, step6_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    security = AreaRun("security", Status.PARTIAL, items=4, notes=["texts missing"],
                       statistics={"input_findings": 6, "by_label": {"REAL": 3, "NOISE": 1},
                                   "real_by_severity": {"high": 2, "medium": 1}, "low_confidence": 1})
    step6_calls.result = EvaluationResult(
        tmp_path / "run",
        [security, AreaRun("test_quality", notes=["Step 5 did not run: no results to evaluate."])],
        audit=tmp_path / "run" / "audit.md",
        errors=["Ollama stopped answering"],
    )
    assert main(["--path", str(root)]) == 9
    out = capsys.readouterr().out
    assert step6_calls == [tmp_path / "run"]
    assert "Step 6 – LLM evaluation and audit" in out
    assert ("security: 3 REAL of 6 findings (2 high, 1 medium); 1 decision with low confidence "
            "(see the review list) – partial (texts missing)") in out
    assert "test quality: skipped (Step 5 did not run: no results to evaluate.)" in out
    assert "Error: Ollama stopped answering" in out
    assert f"Audit: {tmp_path / 'run' / 'audit.md'}" in out
    assert "Step 6 incomplete." in out


@requires_git
def test_step_5_failure_takes_precedence_over_step_6(monkeypatch, make_project, tmp_path, step5_calls,
                                                    step6_calls):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    fake_linting(monkeypatch, LintingResult(tmp_path / "run", [ToolRun("Python", "ruff check", Status.OK)]))
    step5_calls.result = TestingResult(tmp_path / "run", errors=["boom"])
    step6_calls.result = EvaluationResult(tmp_path / "run", errors=["Ollama is not reachable"])
    assert main(["--path", str(root)]) == 8


def make_run_folder(tmp_path):
    run = tmp_path / "results" / "app" / "2026-01-31_120000Z"
    run.mkdir(parents=True)
    (run / "run.json").write_text("{}")
    return run


def test_evaluate_runs_only_step_6(monkeypatch, tmp_path, capsys, step6_calls, analysis_calls, step5_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    run = make_run_folder(tmp_path)
    calls = fake_linting(monkeypatch, LintingResult(None))
    step6_calls.result = EvaluationResult(run)
    assert main(["--evaluate", str(run)]) == 0
    out = capsys.readouterr().out
    assert step6_calls == [run.resolve()]
    assert calls == [] and analysis_calls == [] and step5_calls == []
    assert "Step 6 – LLM evaluation and audit of" in out and "Step 6 complete." in out


def test_evaluate_uses_and_copies_the_threat_model(monkeypatch, tmp_path, step6_calls, threat_model_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    run = make_run_folder(tmp_path)
    step6_calls.result = EvaluationResult(run)
    assert main(["--evaluate", str(run)]) == 0
    (call,) = threat_model_calls.calls
    assert call["folder"] == run.resolve().parent and call["project"] is None  # run.json names no project
    assert threat_model_calls.copies == [run.resolve()]


def test_evaluate_without_threat_model_exits_with_10(monkeypatch, tmp_path, step6_calls, threat_model_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    run = make_run_folder(tmp_path)
    threat_model_calls.error = "aborted"
    assert main(["--evaluate", str(run)]) == 10
    assert step6_calls == []


def test_evaluate_exits_with_9_when_incomplete(monkeypatch, tmp_path, step6_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    run = make_run_folder(tmp_path)
    step6_calls.result = EvaluationResult(run, errors=["Ollama is not reachable"])
    assert main(["--evaluate", str(run)]) == 9


@pytest.mark.parametrize("folder, message", [
    ("results/app", "is not the folder of a run"),
    ("elsewhere", "is outside the results folder"),
    ("results/missing", "does not exist"),
])
def test_evaluate_rejects_other_folders(monkeypatch, tmp_path, capsys, step6_calls, folder, message):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    make_run_folder(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    assert main(["--evaluate", str(tmp_path / folder)]) == 1
    assert message in capsys.readouterr().out
    assert step6_calls == []


def test_evaluate_needs_an_absolute_path(capsys, step6_calls):
    assert main(["--evaluate", "results/app"]) == 1
    assert "is not an absolute path" in capsys.readouterr().out


def test_evaluate_with_resume(monkeypatch, tmp_path, capsys, step6_calls):
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "results"))
    run = make_run_folder(tmp_path)
    step6_calls.result = EvaluationResult(run)
    assert main(["--evaluate", str(run), "--resume"]) == 0
    assert step6_calls == [run.resolve()] and step6_calls.resumed == [True]
    assert "reusing its decisions where possible (--resume)" in capsys.readouterr().out


def test_resume_needs_evaluate(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--resume"])
    assert exit_info.value.code == 2
    assert "--resume needs --evaluate" in capsys.readouterr().err


def test_evaluate_cannot_be_combined_with_path(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--evaluate", "/tmp/run", "--path", "/tmp/project"])
    assert exit_info.value.code == 2
    assert "--evaluate cannot be combined" in capsys.readouterr().err


def test_ollama_line_names_missing_models():
    status = OllamaStatus("http://ollama", True, ("qwen3.8:latest", "llama3.2:3b"))
    text = describe_ollama(status, ("pplx-decider-flash:latest", "qwen3.8"))
    assert text.startswith("Ollama: reachable at http://ollama (2 models: qwen3.8:latest, llama3.2:3b)")
    warning = text.split("\n", 1)[1]
    assert warning == ("Warning: step 6 needs pplx-decider-flash:latest, not installed in Ollama.\n"
                       "         Install on the host with: ollama pull pplx-decider-flash:latest")
    assert "Warning" not in describe_ollama(status, ("qwen3.8",))

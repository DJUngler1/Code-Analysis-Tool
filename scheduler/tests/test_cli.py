import pytest
from conftest import commit_all, requires_git
from test_ollama import unused_port

from scheduler.cli import main
from scheduler.linting import LintingResult, Status, ToolRun
from scheduler.dependencies import Coverage, DependencyFinding, DependencyResult, DependencyRun
from scheduler.static_analysis import AnalysisResult, AnalysisRun


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

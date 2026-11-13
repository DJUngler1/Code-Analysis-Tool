"""Step 5.2 without Docker: report parsers (real reports in tests/fixtures/testing/), configuration,
the HEAD workspace, the comparison and the orchestration with a fake Docker runner."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from conftest import commit_all, git, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.cli import format_tests
from scheduler.config import Settings
from scheduler.linting.docker import DockerError
from scheduler.linting.model import CommandResult, Status
from scheduler.testing import run_tests
from scheduler.testing.config import ConfigError, TestsConfig, load_config
from scheduler.testing.execute import compare_runs
from scheduler.testing.languages import javascript, python
from scheduler.testing.model import SuiteRun, TestCase, TestingResult
from scheduler.testing.reports import parse_jest, parse_junit, parse_libtest, parse_trx
from scheduler.workspace import prepare_head_workspace

FIXTURES = Path(__file__).parent / "fixtures" / "testing"
NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


def outcomes(tests):
    return {test.id: test.outcome for test in tests}


# --- report parsers ---


def test_pytest_junit():
    tests = parse_junit(FIXTURES / "pytest-junit.xml")
    assert outcomes(tests) == {
        "tests.test_calc.test_add": "passed",
        "tests.test_calc.test_broken": "failed",
        "tests.test_calc.test_slow": "skipped",
        "tests.test_legacy.LegacyTests.test_add": "passed",
        "tests.test_network.test_network": "failed",
    }
    broken = tests[1]
    assert broken.message.startswith("assert 2 == 3") and "tests/test_calc.py:11: AssertionError" in broken.message
    assert tests[2].message == "slow"


def test_surefire_and_gradle_junit():
    assert outcomes(parse_junit(FIXTURES / "surefire.xml")) == {
        "calc.CalcTest.broken": "failed", "calc.CalcTest.adds": "passed", "calc.CalcTest.slow": "skipped"}
    gradle = parse_junit(FIXTURES / "gradle.xml")
    assert outcomes(gradle) == {
        "calc.CalcTest.broken()": "failed", "calc.CalcTest.adds()": "passed", "calc.CalcTest.slow()": "skipped"}
    assert "expected: <3> but was: <2>" in gradle[0].message


def test_ctest_junit_uses_the_output_of_a_failed_test():
    tests = parse_junit(FIXTURES / "ctest-junit.xml")
    assert outcomes(tests) == {"passes": "passed", "fails": "failed"}
    assert tests[1].message.startswith("Subprocess aborted\n") and "Assertion `1 + 1 == 3' failed" in tests[1].message


def test_javascript_junit_reports():
    assert list(outcomes(parse_junit(FIXTURES / "vitest-junit.xml")).items()) == [
        ("test/calc.test.js > calc > adds", "passed"),
        ("test/calc.test.js > calc > is broken", "failed"),
        ("test/calc.test.js > calc > is slow", "skipped"),
    ]
    # node:test names every class "test": the describe blocks make the id.
    assert outcomes(parse_junit(FIXTURES / "node-test-junit.xml")) == {
        "calc > adds": "passed", "calc > is broken": "failed", "calc > is slow": "skipped"}


def test_junit_duplicate_ids_get_their_suite_names(tmp_path):
    report = tmp_path / "junit.xml"
    report.write_text('<testsuites><testsuite name="a"><testcase classname="test" name="x"/></testsuite>'
                      '<testsuite name="b"><testcase classname="C" name="y"/></testsuite>'
                      '<testsuite name="c"><testcase classname="C" name="y"><error message="boom"/></testcase>'
                      "</testsuite></testsuites>")
    assert outcomes(parse_junit(report)) == {"a > x": "passed", "b > C.y": "passed", "c > C.y": "error"}


def test_trx():
    tests = parse_trx([FIXTURES / "dotnet.trx"])
    assert outcomes(tests) == {
        "Calc.Tests.CalculatorTests.AddsMany(a: 1, b: 1, sum: 2)": "passed",
        "Calc.Tests.CalculatorTests.Broken": "failed",
        "Calc.Tests.CalculatorTests.Slow": "skipped",
        "Calc.Tests.CalculatorTests.AddsMany(a: 2, b: 2, sum: 4)": "passed",
        "Calc.Tests.CalculatorTests.Adds": "passed",
    }
    assert "Assert.Equal() Failure" in tests[1].message


def test_trx_of_several_target_frameworks(tmp_path):
    for tfm in ("net8.0", "net9.0"):
        (tmp_path / f"results_{tfm}_20261113100000.trx").write_bytes((FIXTURES / "dotnet.trx").read_bytes())
    ids = [test.id for test in parse_trx(sorted(tmp_path.glob("*.trx")))]
    assert "Calc.Tests.CalculatorTests.Adds [net8.0]" in ids and "Calc.Tests.CalculatorTests.Adds [net9.0]" in ids


def test_jest_json():
    tests = parse_jest(FIXTURES / "jest.json", Path("/work/itest/run/step5-tests/workspace"))
    assert outcomes(tests) == {"__tests__/calc.test.js > calc adds": "passed",
                               "__tests__/calc.test.js > calc is broken": "failed",
                               "__tests__/calc.test.js > calc is slow": "skipped"}
    assert "Expected: 3" in tests[1].message


def test_libtest_output():
    tests = parse_libtest((FIXTURES / "cargo-test.txt").read_text())
    assert outcomes(tests) == {
        "calc src/lib.rs::tests::slow": "skipped",
        "calc src/lib.rs::tests::adds": "passed",
        "calc src/lib.rs::tests::broken": "failed",
        "integration tests/integration.rs::integration": "passed",
        "calc doctests::src/lib.rs - add": "passed",  # without "(line 3)": formatting moves lines
    }
    assert "assertion `left == right` failed" in tests[2].message


# --- configuration ---


def test_config_defaults_and_values(tmp_path):
    assert load_config(None) == TestsConfig(network=False, time_limit_minutes=60)
    repository = Path(__file__).resolve().parents[2] / "config"
    assert load_config(repository) == TestsConfig(network=False, time_limit_minutes=60)
    (tmp_path / "tests.toml").write_text("[tests]\nnetwork = true\ntime_limit_minutes = 0\n")
    config = load_config(tmp_path)
    assert config.network and config.time_limit_seconds is None


@pytest.mark.parametrize("text", ["[tests]\nnetwork = 'yes'\n", "[tests]\ntime_limit_minutes = -1\n",
                                  "[tests]\ntime_limit_minutes = 1.5\n", "[tests\n"])
def test_config_errors(tmp_path, text):
    (tmp_path / "tests.toml").write_text(text)
    with pytest.raises(ConfigError):
        load_config(tmp_path)


# --- HEAD workspace and comparison ---


@requires_git
def test_head_workspace_has_the_committed_state(make_project, tmp_path):
    root = make_project({"app/a.py": "x = 1\n", "app/b.py": "y = 1\n", "other.txt": "o\n",
                         ".gitattributes": "app/b.py export-ignore\n"})
    commit_all(root)
    (root / "app/a.py").write_text("x = 2\n")  # changed by step 2
    (root / "app/new.py").write_text("z = 1\n")
    assert prepare_head_workspace(root / "app", tmp_path / "head") == 2  # a subfolder of the repository
    assert (tmp_path / "head" / "a.py").read_text() == "x = 1\n"
    assert (tmp_path / "head" / "b.py").is_file()  # export-ignore does not apply
    assert not (tmp_path / "head" / "new.py").exists()
    git(root, "diff", "--quiet", "HEAD", "--", "other.txt")  # the project's repository is unchanged
    assert not (tmp_path / "head.index").exists() and not (tmp_path / "head.checkout").exists()


def test_compare_runs():
    after = SuiteRun("Python (x)", "Python", "after step 2", "pytest", Status.OK,
                     tests=[TestCase("a", "passed"), TestCase("b", "failed"), TestCase("c", "passed")])
    head = SuiteRun("Python (x)", "Python", "HEAD", "pytest", Status.OK,
                    tests=[TestCase("a", "passed"), TestCase("b", "passed"), TestCase("d", "skipped")])
    comparison = compare_runs(after, head)
    assert comparison.differs
    assert [(t.id, t.after_step_2, t.head) for t in comparison.tests] == [
        ("b", "failed", "passed"), ("c", "passed", "missing"), ("d", "missing", "skipped")]
    assert not compare_runs(after, after).differs
    nothing = SuiteRun("Python (x)", "Python", "after step 2", "pytest", Status.FAILED)
    comparison = compare_runs(nothing, head)
    assert comparison.differs and comparison.tests == []  # no test ran after step 2: only the counts
    assert comparison.counts_head == {"passed": 2, "failed": 0, "error": 0, "skipped": 1}
    text = format_tests(TestingResult(None, runs=[nothing], differences=[comparison]))
    assert "  after step 2: failed, no test ran; at HEAD: ok, 3 tests (2 passed, 1 skipped)" in text


# --- commands ---


def test_python_install_script_per_manager(tmp_path):
    from scheduler.testing.context import Job
    from scheduler.testing.model import Suite

    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\ntest = ["pytest"]\ndocs = ["sphinx"]\n'
        '[dependency-groups]\ndev = ["ruff"]\nlint = ["mypy"]\n')
    for name in ("requirements.txt", "requirements-dev.txt", "requirements-docs.txt"):
        (tmp_path / name).write_text("")
    job = Job(Suite("Python", ".", "pip", "pyproject.toml"), tmp_path, tmp_path / "out", "after step 2")
    script = python.install_script(job)
    assert ("uv pip install --python .venv/bin/python -e '.[test]' --group dev -r 'requirements-dev.txt' "
            "-r 'requirements.txt'") in script
    assert "pytest==9.2.0" in script
    job.suite.manager = "Poetry"
    assert "poetry install --all-extras --all-groups" in python.install_script(job)


def test_javascript_test_commands(tmp_path):
    report = tmp_path / "r"
    assert javascript.test_command("Vitest", "vitest", report) == [
        "vitest", "run", "--reporter=default", "--reporter=junit", f"--outputFile.junit={report}"]
    assert javascript.test_command("Vitest", "vitest run --dir src", report)[:4] == ["vitest", "run", "--dir", "src"]
    assert javascript.test_command("Jest", "npm run lint && jest", report)[:2] == ["jest", "--ci"]  # not a single call
    assert javascript.test_command("node:test", "node --test test/", report)[-1] == "test/"
    assert javascript.install_command("npm", tmp_path) == "npm install --ignore-scripts --no-audit --no-fund"
    (tmp_path / "package-lock.json").write_text("{}")
    assert javascript.install_command("npm", tmp_path).startswith("npm ci --ignore-scripts")


# --- orchestration with a fake Docker ---


class FakeRunner:
    """Docker stand-in: records calls; writes the reports the real tools would write."""

    def __init__(self, *, junit: str | None = None, fail: tuple[str, ...] = ()) -> None:
        self.calls: list[dict] = []
        self.junit = junit or (FIXTURES / "pytest-junit.xml").read_text()
        self.fail = fail

    def check_available(self) -> None:
        pass

    def ensure_image(self, image: str) -> None:
        pass

    def image_versions(self, image: str) -> dict[str, str]:
        return {"cargo": "cargo 1.99.1"}

    def run(self, image, command, *, workdir, mounts, env=None, timeout, read_only=(), network=None):
        self.calls.append({"image": image, "command": list(command), "workdir": workdir, "mounts": list(mounts),
                           "network": network, "timeout": timeout, "env": dict(env or {})})
        text = " ".join(command)
        if any(key in text for key in self.fail):
            return CommandResult(tuple(command), 2, "", "broke", 0.1)
        for word in command:
            if word.startswith("--junitxml="):
                Path(word.split("=", 1)[1]).write_text(self.junit)
        stdout = "platform linux -- Python 3.13.18, pytest-9.2.0, pluggy-1.6.0\n" if "pytest" in text else ""
        return CommandResult(tuple(command), 1 if "pytest" in text else 0, stdout, "", 0.1)


def python_project(make_project):
    project = make_project({"requirements.txt": "", "app.py": "x = 1\n" * 50,
                            "test_app.py": "def test_x():\n    assert True\n"})
    commit_all(project)
    return project


def settings(config_dir=None):
    return Settings(None, None, "x", tools_network="code-analysis-net", config_dir=config_dir)


@requires_git
def test_run_tests_online_download_then_offline_tests(make_project, tmp_path):
    project = python_project(make_project)
    runner = FakeRunner()
    lines: list[str] = []
    result = run_tests(run_acceptance(project), settings(), tmp_path / "run", runner=runner, now=lambda: NOW,
                       out=lines.append)
    assert result.complete and result.network is False
    install, pytest_call = runner.calls
    assert install["network"] == "code-analysis-net" and install["timeout"] == 3600
    assert pytest_call["network"] == "none" and pytest_call["timeout"] == 3600
    assert pytest_call["command"][:3] == [".venv/bin/python", "-m", "pytest"]
    step = tmp_path / "run" / "step5-tests"
    assert all(call["mounts"] == [step] for call in runner.calls)  # only the step folder, never the project
    assert all(str(call["workdir"]).startswith(str(step / "workspace")) for call in runner.calls)
    assert not (step / "workspace").exists() and not (step / "cache").exists()
    (run,) = result.runs
    assert run.status is Status.OK and run.version == "pytest 9.2.0, Python 3.13.18"
    assert run.counts == {"passed": 2, "failed": 2, "error": 0, "skipped": 1}
    assert result.head_runs == []  # nothing changed since the commit
    summary = json.loads((step / "summary.json").read_text())
    assert summary["tests"]["network"] is False and summary["tests"]["time_limit_minutes"] == 60
    assert summary["tests"]["runs"][0]["failed_tests"] == ["tests.test_calc.test_broken",
                                                            "tests.test_network.test_network"]
    assert "differences_to_head" not in summary["tests"]
    tests = json.loads((step / "tests.json").read_text())
    assert tests[0] == {"suite": "Python (requirements.txt)", "id": "tests.test_calc.test_add", "outcome": "passed",
                        "duration_seconds": 0.0, "message": ""}
    assert any("Running the tests without network" in line for line in lines)
    text = format_tests(result)
    assert "Python (requirements.txt): ok, 2 passed, 2 failed, 1 skipped" in text
    assert "2 tests failed. The tests ran without network (config/tests.toml)" in text
    assert "HEAD" not in text


@requires_git
def test_run_tests_compares_with_head_after_changes(make_project, tmp_path):
    project = python_project(make_project)
    acceptance = run_acceptance(project)
    (project / "app.py").write_text("x = 2\n")  # what step 2 changed
    runner = FakeRunner()
    result = run_tests(acceptance, settings(), tmp_path / "run", runner=runner, now=lambda: NOW, out=lambda _: None)
    assert len(runner.calls) == 4  # install and pytest, after step 2 and at HEAD
    assert str(runner.calls[2]["workdir"]).endswith("workspace-head")
    assert result.differences == []  # same results in both states
    assert "HEAD" not in format_tests(result)


@requires_git
def test_run_tests_with_network_from_the_configuration(make_project, tmp_path):
    project = python_project(make_project)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "tests.toml").write_text("[tests]\nnetwork = true\ntime_limit_minutes = 0\n")
    runner = FakeRunner()
    result = run_tests(run_acceptance(project), settings(tmp_path / "config"), tmp_path / "run", runner=runner,
                       now=lambda: NOW, out=lambda _: None)
    assert runner.calls[1]["network"] == "code-analysis-net" and runner.calls[1]["timeout"] is None
    assert "Warning: the tests ran with network" in format_tests(result)


@requires_git
def test_failed_install_and_missing_report(make_project, tmp_path):
    project = python_project(make_project)
    runner = FakeRunner(fail=("uv venv", "pytest"))
    result = run_tests(run_acceptance(project), settings(), tmp_path / "run", runner=runner, now=lambda: NOW,
                       out=lambda _: None)
    (run,) = result.runs
    assert run.status is Status.FAILED and not result.complete
    assert run.notes[0].startswith("installing the project with pip failed (exit code 2: broke)")
    assert run.notes[-1].startswith("pytest failed (exit code 2")
    assert "Failed: Python (requirements.txt)" in format_tests(result)


@requires_git
def test_missing_image_makes_step_5_incomplete(make_project, tmp_path):
    class NoImage(FakeRunner):
        def ensure_image(self, image):
            raise DockerError(f"the tool image {image} is missing")

    project = python_project(make_project)
    result = run_tests(run_acceptance(project), settings(), tmp_path / "run", runner=NoImage(), now=lambda: NOW,
                       out=lambda _: None)
    assert result.errors == ["the tool image code-analysis/test-python:latest is missing"] and not result.complete
    assert result.runs == []


@requires_git
def test_suites_without_tests_need_no_docker(make_project, tmp_path):
    project = make_project({"requirements.txt": "", "main.py": "print(1)\n",
                            "Cargo.toml": '[package]\nname = "a"\n', "src/main.rs": "fn main() {}\n"})
    commit_all(project)

    class NoDocker(FakeRunner):
        def check_available(self):
            raise DockerError("Docker is not reachable")

    result = run_tests(run_acceptance(project), settings(), tmp_path / "run", runner=NoDocker(), now=lambda: NOW,
                       out=lambda _: None)
    assert result.complete and result.runs == []
    summary = json.loads((tmp_path / "run" / "step5-tests" / "summary.json").read_text())
    assert summary["detection"]["languages_without_tests"] == ["Python", "Rust"]
    assert "tests" not in summary and not (tmp_path / "run" / "step5-tests" / "tests.json").exists()

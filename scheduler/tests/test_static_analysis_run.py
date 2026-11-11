"""Step 3 orchestration with a fake Docker runner (no containers needed)."""

import json
from datetime import datetime
from pathlib import Path

from conftest import commit_all, opengrep_config, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting.docker import DockerError
from scheduler.linting.model import CommandResult, Status
from scheduler.static_analysis import run_static_analysis

NOW = datetime(2026, 11, 11, 12, 0, 0)
FIXTURE = Path(__file__).parent / "fixtures" / "opengrep" / "opengrep.json"
IMAGE_VERSIONS = {
    "opengrep": "1.33.1",
    "ruleset:opengrep-rules": "f1d2b562b414783763fd02a6ed2736eaed622efa",
    "ruleset:semgrep-rules": "a5ad1714f00c9f7fde980cab0ea1d6dc6d414fd2",
    "ruleset:0xdea-semgrep-rules": "b9942606d3543e3145a95332e6543fb79a01af00",
}


class FakeRunner:
    """Stands in for DockerRunner: writes a prepared report instead of running OpenGrep."""

    def __init__(self, report: str | None, exit_code: int = 0, versions=None, unavailable=None):
        self.report = report
        self.exit_code = exit_code
        self.versions = IMAGE_VERSIONS if versions is None else versions
        self.unavailable = unavailable
        self.calls = []

    def check_available(self):
        if self.unavailable:
            raise DockerError(self.unavailable)

    def ensure_image(self, image):
        pass

    def image_versions(self, image):
        return self.versions

    def run(self, image, command, *, workdir, mounts, env=None, timeout, read_only=(), network=None):
        command = list(command)
        self.calls.append({"image": image, "command": command, "workdir": workdir, "mounts": mounts,
                           "read_only": read_only, "env": env, "network": network})
        if self.report is not None:
            output = next(arg for arg in command if arg.startswith("--json-output="))
            Path(output.split("=", 1)[1]).write_text(self.report)
        return CommandResult(tuple(command), self.exit_code, "text report", "Ran 433 rules on 5 files: 6 findings.", 1.0)


def settings(tmp_path: Path, config_dir: Path) -> Settings:
    return Settings(projects_dir=None, host_home=None, ollama_base_url="http://x",
                    output_dir=tmp_path / "results", config_dir=config_dir)


def python_and_cpp_project(make_project) -> Path:
    project = make_project({
        "requirements.txt": "flask\n",
        "src/app.py": "import subprocess\n" * 20,
        "native/copy.cpp": "void f() {}\n",
        "native/broken.c": "int main( {\n",
    })
    commit_all(project)
    return project


def analyse(project: Path, tmp_path: Path, runner, config_dir: Path | None = None):
    """Step 3 with opengrep-rules and 0xdea active, unless `config_dir` says otherwise."""
    config_dir = config_dir or opengrep_config(tmp_path / "config")
    return run_static_analysis(run_acceptance(project), settings(tmp_path, config_dir), tmp_path / "run",
                               runner=runner, now=lambda: NOW, out=lambda _: None)


@requires_git
def test_opengrep_run(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    runner = FakeRunner(FIXTURE.read_text())
    result = analyse(project, tmp_path, runner)

    assert result.complete
    (run,) = result.tool_runs
    assert (run.tool, run.status, run.version) == ("opengrep", Status.PARTIAL, "1.33.1")  # broken.c
    assert len(run.findings) == 6 and run.rules_run == 433 and run.files_scanned == 5

    call = runner.calls[0]
    command = call["command"]
    configs = [command[i + 1] for i, arg in enumerate(command) if arg == "--config"]
    # Folders of the project's languages, in the order of the config file.
    assert configs == [
        "/opt/code-analysis/opengrep/rulesets/opengrep-rules/c",
        "/opt/code-analysis/opengrep/rulesets/opengrep-rules/python",
        "/opt/code-analysis/opengrep/rulesets/0xdea-semgrep-rules/rules/c",
    ]
    assert "--taint-intrafile" in command and "--disable-version-check" in command
    assert command[-1] == "." and call["workdir"] == project
    assert call["read_only"] == [project]  # the project can't be changed
    assert call["mounts"] == [tmp_path / "run" / "step3-static-analysis"]
    assert call["network"] == "none"
    assert call["env"] == {
        "SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE": "/opt/code-analysis/opengrep/default.semgrepignore"
    }

    step = tmp_path / "run" / "step3-static-analysis"
    summary = json.loads((step / "summary.json").read_text())
    tool = summary["tools"][0]
    assert summary["complete"] is True and summary["languages"] == ["Python", "C", "C++"]
    assert tool["status"] == "partial" and tool["findings_by_category"] == {"security": 6}
    assert [entry["name"] for entry in tool["rulesets"]] == ["opengrep-rules", "0xdea-semgrep-rules"]
    assert tool["rulesets"][0]["folders"] == ["c", "python"]
    assert "native/broken.c (Syntax error)" in tool["notes"][0]
    findings = json.loads((step / "findings.json").read_text())
    assert {finding["ruleset"] for finding in findings} == {"opengrep-rules", "0xdea-semgrep-rules"}
    assert (step / "opengrep" / "opengrep.stdout.txt").read_text() == "text report"


@requires_git
def test_image_with_another_commit_of_a_rule_set(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    versions = {**IMAGE_VERSIONS, "ruleset:opengrep-rules": "0" * 40}
    runner = FakeRunner(None, versions=versions)
    result = analyse(project, tmp_path, runner)
    assert not result.complete
    assert runner.calls == []
    note = result.tool_runs[0].notes[-1]
    assert "opengrep-rules in the image at another commit than in opengrep.toml" in note
    assert "--profile tools build opengrep" in note


@requires_git
def test_activated_rule_set_that_was_not_downloaded(make_project, tmp_path):
    # The default image only contains the rule sets active at build time.
    project = python_and_cpp_project(make_project)
    config_dir = opengrep_config(tmp_path / "switched", ("semgrep-rules", "0xdea-semgrep-rules"))
    versions = {key: value for key, value in IMAGE_VERSIONS.items() if key != "ruleset:semgrep-rules"}
    runner = FakeRunner(None, versions=versions)
    result = analyse(project, tmp_path, runner, config_dir)
    assert not result.complete and runner.calls == []
    note = result.tool_runs[0].notes[-1]
    assert "semgrep-rules not in the image" in note
    assert "accepting their licenses" in note and "--profile tools build opengrep" in note


@requires_git
def test_switching_to_semgrep_rules(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    config_dir = opengrep_config(tmp_path / "switched", ("semgrep-rules", "0xdea-semgrep-rules"))
    runner = FakeRunner(json.dumps({"results": [], "errors": [], "paths": {"scanned": []}}))
    result = analyse(project, tmp_path, runner, config_dir)
    assert result.tool_runs[0].status is Status.OK
    assert "/opt/code-analysis/opengrep/rulesets/semgrep-rules/python" in runner.calls[0]["command"]
    assert [entry["name"] for entry in result.tool_runs[0].rulesets] == ["semgrep-rules", "0xdea-semgrep-rules"]


@requires_git
def test_language_without_rules_is_noted(make_project, tmp_path):
    project = make_project({"conanfile.txt": "[requires]\n", "main.cpp": "int main() {}\n"})
    commit_all(project)
    config_dir = opengrep_config(tmp_path / "only-opengrep-rules", ("opengrep-rules",))
    runner = FakeRunner(None)
    result = analyse(project, tmp_path, runner, config_dir)
    (run,) = result.tool_runs
    assert run.status is Status.SKIPPED and runner.calls == []
    assert run.notes == ["no rules for C++ in the active rule sets (opengrep-rules)"]
    assert result.complete


@requires_git
def test_opengrep_fails(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    report = json.dumps({"results": [], "errors": [{"type": "Rule parse error", "message": "bad rule\nmore"}]})
    result = analyse(project, tmp_path, FakeRunner(report, exit_code=2))
    (run,) = result.tool_runs
    assert run.status is Status.FAILED and not result.complete
    assert run.notes[0].startswith("opengrep failed (exit code 2)")
    assert run.notes[1] == "Rule parse error: bad rule"


@requires_git
def test_missing_report_fails(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    result = analyse(project, tmp_path, FakeRunner(None))
    assert result.tool_runs[0].status is Status.FAILED


@requires_git
def test_invalid_config_and_docker_errors(make_project, tmp_path):
    project = python_and_cpp_project(make_project)
    result = analyse(project, tmp_path, FakeRunner(None), config_dir=tmp_path / "nothing")
    assert "opengrep.toml is missing" in result.errors[0] and not result.complete
    summary = json.loads((tmp_path / "run" / "step3-static-analysis" / "summary.json").read_text())
    assert summary["errors"] == result.errors and summary["tools"] == []

    result = analyse(project, tmp_path, FakeRunner(None, unavailable="Docker is not reachable"))
    assert result.errors == ["Docker is not reachable"]

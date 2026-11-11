"""Step 2 orchestration with a fake Docker runner (no containers needed)."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import commit_all, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting import Status, run_linting
from scheduler.linting.docker import DockerError
from scheduler.linting.languages import groups_for
from scheduler.linting.model import CommandResult

NOW = datetime(2026, 11, 7, 12, 0, 0)


class FakeRunner:
    """Stands in for DockerRunner: calls a handler per tool instead of a container."""

    def __init__(self, handlers, versions=None, unavailable=None):
        self.handlers = handlers
        self.versions = versions or {}
        self.unavailable = unavailable
        self.calls = []

    def check_available(self):
        if self.unavailable:
            raise DockerError(self.unavailable)

    def ensure_image(self, image):
        pass

    def image_versions(self, image):
        return self.versions

    def run(self, image, command, *, workdir, mounts, env=None, timeout):
        command = list(command)
        self.calls.append({"image": image, "command": command, "workdir": workdir, "mounts": mounts, "env": env})
        exit_code = self.handlers[" ".join(command[:2])](command, workdir)
        return CommandResult(tuple(command), exit_code, "stdout", "stderr", 0.1)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        projects_dir=None, host_home=None, ollama_base_url="http://x", output_dir=tmp_path / "results"
    )


def option(command, name):
    return command[command.index(name) + 1]


def sarif(project: Path, rule: str, file: str, line: int) -> str:
    return json.dumps({
        "version": "2.1.0",
        "runs": [{"results": [{
            "ruleId": rule,
            "level": "error",
            "message": {"text": f"{rule} message"},
            "locations": [{"physicalLocation": {
                "artifactLocation": {"uri": f"file://{project}/{file}"},
                "region": {"startLine": line, "startColumn": 1},
            }}],
        }]}],
    })


@requires_git
def test_python_project(make_project, tmp_path):
    project = make_project({
        "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.9"\n',
        "app.py": "x = 1\n",
    })
    commit_all(project)

    def ruff_check(command, workdir):
        Path(option(command, "--output-file")).write_text(sarif(project, "F841", "app.py", 1))
        (project / "app.py").write_text("x = 1  # fixed\n")
        return 1  # findings remain: normal

    def ruff_format(command, workdir):
        (project / "app.py").write_text("x = 1  # fixed and formatted\n")
        return 0

    runner = FakeRunner({"ruff check": ruff_check, "ruff format": ruff_format}, versions={"ruff": "ruff 0.17.4"})
    result = run_linting(run_acceptance(project), settings(tmp_path), runner=runner, now=lambda: NOW, out=lambda _: None)

    assert result.complete
    assert [(run.tool, run.status, run.version) for run in result.tool_runs] == [
        ("ruff check", Status.OK, "ruff 0.17.4"),
        ("ruff format", Status.OK, "ruff 0.17.4"),
    ]
    check = runner.calls[0]["command"]
    assert "--isolated" in check and option(check, "--target-version") == "py39"
    assert runner.calls[0]["workdir"] == project
    assert result.changed_files == ["app.py"]
    assert [run.changed_files for run in result.tool_runs] == [["app.py"], ["app.py"]]

    run_dir = tmp_path / "results" / project.name / "2026-11-07_120000"
    assert result.run_dir == run_dir
    run_info = json.loads((run_dir / "run.json").read_text())
    assert run_info["languages"]["primary"] == "Python" and run_info["commit"]

    step = run_dir / "step2-linting"
    summary = json.loads((step / "summary.json").read_text())
    assert summary["complete"] is True and summary["changed_files"] == ["app.py"]
    assert summary["tools"][0]["findings"] == 1
    assert summary["tools"][0]["commands"][0]["exit_code"] == 1
    assert any("--target-version py39" in note for note in summary["tools"][0]["notes"])

    findings = json.loads((step / "findings.json").read_text())
    assert findings == [{
        "language": "Python", "tool": "ruff check", "rule": "F841", "severity": "error",
        "message": "F841 message", "file": "app.py", "line": 1, "column": 1, "fixed": False,
    }]
    assert "+x = 1  # fixed and formatted" in (step / "changes.patch").read_text()
    assert "+x = 1  # fixed" in (step / "python" / "ruff-check" / "changes.patch").read_text()
    assert (step / "python" / "ruff-check" / "ruff-check.stdout.txt").read_text() == "stdout"
    # The project keeps the changes, uncommitted.
    assert (project / "app.py").read_text() == "x = 1  # fixed and formatted\n"


@requires_git
def test_biome_configs_are_hidden_and_a_missing_report_fails(make_project, tmp_path):
    project = make_project({
        "package.json": "{}",
        "src/a.ts": "export const a = 1\n",
        "biome.json": '{"formatter": {"indentStyle": "space"}}\n',
        "pkg/biome.json": '{"root": false}\n',
    })
    commit_all(project)
    seen = {}

    def biome(command, workdir):
        seen["configs"] = [(project / "biome.json").exists(), (project / "pkg" / "biome.json").exists()]
        seen["tool config"] = (project / ".code-analysis-biome.json").read_text()
        seen["config path"] = command[3]
        return 1  # crashed: no report written

    result = run_linting(
        run_acceptance(project), settings(tmp_path), runner=FakeRunner({"biome check": biome}),
        now=lambda: NOW, out=lambda _: None,
    )

    assert seen == {
        "configs": [False, False],
        "tool config": "{}\n",
        "config path": f"--config-path={project}/.code-analysis-biome.json",
    }
    assert (project / "biome.json").read_text() == '{"formatter": {"indentStyle": "space"}}\n'
    assert (project / "pkg" / "biome.json").exists()
    assert not (project / ".code-analysis-biome.json").exists()
    assert result.tool_runs[0].status is Status.FAILED
    assert "Biome wrote no report" in result.tool_runs[0].notes[-1]
    assert not result.complete
    assert result.changed_files == []  # hiding and restoring is not a change
    assert not (result.run_dir / "step2-linting" / "hidden").exists()


@requires_git
def test_docker_unavailable(make_project, tmp_path):
    project = make_project({"app.py": "x = 1\n", "requirements.txt": ""})
    commit_all(project)
    result = run_linting(
        run_acceptance(project), settings(tmp_path), runner=FakeRunner({}, unavailable="Docker is not reachable"),
        now=lambda: NOW, out=lambda _: None,
    )
    assert result.errors == ["Docker is not reachable"]
    assert not result.complete
    summary = json.loads((result.run_dir / "step2-linting" / "summary.json").read_text())
    assert summary["errors"] == ["Docker is not reachable"] and summary["tools"] == []


@requires_git
def test_output_dir_missing(make_project):
    project = make_project({"app.py": "x = 1\n", "requirements.txt": ""})
    commit_all(project)
    plain = Settings(projects_dir=None, host_home=None, ollama_base_url="http://x")
    result = run_linting(run_acceptance(project), plain, runner=FakeRunner({}), out=lambda _: None)
    assert result.run_dir is None and "OUTPUT_DIR is not set" in result.errors[0]


@pytest.mark.parametrize(
    ("languages", "expected"),
    [
        (("C++", "C", "Go"), ["C/C++"]),
        (("TypeScript", "Python", "JavaScript"), ["JavaScript/TypeScript", "Python"]),
        (("Rust", "C#", "Java"), ["Rust", "C#", "Java"]),
    ],
)
def test_language_groups(languages, expected):
    assert [group.name for group in groups_for(languages)] == expected


@requires_git
def test_language_without_files_known_to_git_is_skipped(make_project, tmp_path):
    # Step 1 sees every file; step 2 only the ones git knows (here: ignored).
    project = make_project({
        "requirements.txt": "",
        ".gitignore": "generated/\n",
        "generated/app.py": "x = 1\n",
    })
    commit_all(project)
    runner = FakeRunner({})  # any tool call would fail with a KeyError
    result = run_linting(run_acceptance(project), settings(tmp_path), runner=runner, now=lambda: NOW, out=lambda _: None)
    assert [(run.tool, run.status) for run in result.tool_runs] == [
        ("ruff check", Status.SKIPPED),
        ("ruff format", Status.SKIPPED),
    ]
    assert runner.calls == [] and result.complete


PROTOBUF = "# Generated by the protocol buffer compiler.  DO NOT EDIT!\nimport x\n"
SWIG = "/* This file was automatically generated by SWIG (http://www.swig.org). */\npackage a;\nclass B {}\n"


@requires_git
def test_generated_files_are_neither_changed_nor_reported(make_project, tmp_path):
    # Ruff works on the whole folder: it reaches the generated file anyway.
    project = make_project({
        "requirements.txt": "",
        "app.py": "x = 1\n",
        "api/message_pb2.py": PROTOBUF,
    })
    commit_all(project)

    def ruff_check(command, workdir):
        findings = [json.loads(sarif(project, rule, file, 1))["runs"][0]["results"][0]
                    for rule, file in (("F401", "api/message_pb2.py"), ("F841", "app.py"))]
        Path(option(command, "--output-file")).write_text(json.dumps({"runs": [{"results": findings}]}))
        (project / "api/message_pb2.py").write_text(PROTOBUF + "# fixed\n")
        return 1

    def ruff_format(command, workdir):
        (project / "api/message_pb2.py").write_text(PROTOBUF.replace("import x", "import  x"))
        (project / "app.py").write_text("x = 1  # formatted\n")
        return 0

    runner = FakeRunner({"ruff check": ruff_check, "ruff format": ruff_format})
    result = run_linting(run_acceptance(project), settings(tmp_path), runner=runner, now=lambda: NOW, out=lambda _: None)

    assert result.complete
    assert [finding.file for finding in result.findings] == ["app.py"]
    check, format_ = result.tool_runs
    assert "1 findings in generated files left out" in check.notes
    assert "changes to 1 generated file(s) undone" in check.notes
    assert check.changed_files == [] and format_.changed_files == ["app.py"]
    assert (project / "api/message_pb2.py").read_text() == PROTOBUF
    assert result.changed_files == ["app.py"]

    step = result.run_dir / "step2-linting"
    assert json.loads((step / "excluded-files.json").read_text()) == [{
        "file": "api/message_pb2.py",
        "reason": "generated: Generated by the protocol buffer compiler.  DO NOT EDIT!",
    }]
    assert json.loads((step / "summary.json").read_text())["excluded_files"] == 1
    assert not (step / "generated-originals").exists()


@requires_git
def test_generated_files_are_not_given_to_the_tools(make_project, tmp_path):
    project = make_project({
        "pom.xml": "<project/>\n",
        "src/App.java": "class App {}\n",
        "swig/B.java": SWIG,
    })
    commit_all(project)
    seen = {}

    def gjf(command, workdir):
        seen["gjf"] = Path(command[-1]).read_bytes().split(b"\0")
        return 0

    def pmd(command, workdir):
        seen["pmd"] = Path(option(command, "--exclude-file-list")).read_text().splitlines()
        Path(option(command, "-r")).write_text(json.dumps({"runs": [{"results": []}]}))
        return 0

    runner = FakeRunner({"sh -c": gjf, "pmd check": pmd})
    result = run_linting(run_acceptance(project), settings(tmp_path), runner=runner, now=lambda: NOW, out=lambda _: None)

    assert result.complete
    assert seen["gjf"] == [str(project / "src/App.java").encode(), b""]
    assert seen["pmd"] == [str(project / "swig/B.java")]

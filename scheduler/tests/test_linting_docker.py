"""Step 2 with the real tool images (`pytest -m docker`).

Needs Docker and the images (`docker compose -f docker/docker-compose.yml
--profile tools build`). The test projects must lie in a folder Docker can
mount; set CODE_ANALYSIS_DOCKER_TEST_DIR to one (e.g. below your home folder)
if the temporary folder is not shared with Docker.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from conftest import commit_all

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting import Status, run_linting
from scheduler.linting.docker import DockerError, DockerRunner

pytestmark = pytest.mark.docker

IMAGES = [f"code-analysis/lint-{name}:latest" for name in ("cpp", "dotnet", "java", "node", "python", "rust")]


@pytest.fixture(scope="module")
def runner():
    runner = DockerRunner(network="bridge")
    try:
        runner.check_available()
        for image in IMAGES:
            runner.ensure_image(image)
    except DockerError as exc:
        pytest.skip(f"Docker or tool images not available: {exc}")
    return runner


@pytest.fixture
def workspace(tmp_path, runner):
    base = Path(os.environ.get("CODE_ANALYSIS_DOCKER_TEST_DIR") or tmp_path).resolve()
    folder = base / f"itest-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    probe = subprocess.run(
        ["docker", "run", "--rm", "--user", runner.user, "--mount", f"type=bind,source={folder},target={folder}",
         "code-analysis/lint-python:latest", "touch", str(folder / "probe")],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        pytest.skip(f"{base} cannot be mounted by Docker; set CODE_ANALYSIS_DOCKER_TEST_DIR")
    (folder / "probe").unlink()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def project(workspace: Path, files: dict[str, str]) -> Path:
    root = workspace / "project"
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit_all(root)
    return root


def lint(root: Path, workspace: Path, runner):
    settings = Settings(projects_dir=None, host_home=None, ollama_base_url="x", output_dir=workspace / "results")
    acceptance = run_acceptance(root)
    assert acceptance.accepted, acceptance.message
    return run_linting(acceptance, settings, runner=runner, out=lambda _: None)


def rules(result) -> set[str]:
    return {finding.rule for finding in result.findings}


def test_python(workspace, runner):
    root = project(workspace, {
        "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.8"\n\n[tool.ruff]\nline-length = 200\n',
        "m.py": "import os\nfrom typing import Optional\n\n\ndef f(x: Optional[int]) -> Optional[int]:\n"
                "    unused = 3\n    return x\n\n\nLONG = '" + "a" * 80 + "' + 'b'\n",
    })
    result = lint(root, workspace, runner)
    assert result.complete
    assert "F841" in rules(result)  # unsafe fix: reported, not applied
    text = (root / "m.py").read_text()
    assert "import os" not in text  # safe fix applied
    assert "Optional[int]" in text  # py38: no rewrite to int | None
    assert "LONG = (" in text  # project's line-length 200 ignored, default 88


def test_javascript(workspace, runner):
    root = project(workspace, {
        "package.json": '{"name": "x"}\n',
        "biome.json": '{"formatter": {"indentStyle": "space"}, "linter": {"rules": {"suspicious": {"noDoubleEquals": "off"}}}}\n',
        ".gitignore": "dist/\n",
        "src/a.ts": "export function f(x: number) {\nif (x == 1) { return 'a' }\nreturn 'b'\n}\n",
    })
    (root / "dist").mkdir()
    (root / "dist" / "out.js").write_text("var  y = 2\n")
    result = lint(root, workspace, runner)
    assert result.complete
    assert "lint/suspicious/noDoubleEquals" in rules(result)  # project config ignored
    assert "\tif (x == 1) {" in (root / "src" / "a.ts").read_text()  # Biome default: tabs
    assert (root / "dist" / "out.js").read_text() == "var  y = 2\n"  # .gitignore respected
    assert (root / "biome.json").is_file() and not (root / ".code-analysis-biome.json").exists()


def test_java(workspace, runner):
    root = project(workspace, {
        "pom.xml": "<project><modelVersion>4.0.0</modelVersion><groupId>x</groupId><artifactId>x</artifactId>"
                   "<version>1</version><properties><maven.compiler.release>17</maven.compiler.release>"
                   "</properties></project>\n",
        "src/main/java/x/A.java": "package x;\nimport java.util.Map;\npublic class A {\n    private int unused;\n"
                                  "    public boolean f(String s) { return s == \"y\"; }\n}\n",
    })
    result = lint(root, workspace, runner)
    assert result.complete
    assert {"UnusedPrivateField", "UseEqualsToCompareStrings"} <= rules(result)
    text = (root / "src/main/java/x/A.java").read_text()
    assert "import java.util.Map" not in text and "  private int unused;" in text  # Google style, 2 spaces
    pmd = next(run for run in result.tool_runs if run.tool == "pmd")
    assert any("java-17" in note for note in pmd.notes)


def test_csharp(workspace, runner):
    root = project(workspace, {
        "app.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                      "<TargetFramework>net10.0</TargetFramework></PropertyGroup></Project>\n",
        ".editorconfig": "root = true\n[*.cs]\nindent_size = 2\ncsharp_new_line_before_open_brace = none\n",
        ".gitignore": "bin/\nobj/\n",
        "Program.cs": "using System;\nnamespace App {\nclass Program {\n  static void Main() {\n"
                      "    try { Run(); } catch (Exception e) { throw e; }\n  }\n  static void Run() { }\n}\n}\n",
    })
    result = lint(root, workspace, runner)
    assert result.complete
    fixed = [finding for finding in result.findings if finding.rule == "CA2200"]
    assert fixed and fixed[0].fixed
    text = (root / "Program.cs").read_text()
    assert "namespace App\n{" in text  # .editorconfig ignored: default Allman braces
    assert (root / ".editorconfig").read_text().startswith("root = true")


def test_rust(workspace, runner):
    root = project(workspace, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\nedition = "2021"\n',
        "rustfmt.toml": "hard_tabs = true\n",
        ".gitignore": "target/\n",
        "src/main.rs": 'fn main() {\n    let v = vec![1, 2, 3];\n    if v.len() == 0 {   println!("empty"); }\n}\n',
    })
    result = lint(root, workspace, runner)
    assert result.complete
    text = (root / "src" / "main.rs").read_text()
    assert "v.is_empty()" in text  # Clippy fix
    assert "\n    if v.is_empty() {\n" in text  # rustfmt default (spaces), rustfmt.toml ignored


def test_cpp_without_compilation_database(workspace, runner):
    root = project(workspace, {
        "conanfile.txt": "[requires]\n",
        ".clang-format-ignore": "src/skip.cpp\n",
        "include/util.h": "#pragma once\ninline int h(int* p) { if (p == 0) return 1; return *p; }\n",
        "src/main.cpp": '#include "util.h"\nint main() {\n  int* q = 0;\n  return h(q);\n}\n',
        "src/skip.cpp": "int skipped(){return 0;}\n",
    })
    result = lint(root, workspace, runner)
    assert result.complete
    tidy = next(run for run in result.tool_runs if run.tool == "clang-tidy")
    assert tidy.status is Status.OK
    assert any("results limited" in note for note in tidy.notes)
    assert "modernize-use-nullptr" in rules(result)
    assert "    int *q = 0;" in (root / "src" / "main.cpp").read_text()  # LLVM style, IndentWidth 4
    assert (root / "src" / "skip.cpp").read_text() == "int skipped() { return 0; }\n"  # ignore file ignored
    assert (root / ".clang-format-ignore").is_file()

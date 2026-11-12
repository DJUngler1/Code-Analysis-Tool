"""Step 4 with the real tool images and the online services (`pytest -m docker`, needs internet).

Needs the images osv-scanner, audit-python, lint-node, lint-rust, lint-dotnet,
lint-cpp and sonar-scanner (`docker compose -f docker/docker-compose.yml
--profile tools build`). The test projects must lie in a folder Docker can
mount (CODE_ANALYSIS_DOCKER_TEST_DIR).
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
from scheduler.dependencies import lockfiles, osv, run_dependencies
from scheduler.linting.docker import DockerError, DockerRunner
from scheduler.linting.model import Status

pytestmark = pytest.mark.docker

IMAGES = [osv.IMAGE, lockfiles.NODE_IMAGE, lockfiles.RUST_IMAGE, lockfiles.DOTNET_IMAGE, lockfiles.CPP_IMAGE,
          lockfiles.JAVA_IMAGE, lockfiles.PYTHON_IMAGE]
POM = """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>
<groupId>x</groupId><artifactId>x</artifactId><version>1</version><dependencies><dependency>
<groupId>org.apache.logging.log4j</groupId><artifactId>log4j-core</artifactId><version>2.14.1</version>
</dependency></dependencies></project>
"""
LOG4SHELL = "CVE-2021-44228"


@pytest.fixture(scope="module")
def runner():
    runner = DockerRunner(network="code-analysis-net")
    try:
        runner.check_available()
        for image in IMAGES:
            runner.ensure_image(image)
    except DockerError as exc:
        pytest.skip(f"Docker or the tool images not available: {exc}")
    return runner


@pytest.fixture
def workspace(tmp_path, runner):
    base = Path(os.environ.get("CODE_ANALYSIS_DOCKER_TEST_DIR") or tmp_path).resolve()
    folder = base / f"itest-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def write(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def in_container(runner: DockerRunner, image: str, folder: Path, command: str) -> None:
    """Prepare a test project (create a lock file) with the tool images."""
    result = runner.run(image, ["sh", "-c", command], workdir=folder, mounts=[folder.parent], timeout=600)
    assert result.exit_code == 0, result.stderr


def check(root: Path, workspace: Path, runner):
    settings = Settings(projects_dir=None, host_home=None, ollama_base_url="x", output_dir=workspace / "results")
    acceptance = run_acceptance(root)
    assert acceptance.accepted, acceptance.message
    return run_dependencies(acceptance, settings, workspace / "results" / "run", runner=runner, out=lambda _: None)


def ids(findings) -> set[str]:
    return {advisory for finding in findings for advisory in (finding.id, *finding.aliases)}


def test_mixed_project(workspace, runner):
    root = workspace / "project"
    write(root, {
        "web/index.js": "module.exports = require('lodash');\n",
        "web/package.json": '{"name": "web", "dependencies": {"lodash": "4.17.15"}}\n',
        "py/app.py": "import django\n",
        "py/requirements.txt": "django==2.0\n",
        "pipenvapp/app.py": "import jinja2\n",
        "pipenvapp/Pipfile": '[packages]\njinja2 = "==2.10"\n',
        "poetryapp/app.py": "import jinja2\n",
        "poetryapp/pyproject.toml": '[tool.poetry]\nname = "x"\nversion = "0.1.0"\ndescription = ""\nauthors = []\n\n'
                                    '[tool.poetry.dependencies]\npython = ">=3.10"\njinja2 = "2.10"\n',
        "rs/src/main.rs": "fn main() {}\n",
        "rs/Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n\n'
                         '[dependencies]\nsmallvec = "=1.6.0"\n',
        "cs/Program.cs": "class P { static void Main() {} }\n",
        "cs/app.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType>'
                         "<TargetFramework>net10.0</TargetFramework></PropertyGroup><ItemGroup>"
                         '<PackageReference Include="Newtonsoft.Json" Version="12.0.1" /></ItemGroup></Project>\n',
        "maven/src/A.java": "class A {}\n",
        "maven/pom.xml": POM,
        "gradle/src/main/java/B.java": "class B {}\n",
        "gradle/settings.gradle": "rootProject.name = 'x'\n",
        "gradle/build.gradle": "plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                               "dependencies { implementation 'org.apache.logging.log4j:log4j-core:2.14.1' }\n",
        "cpp/main.cpp": "int main() { return 0; }\n",
        "cpp/conanfile.txt": "[requires]\nzlib/1.2.11\n",
    })
    commit_all(root)
    result = check(root, workspace, runner)
    assert result.complete, [(run.tool, run.notes) for run in result.tool_runs if run.status is Status.FAILED]

    coverage = {entry.manager: entry for entry in result.coverage}
    for manager in ("npm", "Pipenv", "Poetry", "Cargo", "NuGet", "Gradle", "Conan"):
        assert coverage[manager].lockfile_state == "generated", (manager, coverage[manager].notes)
    lockfile_dir = workspace / "results/run/step4-dependencies/lockfiles"
    assert (lockfile_dir / "gradle/gradle.lockfile").is_file() and (lockfile_dir / "cs/packages.lock.json").is_file()

    osv_run = next(run for run in result.tool_runs if run.tool == "osv-scanner")
    found = ids(osv_run.findings)
    manifests = {finding.manifest for finding in osv_run.findings}
    assert {"web/package-lock.json", "py/requirements.txt", "pipenvapp/Pipfile.lock", "poetryapp/poetry.lock",
            "rs/Cargo.lock", "cs/packages.lock.json", "maven/pom.xml", "gradle/gradle.lockfile"} <= manifests
    assert {"RUSTSEC-2021-0003", "GHSA-5crp-9r3c-p9vr", LOG4SHELL, "CVE-2021-23337"} <= found
    assert not any(finding.manifest == "cpp/conan.lock" for finding in osv_run.findings)  # no ConanCenter data

    audits = {(run.tool, run.manifest): run for run in result.tool_runs if run.tool != "osv-scanner"}
    # npm audit names only GitHub advisories; the CVE comes from OSV's finding of the same advisory.
    assert "CVE-2021-23337" in ids(audits[("npm audit", "web/package.json")].findings)
    assert "RUSTSEC-2021-0003" in ids(audits[("cargo audit", "rs/Cargo.toml")].findings)
    assert "GHSA-5crp-9r3c-p9vr" in ids(audits[("dotnet package list --vulnerable", "cs/app.csproj")].findings)
    for manifest in ("py/requirements.txt", "pipenvapp/Pipfile", "poetryapp/pyproject.toml"):
        assert audits[("pip-audit", manifest)].findings, manifest
    assert all(finding.severity != "unknown" for finding in audits[("cargo audit", "rs/Cargo.toml")].findings)

    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""  # lock files only in the copy


def test_pnpm_bun_yarn_and_uv(workspace, runner):
    package = '{"name": "x", "version": "1.0.0", "dependencies": {"lodash": "4.17.15"}}\n'
    root = workspace / "project"
    write(root, {"pnpmapp/index.js": "1;\n", "pnpmapp/package.json": package,
                 "bunapp/index.js": "1;\n", "bunapp/package.json": package,
                 "yarnapp/index.js": "1;\n", "yarnapp/package.json": package,
                 "uvapp/app.py": "import jinja2\n",
                 "uvapp/pyproject.toml": '[project]\nname = "x"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n'
                                         'dependencies = ["jinja2==2.10"]\n'})
    in_container(runner, lockfiles.NODE_IMAGE, root / "pnpmapp", "pnpm install --lockfile-only --ignore-scripts")
    in_container(runner, lockfiles.NODE_IMAGE, root / "bunapp", "bun install --lockfile-only --ignore-scripts")
    in_container(runner, lockfiles.NODE_IMAGE, root / "yarnapp",
                 "yarn install --ignore-scripts --silent && rm -rf node_modules")
    in_container(runner, lockfiles.PYTHON_IMAGE, root / "uvapp", "uv lock -q")
    for leftover in root.rglob("node_modules"):
        shutil.rmtree(leftover, ignore_errors=True)
    commit_all(root)
    result = check(root, workspace, runner)
    assert result.complete, [(run.tool, run.notes) for run in result.tool_runs if run.status is Status.FAILED]
    coverage = {entry.manager: entry.lockfile_state for entry in result.coverage}
    assert coverage == {"pnpm": "committed", "Bun": "committed", "Yarn": "committed", "uv": "committed"}
    for tool in ("pnpm audit", "bun audit", "yarn audit"):
        run = next(run for run in result.tool_runs if run.tool == tool)
        assert "GHSA-35jh-r3h4-6jhm" in ids(run.findings), tool
    pip_audit = next(run for run in result.tool_runs if run.tool == "pip-audit")
    assert {finding.package for finding in pip_audit.findings} == {"jinja2"}


def test_gradle_module_that_cannot_be_resolved(workspace, runner):
    """One module with a dependency nobody can resolve must not stop the others from being locked."""
    root = workspace / "project"
    write(root, {
        "settings.gradle": "rootProject.name = 'x'\ninclude 'good', 'broken'\n",
        "good/src/main/java/A.java": "class A {}\n",
        "good/build.gradle": "plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                             "dependencies { implementation 'org.apache.logging.log4j:log4j-core:2.14.1' }\n",
        "broken/src/main/java/B.java": "class B {}\n",
        "broken/build.gradle": "plugins { id 'java' }\nrepositories { mavenCentral() }\n"
                               "dependencies { implementation 'com.example.missing:does-not-exist:1.0' }\n",
    })
    commit_all(root)
    result = check(root, workspace, runner)
    entry = next(entry for entry in result.coverage if entry.manager == "Gradle")
    assert entry.lockfile_state == "partial", entry.notes
    assert ":broken compileClasspath: unresolved com.example.missing:does-not-exist:1.0" in entry.notes[-1]
    assert not result.complete
    osv_run = next(run for run in result.tool_runs if run.tool == "osv-scanner")
    log4j = [finding for finding in osv_run.findings if finding.manifest == "good/gradle.lockfile"]
    assert LOG4SHELL in ids(log4j)
    assert {finding.scope for finding in log4j} == {"runtime"}
    assert "runtimeClasspath" in log4j[0].configurations

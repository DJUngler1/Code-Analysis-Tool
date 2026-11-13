"""Step 5.2 with the real test images: small projects per language with known results.

Needs Docker, the network code-analysis-net (`docker compose ... up -d`), the
test images (docker compose -f docker/docker-compose.yml --profile tools build)
and internet access for the online phase. Docker must be able to mount
CODE_ANALYSIS_DOCKER_TEST_DIR (see README, Development).
"""

import json
import os
import shutil
import uuid
from pathlib import Path

import pytest
from conftest import commit_all

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting.docker import DockerError, DockerRunner
from scheduler.linting.model import Status
from scheduler.testing import run_tests
from scheduler.testing.execute import LANGUAGE_MODULES

pytestmark = pytest.mark.docker
FIXTURES = Path(__file__).parent / "fixtures" / "testing"
# Set CODE_ANALYSIS_KEEP_REPORTS=1 to copy the raw reports to tests/fixtures/testing/ (parser tests).
KEEP_REPORTS = os.environ.get("CODE_ANALYSIS_KEEP_REPORTS") == "1"


@pytest.fixture(scope="module")
def runner():
    runner = DockerRunner(network="code-analysis-net")
    try:
        runner.check_available()
        for module in LANGUAGE_MODULES.values():
            runner.ensure_image(module.IMAGE)
    except DockerError as exc:
        pytest.skip(str(exc))
    return runner


@pytest.fixture
def workspace(tmp_path, runner):
    base = Path(os.environ.get("CODE_ANALYSIS_DOCKER_TEST_DIR") or tmp_path).expanduser().resolve()
    folder = base / f"itest-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def write(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def check(runner, workspace: Path, files: dict[str, str], *, after_commit: dict[str, str] | None = None):
    """Run step 5 on a project; `after_commit` stands for changes step 2 made (uncommitted)."""
    project = workspace / "project"
    write(project, files)
    commit_all(project)
    acceptance = run_acceptance(project)
    assert acceptance.accepted, acceptance.message
    if after_commit:
        write(project, after_commit)
    settings = Settings(projects_dir=None, host_home=None, ollama_base_url="x", output_dir=workspace / "results")
    result = run_tests(acceptance, settings, workspace / "run", runner=runner)
    assert result.errors == []
    assert not (workspace / "run" / "step5-tests" / "workspace").exists()  # the copy is deleted
    return result


def outcomes(run) -> dict[str, str]:
    return {test.id: test.outcome for test in run.tests}


def keep(workspace: Path, run, pattern: str, name: str) -> None:
    if KEEP_REPORTS:
        report = next((workspace / "run" / "step5-tests" / run.output_dir).glob(pattern))
        FIXTURES.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report, FIXTURES / name)


# --- Python ---

PYTHON = {
    "pyproject.toml": ('[project]\nname = "calc"\nversion = "0.1.0"\nrequires-python = ">=3.10"\n\n'
                       '[build-system]\nrequires = ["setuptools>=61"]\nbuild-backend = "setuptools.build_meta"\n\n'
                       '[tool.setuptools]\npackages = ["calc"]\n'),
    "calc/__init__.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": ("import pytest\n\nfrom calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                           "def test_broken():\n    assert add(1, 1) == 3\n\n\n"
                           "@pytest.mark.skip(reason='slow')\ndef test_slow():\n    pass\n"),
    "tests/test_legacy.py": ("import unittest\n\nfrom calc import add\n\n\nclass LegacyTests(unittest.TestCase):\n"
                             "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n"),
}


def test_python_pytest_without_network(runner, workspace):
    files = {**PYTHON, "tests/test_network.py": (
        "import socket\n\n\ndef test_network():\n    socket.create_connection(('pypi.org', 443), timeout=5)\n")}
    result = check(runner, workspace, files)
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert run.version.startswith("pytest 9.2.0, Python 3.13")  # pytest added by the tool
    assert outcomes(run) == {
        "tests.test_calc.test_add": "passed",
        "tests.test_calc.test_broken": "failed",
        "tests.test_calc.test_slow": "skipped",
        "tests.test_legacy.LegacyTests.test_add": "passed",
        "tests.test_network.test_network": "failed",  # no network in the test run
    }
    assert result.head_runs == [] and result.differences == []  # step 2 changed nothing
    summary = json.loads((workspace / "run" / "step5-tests" / "summary.json").read_text())
    assert summary["tests"]["network"] is False
    assert "differences_to_head" not in summary["tests"]
    keep(workspace, run, "junit.xml", "pytest-junit.xml")


def test_python_differences_to_head(runner, workspace):
    result = check(runner, workspace, PYTHON, after_commit={"calc/__init__.py": "def add(a, b):\n    return a - b\n"})
    assert outcomes(result.runs[0])["tests.test_calc.test_add"] == "failed"
    assert outcomes(result.head_runs[0])["tests.test_calc.test_add"] == "passed"
    (comparison,) = result.differences
    differences = {test.id: (test.after_step_2, test.head) for test in comparison.tests}
    assert differences == {"tests.test_calc.test_add": ("failed", "passed"),
                           "tests.test_legacy.LegacyTests.test_add": ("failed", "passed")}
    summary = json.loads((workspace / "run" / "step5-tests" / "summary.json").read_text())
    assert summary["tests"]["differences_to_head"][0]["suite"] == "Python (pyproject.toml)"


# --- Rust ---

RUST = {
    "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": ("/// Adds two numbers.\n///\n/// ```\n/// assert_eq!(calc::add(1, 2), 3);\n/// ```\n"
                   "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n#[cfg(test)]\nmod tests {\n"
                   "    use super::add;\n\n    #[test]\n    fn adds() {\n        assert_eq!(add(1, 2), 3);\n    }\n\n"
                   "    #[test]\n    fn broken() {\n        assert_eq!(add(1, 1), 3);\n    }\n\n"
                   "    #[test]\n    #[ignore]\n    fn slow() {}\n}\n"),
    "tests/integration.rs": "#[test]\nfn integration() {\n    assert_eq!(calc::add(2, 2), 4);\n}\n",
}


def test_rust_cargo_test(runner, workspace):
    result = check(runner, workspace, RUST)
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert outcomes(run) == {
        "calc src/lib.rs::tests::adds": "passed",
        "calc src/lib.rs::tests::broken": "failed",
        "calc src/lib.rs::tests::slow": "skipped",
        "integration tests/integration.rs::integration": "passed",
        "calc doctests::src/lib.rs - add": "passed",
    }
    broken = next(test for test in run.tests if test.id.endswith("broken"))
    assert "assertion `left == right` failed" in broken.message
    keep(workspace, run, "cargo-test.stdout.txt", "cargo-test.txt")


# --- C# ---

CSHARP = {
    "Calc.sln": "",
    "Calc/Calc.csproj": ('<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
                        "</PropertyGroup></Project>\n"),
    "Calc/Calculator.cs": "namespace Calc;\n\npublic static class Calculator\n{\n"
                          "    public static int Add(int a, int b) => a + b;\n}\n",
    "Calc.Tests/Calc.Tests.csproj": (
        '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework>'
        "<IsPackable>false</IsPackable></PropertyGroup><ItemGroup>"
        '<PackageReference Include="Microsoft.NET.Test.Sdk" Version="18.12.0" />'
        '<PackageReference Include="xunit" Version="2.9.3" />'
        '<PackageReference Include="xunit.runner.visualstudio" Version="4.0.1" />'
        '</ItemGroup><ItemGroup><ProjectReference Include="../Calc/Calc.csproj" /></ItemGroup></Project>\n'),
    "Calc.Tests/CalculatorTests.cs": (
        "using Calc;\nusing Xunit;\n\nnamespace Calc.Tests;\n\npublic class CalculatorTests\n{\n"
        "    [Fact]\n    public void Adds() => Assert.Equal(3, Calculator.Add(1, 2));\n\n"
        "    [Fact]\n    public void Broken() => Assert.Equal(3, Calculator.Add(1, 1));\n\n"
        '    [Fact(Skip = "slow")]\n    public void Slow() { }\n\n'
        "    [Theory]\n    [InlineData(1, 1, 2)]\n    [InlineData(2, 2, 4)]\n"
        "    public void AddsMany(int a, int b, int sum) => Assert.Equal(sum, Calculator.Add(a, b));\n}\n"),
}


def test_csharp_dotnet_test_on_net8(runner, workspace):
    result = check(runner, workspace, CSHARP)
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert outcomes(run) == {
        "Calc.Tests.CalculatorTests.Adds": "passed",
        "Calc.Tests.CalculatorTests.Broken": "failed",
        "Calc.Tests.CalculatorTests.Slow": "skipped",
        "Calc.Tests.CalculatorTests.AddsMany(a: 1, b: 1, sum: 2)": "passed",
        "Calc.Tests.CalculatorTests.AddsMany(a: 2, b: 2, sum: 4)": "passed",
    }
    keep(workspace, run, "trx/*.trx", "dotnet.trx")


# --- Java ---

JAVA_SOURCES = {
    "src/main/java/calc/Calc.java": "package calc;\n\npublic class Calc {\n"
                                    "    public static int add(int a, int b) {\n        return a + b;\n    }\n}\n",
    "src/test/java/calc/CalcTest.java": (
        "package calc;\n\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
        "import org.junit.jupiter.api.Disabled;\nimport org.junit.jupiter.api.Test;\n\nclass CalcTest {\n"
        "    @Test\n    void adds() {\n        assertEquals(3, Calc.add(1, 2));\n    }\n\n"
        "    @Test\n    void broken() {\n        assertEquals(3, Calc.add(1, 1));\n    }\n\n"
        '    @Disabled("slow")\n    @Test\n    void slow() {}\n}\n'),
}
JAVA_OUTCOMES = {"calc.CalcTest.adds": "passed", "calc.CalcTest.broken": "failed", "calc.CalcTest.slow": "skipped"}


def test_java_maven(runner, workspace):
    pom = (
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        "<groupId>calc</groupId><artifactId>calc</artifactId><version>1.0</version>"
        "<properties><maven.compiler.release>21</maven.compiler.release>"
        "<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>"
        "<dependencies><dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>"
        "<version>6.1.3</version><scope>test</scope></dependency></dependencies>"
        "<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>"
        "<artifactId>maven-surefire-plugin</artifactId><version>3.6.1</version></plugin></plugins></build>"
        "</project>\n")
    result = check(runner, workspace, {"pom.xml": pom, **JAVA_SOURCES})
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert outcomes(run) == JAVA_OUTCOMES
    keep(workspace, run, "reports/*.xml", "surefire.xml")


def test_java_gradle(runner, workspace):
    build = ("plugins { id 'java' }\nrepositories { mavenCentral() }\n"
             "java { toolchain { languageVersion = JavaLanguageVersion.of(21) } }\n"
             "dependencies {\n    testImplementation 'org.junit.jupiter:junit-jupiter:6.1.3'\n"
             "    testRuntimeOnly 'org.junit.platform:junit-platform-launcher:6.1.3'\n}\n"
             "test { useJUnitPlatform() }\n")
    result = check(runner, workspace, {"settings.gradle": "rootProject.name = 'calc'\n", "build.gradle": build,
                                       **JAVA_SOURCES})
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    # Gradle names JUnit Platform tests by their display name, "adds()".
    assert outcomes(run) == {f"{test_id}()": outcome for test_id, outcome in JAVA_OUTCOMES.items()}
    keep(workspace, run, "reports/*.xml", "gradle.xml")


# --- JavaScript and TypeScript ---

JS_SOURCE = {"src/calc.js": "export function add(a, b) {\n  return a + b;\n}\n"}
JS_TESTS = ("import { describe, expect, it } from '{module}';\nimport {{ add }} from '../src/calc.js';\n\n"
            "describe('calc', () => {\n  it('adds', () => expect(add(1, 2)).toBe(3));\n"
            "  it('is broken', () => expect(add(1, 1)).toBe(3));\n  it.skip('is slow', () => {});\n});\n")


def test_javascript_vitest(runner, workspace):
    package = {"name": "calc", "type": "module", "scripts": {"test": "vitest"},
               "devDependencies": {"vitest": "5.1.0"}}
    result = check(runner, workspace, {"package.json": json.dumps(package), **JS_SOURCE,
                                       "test/calc.test.js": JS_TESTS.replace("{module}", "vitest")
                                       .replace("{{", "{").replace("}}", "}")})
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert set(outcomes(run).values()) == {"passed", "failed", "skipped"} and len(run.tests) == 3
    keep(workspace, run, "junit.xml", "vitest-junit.xml")


def test_javascript_jest(runner, workspace):
    package = {"name": "calc", "scripts": {"test": "jest"}, "devDependencies": {"jest": "30.5.2"}}
    tests = ("const { add } = require('../src/calc.cjs');\n\ndescribe('calc', () => {\n"
             "  it('adds', () => expect(add(1, 2)).toBe(3));\n  it('is broken', () => expect(add(1, 1)).toBe(3));\n"
             "  it.skip('is slow', () => {});\n});\n")
    result = check(runner, workspace, {"package.json": json.dumps(package),
                                       "src/calc.cjs": "exports.add = (a, b) => a + b;\n",
                                       "__tests__/calc.test.js": tests})
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert outcomes(run) == {"__tests__/calc.test.js > calc adds": "passed",
                             "__tests__/calc.test.js > calc is broken": "failed",
                             "__tests__/calc.test.js > calc is slow": "skipped"}
    keep(workspace, run, "jest.json", "jest.json")


def test_javascript_node_test_runner(runner, workspace):
    package = {"name": "calc", "type": "module", "scripts": {"test": "node --test"}}
    tests = ("import { describe, it } from 'node:test';\nimport assert from 'node:assert/strict';\n"
             "import { add } from '../src/calc.js';\n\ndescribe('calc', () => {\n"
             "  it('adds', () => assert.equal(add(1, 2), 3));\n  it('is broken', () => assert.equal(add(1, 1), 3));\n"
             "  it('is slow', { skip: true }, () => {});\n});\n")
    result = check(runner, workspace, {"package.json": json.dumps(package), **JS_SOURCE, "test/calc.test.js": tests})
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert sorted(outcomes(run).values()) == ["failed", "passed", "skipped"]
    keep(workspace, run, "junit.xml", "node-test-junit.xml")


# --- C and C++ ---


def test_cpp_ctest(runner, workspace):
    files = {
        "vcpkg.json": '{"name": "calc", "version": "0.1.0", "dependencies": []}\n',
        "CMakeLists.txt": ("cmake_minimum_required(VERSION 3.21)\nproject(calc C)\nenable_testing()\n"
                           "add_executable(passes tests/passes.c)\nadd_executable(fails tests/fails.c)\n"
                           "add_test(NAME passes COMMAND passes)\nadd_test(NAME fails COMMAND fails)\n"),
        "tests/passes.c": "int main(void) { return 0; }\n",
        "tests/fails.c": '#include <assert.h>\nint main(void) { assert(1 + 1 == 3); return 0; }\n',
    }
    result = check(runner, workspace, files)
    run = result.runs[0]
    assert run.status is Status.OK, run.notes
    assert outcomes(run) == {"passes": "passed", "fails": "failed"}  # Debug build: assert() is active
    keep(workspace, run, "ctest-junit.xml", "ctest-junit.xml")

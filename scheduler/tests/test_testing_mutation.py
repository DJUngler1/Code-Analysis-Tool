"""Step 5.4 without Docker: mutation reports (real reports in tests/fixtures/testing/), the file sample,
the settings the tool writes for the mutation tools, and the score."""

import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from scheduler.config import Settings
from scheduler.linting.model import Status
from scheduler.testing.config import MutationConfig, TestsConfig
from scheduler.testing.context import Job, TestContext
from scheduler.testing.languages import java, python
from scheduler.testing.model import AFTER_STEP_2, CoverageRun, FileCoverage, Mutant, MutationRun, Suite
from scheduler.testing.mutation import parse_cargo_mutants, parse_pit, parse_stryker, select_files

FIXTURES = Path(__file__).parent / "fixtures" / "testing"
WORKSPACE = Path("/work/itest/run/step5-tests/workspace")  # where the reports were written (paths made neutral)


def states(mutants):
    counts: dict[str, int] = {}
    for mutant in mutants:
        counts[mutant.status] = counts.get(mutant.status, 0) + 1
    return counts


# --- reports ---


def test_stryker_net_report():
    mutants = parse_stryker(FIXTURES / "stryker-net.json", WORKSPACE)
    assert states(mutants) == {"Killed": 1, "Ignored": 2, "NoCoverage": 4}
    assert mutants[0] == Mutant("Calc/Calculator.cs", 5, "Arithmetic mutation", "a - b", "Killed")


def test_stryker_js_report():
    mutants = parse_stryker(FIXTURES / "stryker-js.json", WORKSPACE)  # file keys relative to projectRoot
    assert states(mutants) == {"Killed": 2, "NoCoverage": 7}
    assert {mutant.file for mutant in mutants} == {"src/calc.js"}


def test_stryker_report_outside_the_copy_is_left_out(tmp_path):
    assert parse_stryker(FIXTURES / "stryker-net.json", tmp_path) == []


def test_pit_report():
    mutants = parse_pit(FIXTURES / "pit.xml", ["src/main/java/calc/Calc.java"])
    assert states(mutants) == {"Killed": 2, "NoCoverage": 4}
    assert mutants[0] == Mutant("src/main/java/calc/Calc.java", 5, "MathMutator",
                                "Replaced integer addition with subtraction", "Killed")


def test_cargo_mutants_report():
    mutants = parse_cargo_mutants(FIXTURES / "cargo-mutants.json", WORKSPACE, WORKSPACE)
    assert states(mutants) == {"Killed": 5, "Survived": 7}  # the baseline is not a mutant
    assert Mutant("src/lib.rs", 13, "UnaryOperator", "src/lib.rs:13:9: delete - in sign", "Survived") in mutants


def test_mutmut_results_and_diffs(tmp_path):
    (tmp_path / "calc").mkdir()
    (tmp_path / "calc" / "__init__.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef sign(x):  # not tested\n    if x < 0:\n        return -1\n    return 1\n")
    output = ("    calc.x_add__mutmut_1: killed\n    calc.x_sign__mutmut_1: no tests\n"
              "=== code-analysis diffs\n=== calc.x_sign__mutmut_1\n# calc.x_sign__mutmut_1: no tests\n"
              "--- calc/__init__.py\n+++ calc/__init__.py\n@@ -1,4 +1,4 @@\n def sign(x):  # not tested\n"
              "-    if x < 0:\n+    if x <= 0:\n         return -1\n     return 1\n")
    assert python.parse_mutmut(output, tmp_path, tmp_path) == [
        Mutant("calc/__init__.py", None, "mutmut", "calc.x_add__mutmut_1", "Killed"),
        Mutant("calc/__init__.py", 6, "mutmut", "if x <= 0:", "NoCoverage", "if x < 0:"),
    ]


# --- what the tool writes for the tools ---


def test_pytest_node_ids(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text("")
    assert python.pytest_node(tmp_path, "tests.test_calc.test_x") == "tests/test_calc.py::test_x"
    assert python.pytest_node(tmp_path, "tests.test_calc.TestX.test_y[1.5]") == \
        "tests/test_calc.py::TestX::test_y[1.5]"
    assert python.pytest_node(tmp_path, "other.test_z") is None


def test_mutmut_settings_replace_the_projects_own(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text("[project]\nname = 'x'\n\n[tool.mutmut]\nsource_paths = ['everything/']\n\n[tool.other]\na = 1\n")
    python.write_mutmut_settings(pyproject, ["calc/__init__.py"], ["tests/test_calc.py::test_broken"])
    data = tomllib.loads(pyproject.read_text())
    assert data["tool"]["mutmut"] == {"source_paths": ["calc/__init__.py"],
                                      "pytest_add_cli_args_test_selection": ["--deselect",
                                                                             "tests/test_calc.py::test_broken"]}
    assert data["tool"]["other"] == {"a": 1} and data["project"]["name"] == "x"


def test_pitest_plugin_is_added_to_the_pom(tmp_path):
    pom = tmp_path / "pom.xml"
    pom.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"><build><plugins><plugin>'
                   "<groupId>org.pitest</groupId><artifactId>pitest-maven</artifactId><version>1.0</version>"
                   "</plugin></plugins></build></project>")
    java.add_pitest_plugin(pom, junit5=True)
    ns = "{http://maven.apache.org/POM/4.0.0}"
    plugins = ET.parse(pom).getroot().findall(f"{ns}build/{ns}plugins/{ns}plugin")
    assert len(plugins) == 1  # the project's own PIT plugin is replaced
    assert plugins[0].findtext(f"{ns}version") == java.PITEST_VERSION
    assert plugins[0].findtext(f"{ns}configuration/{ns}skipFailingTests") == "true"
    assert plugins[0].findtext(f"{ns}dependencies/{ns}dependency/{ns}artifactId") == "pitest-junit5-plugin"
    assert "ns0:" not in pom.read_text()  # the POM namespace stays the default namespace


def test_pit_target_classes_and_gradle_script(tmp_path):
    (tmp_path / "src" / "calc").mkdir(parents=True)
    (tmp_path / "src" / "calc" / "Calc.java").write_text("// x\npackage calc.core;\nclass Calc {}\n")
    classes = java.target_classes(tmp_path, ["src/calc/Calc.java"])
    assert classes == ["calc.core.Calc", "calc.core.Calc$*"]
    script = java.gradle_pitest_script(classes, junit5=False)
    assert 'targetClasses = ["calc.core.Calc", "calc.core.Calc\\$*"]' in script  # $ escaped for Groovy
    assert "junit5PluginVersion" not in script and "skipFailingTests = true" in script


# --- sample and score ---


def context(tmp_path, mutation: MutationConfig) -> TestContext:
    return TestContext(tmp_path, tmp_path / "step", None, None, Settings(None, None, "x"),
                       TestsConfig(mutation=mutation))


def test_sample_of_covered_files_is_reproducible(tmp_path):
    coverage = CoverageRun("Python (x)", "Python", "coverage.py", Status.OK,
                           files=[FileCoverage(f"m{index:02d}.py", 10, 0 if index == 0 else 5)
                                  for index in range(30)])
    job = Job(Suite("Python", ".", "pip", "x"), tmp_path, tmp_path / "out", AFTER_STEP_2)
    files, sampled, candidates, _ = select_files(context(tmp_path, MutationConfig(sample_files=5)), job, coverage)
    assert sampled and candidates == 29 and len(files) == 5 and "m00.py" not in files  # m00.py: no line covered
    assert select_files(context(tmp_path, MutationConfig(sample_files=5)), job, coverage)[0] == files
    assert select_files(context(tmp_path, MutationConfig(sample_files=5, seed=2)), job, coverage)[0] != files
    few = select_files(context(tmp_path, MutationConfig(sample_files=50)), job, coverage)
    assert few[1] is False and len(few[0]) == 29


def test_score_counts_detected_of_valid_mutants():
    run = MutationRun("x", "Python", "mutmut", Status.OK, mutants=[
        Mutant("a.py", 1, "m", "r", status) for status in
        ("Killed", "Killed", "Timeout", "Survived", "NoCoverage", "CompileError", "Ignored", "RuntimeError")])
    assert run.score == 0.6  # (2 Killed + 1 Timeout) / 5 valid
    assert MutationRun("x", "Python", "mutmut", Status.OK).score is None

"""Data model of step 5: the test suites of the project, their runs and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scheduler.linting.model import CommandResult, Status

OUTCOMES = ("passed", "failed", "error", "skipped")
# The two states the tests run on (5.2): the main result, and the comparison.
AFTER_STEP_2 = "after step 2"
HEAD = "HEAD"


@dataclass
class Suite:
    """The tests of one build unit, e.g. a Cargo workspace, a Maven build or a .NET test project.

    `test_files` contain only tests (they are left out of the coverage in 5.3).
    `inline_test_files` are source files that also contain tests (Rust's
    `#[cfg(test)]` modules); they stay in the coverage.
    """

    language: str  # "Python", "JavaScript/TypeScript", "Java", "C#", "Rust" or "C/C++"
    root: str  # folder relative to the project, "." for the project folder
    manager: str  # package manager or build tool, e.g. "Poetry", "npm", "Gradle", "NuGet", "Cargo", "CMake"
    manifest: str  # file the suite was found by, relative to the project
    tests_found: bool = False
    frameworks: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    inline_test_files: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)  # what was found: test files, configuration, packages
    notes: list[str] = field(default_factory=list)  # limits, e.g. tests that need a browser

    @property
    def label(self) -> str:
        return f"{self.language} ({self.manifest})"


@dataclass(frozen=True)
class TestCase:
    """One test and how it ended."""

    __test__ = False  # not a pytest test class

    id: str  # the same in both states, e.g. "tests.test_app.test_x" or "crate::module::test"
    outcome: str  # one of OUTCOMES
    duration_seconds: float | None = None
    message: str = ""  # failure message, shortened


@dataclass
class SuiteRun:
    """Running one suite's tests in one state (5.2)."""

    suite: str  # Suite.label
    language: str
    state: str  # AFTER_STEP_2 or HEAD
    tool: str  # e.g. "pytest", "cargo test", "dotnet test", "Maven", "Gradle", "Jest", "CTest"
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    tests: list[TestCase] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder

    @property
    def counts(self) -> dict[str, int]:
        counts = {outcome: 0 for outcome in OUTCOMES}
        for test in self.tests:
            counts[test.outcome] = counts.get(test.outcome, 0) + 1
        return counts


@dataclass(frozen=True)
class TestDifference:
    """A test that ended differently after step 2 than at HEAD ("missing": not run in that state)."""

    __test__ = False  # not a pytest test class

    id: str
    after_step_2: str
    head: str


@dataclass
class HeadComparison:
    """How the run after step 2 differs from the run at HEAD; empty when they agree.

    Single tests are compared only if both runs have results; otherwise the
    counts per outcome show the difference (e.g. no test ran after step 2).
    """

    suite: str
    status_after_step_2: str
    status_head: str
    counts_after_step_2: dict[str, int] = field(default_factory=dict)
    counts_head: dict[str, int] = field(default_factory=dict)
    tests: list[TestDifference] = field(default_factory=list)

    @property
    def differs(self) -> bool:
        return (self.status_after_step_2 != self.status_head or bool(self.tests)
                or self.counts_after_step_2 != self.counts_head)


@dataclass
class TestingResult:
    __test__ = False  # not a pytest test class

    run_dir: Path | None
    suites: list[Suite] = field(default_factory=list)
    # Languages of step 1 that have no suite, with the reason (e.g. no CMakeLists.txt).
    not_checked: dict[str, str] = field(default_factory=dict)
    runs: list[SuiteRun] = field(default_factory=list)  # after step 2: the result
    head_runs: list[SuiteRun] = field(default_factory=list)  # empty when step 2 changed nothing
    differences: list[HeadComparison] = field(default_factory=list)  # only those that differ
    network: bool = False  # the test runs had network
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and all(run.status is not Status.FAILED for run in self.runs)

    @property
    def without_tests(self) -> list[Suite]:
        return [suite for suite in self.suites if not suite.tests_found]

    @property
    def tests(self) -> list[TestCase]:
        return [test for run in self.runs for test in run.tests]

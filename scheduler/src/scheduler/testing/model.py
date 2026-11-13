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


@dataclass(frozen=True)
class FileCoverage:
    """Test coverage of one source file (5.3). Branches are None when the tool reports none."""

    file: str  # relative to the project
    lines_total: int
    lines_covered: int
    branches_total: int | None = None
    branches_covered: int | None = None
    uncovered_lines: str = ""  # line numbers not run by any test, as ranges: "3-5, 9"


@dataclass
class CoverageRun:
    """Measuring the coverage of one suite's tests (5.3), on the state after step 2."""

    suite: str  # Suite.label
    language: str
    tool: str  # e.g. "coverage.py", "JaCoCo", "coverlet", "cargo-llvm-cov", "gcovr"
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    files: list[FileCoverage] = field(default_factory=list)  # source files with coverage data
    not_loaded: list[str] = field(default_factory=list)  # source files without data: no test loaded them
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder

    @property
    def totals(self) -> dict[str, int | None]:
        branches = [file for file in self.files if file.branches_total is not None]
        return {
            "files": len(self.files),
            "files_not_loaded": len(self.not_loaded),
            "lines_total": sum(file.lines_total for file in self.files),
            "lines_covered": sum(file.lines_covered for file in self.files),
            "branches_total": sum(file.branches_total or 0 for file in branches) if branches else None,
            "branches_covered": sum(file.branches_covered or 0 for file in branches) if branches else None,
        }


# States of the mutation-testing-elements schema (Stryker), used for every tool.
MUTANT_STATES = ("Killed", "Survived", "NoCoverage", "Timeout", "CompileError", "RuntimeError", "Ignored", "Pending")


@dataclass(frozen=True)
class Mutant:
    """One mutant of 5.4 and how the tests dealt with it."""

    file: str  # relative to the project
    line: int | None
    mutator: str  # the tool's name for the kind of change
    replacement: str  # the tool's description or the new code
    status: str  # one of MUTANT_STATES
    original: str = ""  # the source line, for mutants the tests did not detect


@dataclass
class MutationRun:
    """Mutation testing of one suite (5.4), on the state after step 2."""

    suite: str  # Suite.label
    language: str
    tool: str  # e.g. "mutmut", "StrykerJS", "PIT", "Stryker.NET", "cargo-mutants"
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    mode: str = "sample"  # from config/tests.toml
    files: list[str] = field(default_factory=list)  # the source files mutated
    sampled: bool = False  # True if `files` is a sample of the candidates
    candidates: int = 0  # number of files the sample was drawn from
    excluded_tests: list[str] = field(default_factory=list)  # tests failing in 5.2, left out
    mutants: list[Mutant] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder

    @property
    def counts(self) -> dict[str, int]:
        counts = {state: 0 for state in MUTANT_STATES}
        for mutant in self.mutants:
            counts[mutant.status] = counts.get(mutant.status, 0) + 1
        return counts

    @property
    def score(self) -> float | None:
        """detected / valid, as Stryker defines it: (Killed + Timeout) / (Killed + Timeout + Survived + NoCoverage)."""
        counts = self.counts
        detected = counts["Killed"] + counts["Timeout"]
        valid = detected + counts["Survived"] + counts["NoCoverage"]
        return round(detected / valid, 4) if valid else None


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
    coverage: list[CoverageRun] = field(default_factory=list)  # after step 2
    mutation: list[MutationRun] = field(default_factory=list)  # after step 2
    network: bool = False  # the test runs had network
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        runs = [*self.runs, *self.coverage, *self.mutation]
        return not self.errors and all(run.status is not Status.FAILED for run in runs)

    @property
    def without_tests(self) -> list[Suite]:
        return [suite for suite in self.suites if not suite.tests_found]

    @property
    def tests(self) -> list[TestCase]:
        return [test for run in self.runs for test in run.tests]

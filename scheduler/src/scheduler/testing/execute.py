"""5.2 to 5.4: run the tests of each suite, after step 2 and at HEAD, and compare the two;
measure the coverage of the tests and test them with mutants, after step 2.

Each suite runs in two phases: an online phase downloads the dependencies,
then the tests run without network (unless config/tests.toml allows it).
Suites run one after another: the test tools use all processor cores
themselves, and parallel runs could make timing-sensitive tests fail.

The coverage runs right after the tests, in the same copy (dependencies and
builds are there), as a run of its own: the instrumentation must not change
the test results of 5.2. Mutation testing follows in the same copy.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import traceback
from types import ModuleType

from scheduler.linting.model import Status
from scheduler.linting.session import slug
from scheduler.testing.context import Job, TestContext
from scheduler.testing.languages import cpp, csharp, java, javascript, python, rust
from scheduler.testing.model import (
    AFTER_STEP_2,
    HEAD,
    CoverageRun,
    HeadComparison,
    MutationRun,
    Suite,
    SuiteRun,
    TestDifference,
    TestingResult,
)
from scheduler.testing.mutation import failing_tests, select_files
from scheduler.workspace import WorkspaceError, prepare_head_workspace, prepare_workspace

LANGUAGE_MODULES: dict[str, ModuleType] = {
    "Python": python,
    "JavaScript/TypeScript": javascript,
    "Java": java,
    "C#": csharp,
    "Rust": rust,
    "C/C++": cpp,
}
MISSING = "missing"  # a test that did not run in one of the states


def images(suites: list[Suite]) -> list[str]:
    return sorted({LANGUAGE_MODULES[suite.language].IMAGE for suite in suites})


def run_suites(ctx: TestContext, suites: list[Suite], result: TestingResult) -> None:
    """Run the suites (all of them have tests) in both states; fills result.runs, head_runs, differences."""
    workspace = ctx.step_dir / "workspace"
    head_workspace = ctx.step_dir / "workspace-head"
    compare = changed_since_head(ctx)
    try:
        prepare_workspace(ctx.project, workspace)
        if compare:
            prepare_head_workspace(ctx.project, head_workspace)
        for index, suite in enumerate(suites, start=1):
            name = slug(suite.root) if suite.root != "." else "root"
            folder = ctx.step_dir / f"{index:02d}-{slug(suite.language)}-{name}"
            run = _run(ctx, Job(suite, workspace, folder / "run", AFTER_STEP_2))
            result.runs.append(run)
            ctx.out(f"  {suite.label:<40} {describe(run)}")
            for note in run.notes:
                ctx.out(f"  {'':<40}   note: {note}")
            coverage = _coverage(ctx, Job(suite, workspace, folder / "coverage", AFTER_STEP_2), run)
            result.coverage.append(coverage)
            ctx.out(f"  {suite.label:<40} coverage: {describe_coverage(coverage)}")
            for note in coverage.notes:
                ctx.out(f"  {'':<40}   note: {note}")
            mutation = _mutation(ctx, Job(suite, workspace, folder / "mutation", AFTER_STEP_2), run, coverage)
            result.mutation.append(mutation)
            ctx.out(f"  {suite.label:<40} mutation testing: {describe_mutation(mutation)}")
            for note in mutation.notes:
                ctx.out(f"  {'':<40}   note: {note}")
            if compare:
                head = _run(ctx, Job(suite, head_workspace, folder / "run-head", HEAD))
                result.head_runs.append(head)
                comparison = compare_runs(run, head)
                if comparison.differs:
                    result.differences.append(comparison)
    except WorkspaceError as exc:
        result.errors.append(f"the copy of the project could not be created: {exc}")
    finally:
        for folder in (workspace, head_workspace, ctx.cache_dir, ctx.step_dir / "build"):
            shutil.rmtree(folder, ignore_errors=True)


def changed_since_head(ctx: TestContext) -> bool:
    """True if the working tree differs from HEAD, i.e. step 2 changed or added files."""
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(ctx.project), "status", "--porcelain", "--untracked-files=all",
         "--", "."],
        capture_output=True, text=True, timeout=600, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _run(ctx: TestContext, job: Job) -> SuiteRun:
    started = time.monotonic()
    state = "" if job.state == AFTER_STEP_2 else " at HEAD (comparison)"
    ctx.out(f"  {job.suite.label:<40} running{state} ...")
    try:
        run = LANGUAGE_MODULES[job.suite.language].run_suite(ctx, job)
    except Exception as exc:  # noqa: BLE001 – one broken suite must not stop the others
        job.out_dir.mkdir(parents=True, exist_ok=True)
        (job.out_dir / "internal-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        run = SuiteRun(job.suite.label, job.suite.language, job.state, "-", Status.FAILED,
                       notes=[f"internal error: {exc}"],
                       output_dir=job.out_dir.relative_to(ctx.step_dir).as_posix())
    run.duration_seconds = round(time.monotonic() - started, 1)
    return run


def _coverage(ctx: TestContext, job: Job, run: SuiteRun) -> CoverageRun:
    if run.status not in (Status.OK, Status.PARTIAL) or not run.tests:
        return CoverageRun(job.suite.label, job.suite.language, "-", Status.SKIPPED,
                           notes=["not measured: the tests did not run"])
    started = time.monotonic()
    ctx.out(f"  {job.suite.label:<40} measuring the coverage ...")
    try:
        coverage = LANGUAGE_MODULES[job.suite.language].coverage_suite(ctx, job)
    except Exception as exc:  # noqa: BLE001 – one broken suite must not stop the others
        job.out_dir.mkdir(parents=True, exist_ok=True)
        (job.out_dir / "internal-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        coverage = CoverageRun(job.suite.label, job.suite.language, "-", Status.FAILED,
                               notes=[f"internal error: {exc}"],
                               output_dir=job.out_dir.relative_to(ctx.step_dir).as_posix())
    coverage.duration_seconds = round(time.monotonic() - started, 1)
    return coverage


def _mutation(ctx: TestContext, job: Job, run: SuiteRun, coverage: CoverageRun) -> MutationRun:
    def skipped(note: str) -> MutationRun:
        return MutationRun(job.suite.label, job.suite.language, "-", Status.SKIPPED, mode=ctx.config.mutation.mode,
                           notes=[note])

    if run.status not in (Status.OK, Status.PARTIAL) or not run.tests:
        return skipped("not run: the tests did not run")
    files, sampled, candidates, notes = select_files(ctx, job, coverage)
    if not files:
        return skipped("no source file to mutate")
    started = time.monotonic()
    mode = "a sample of " if sampled else ""
    ctx.out(f"  {job.suite.label:<40} mutation testing of {mode}{len(files)} files ...")
    try:
        mutation = LANGUAGE_MODULES[job.suite.language].mutation_suite(ctx, job, files, failing_tests(run))
    except Exception as exc:  # noqa: BLE001 – one broken suite must not stop the others
        job.out_dir.mkdir(parents=True, exist_ok=True)
        (job.out_dir / "internal-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        mutation = MutationRun(job.suite.label, job.suite.language, "-", Status.FAILED, mode=ctx.config.mutation.mode,
                               notes=[f"internal error: {exc}"],
                               output_dir=job.out_dir.relative_to(ctx.step_dir).as_posix())
    mutation.files = mutation.files or files
    mutation.sampled, mutation.candidates = sampled, candidates
    mutation.notes = notes + mutation.notes
    mutation.duration_seconds = round(time.monotonic() - started, 1)
    return mutation


def describe_mutation(mutation: MutationRun) -> str:
    parts = [mutation.status.value]
    if mutation.mutants:
        counts = mutation.counts
        score = mutation.score
        parts.append(f"{len(mutation.mutants)} mutants in {len(mutation.files)}"
                     + (f" of {mutation.candidates}" if mutation.sampled else "") + " files")
        parts.append(", ".join(f"{count} {state}" for state, count in counts.items() if count))
        if score is not None:
            parts.append(f"score {100 * score:.1f}%")
    if mutation.duration_seconds:
        parts.append(f"{mutation.duration_seconds:.0f} s")
    return ", ".join(parts)


def describe_coverage(coverage: CoverageRun) -> str:
    parts = [coverage.status.value]
    totals = coverage.totals
    if coverage.files or coverage.not_loaded:
        parts.append(f"lines {percent(totals['lines_covered'], totals['lines_total'])}")
        if totals["branches_total"] is not None:
            parts.append(f"branches {percent(totals['branches_covered'], totals['branches_total'])}")
        if coverage.not_loaded:
            parts.append(f"{len(coverage.not_loaded)} of {len(coverage.files) + len(coverage.not_loaded)} source files "
                         "not loaded by any test")
    if coverage.duration_seconds:
        parts.append(f"{coverage.duration_seconds:.0f} s")
    return ", ".join(parts)


def percent(covered: int | None, total: int | None) -> str:
    if not total:
        return "-"
    return f"{100 * (covered or 0) / total:.1f}% ({covered}/{total})"


def describe(run: SuiteRun) -> str:
    parts = [run.status.value]
    if run.tests:
        counts = run.counts
        parts.append(f"{len(run.tests)} tests: " + ", ".join(f"{count} {outcome}" for outcome, count in counts.items()
                                                             if count))
    parts.append(f"{run.duration_seconds:.0f} s")
    return ", ".join(parts)


def compare_runs(after: SuiteRun, head: SuiteRun) -> HeadComparison:
    """Tests whose outcome differs between the two states (or that ran in only one of them).

    If one state has no test results at all, the counts show the difference
    instead of listing every test as missing.
    """
    comparison = HeadComparison(after.suite, after.status.value, head.status.value, after.counts, head.counts)
    if after.tests and head.tests:
        outcomes_after = {test.id: test.outcome for test in after.tests}
        outcomes_head = {test.id: test.outcome for test in head.tests}
        comparison.tests = [
            TestDifference(test_id, outcomes_after.get(test_id, MISSING), outcomes_head.get(test_id, MISSING))
            for test_id in sorted(outcomes_after.keys() | outcomes_head.keys())
            if outcomes_after.get(test_id) != outcomes_head.get(test_id)]
    return comparison

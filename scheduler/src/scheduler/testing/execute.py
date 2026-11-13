"""5.2: run the tests of each suite, after step 2 and at HEAD, and compare the two.

Each suite runs in two phases: an online phase downloads the dependencies,
then the tests run without network (unless config/tests.toml allows it).
Suites run one after another: the test tools use all processor cores
themselves, and parallel runs could make timing-sensitive tests fail.
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
from scheduler.testing.model import AFTER_STEP_2, HEAD, HeadComparison, Suite, SuiteRun, TestDifference, TestingResult
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

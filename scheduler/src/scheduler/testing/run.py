"""Step 5 of the workflow: the project's tests.

5.1 finds the test suites without running anything: which build units have
tests, with which frameworks, in which files. A language without tests is
noted in the report.
5.2 runs the tests of each suite in a container of its language's test image,
on a copy of the project as step 2 left it. If step 2 changed the project,
they also run on a copy of the last commit (HEAD), and the results are
compared.
5.3 measures the test coverage of each suite after step 2, with one tool per
language.
5.4 tests the tests with mutants (one tool per language, a sample of the
covered files or all files, with a time limit). Results go into the run folder:

<OUTPUT_DIR>/<project>/<date_time>/
  step5-tests/
    summary.json      the suites, the test runs and their counts, differences to HEAD (if any)
    tests.json        every test after step 2 and how it ended
    coverage.json     coverage per source file, and the source files no test loaded
    mutants.json      every mutant: file, line, mutator, change, state; the source line of undetected ones
    <nn>-<language>-<folder>/run/       raw output of the run (logs, reports)
    <nn>-<language>-<folder>/run-head/  the same for the run at HEAD
    <nn>-<language>-<folder>/coverage/  raw output of the coverage run
    <nn>-<language>-<folder>/mutation/  raw output of the mutation run
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.linting.docker import DockerError, DockerRunner, ToolRunner
from scheduler.testing import execute
from scheduler.testing.config import ConfigError, TestsConfig, load_config
from scheduler.testing.context import TestContext
from scheduler.testing.detect import LANGUAGE_GROUPS, detect_suites
from scheduler.testing.model import CoverageRun, MutationRun, Suite, SuiteRun, TestingResult

STEP_DIR = "step5-tests"


def run_tests(
    acceptance: AcceptanceResult,
    settings: Settings,
    run_dir: Path,
    *,
    runner: ToolRunner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out: Callable[[str], None] = print,
) -> TestingResult:
    started = now()
    step_dir = run_dir / STEP_DIR
    step_dir.mkdir(parents=True, exist_ok=True)
    result = TestingResult(run_dir)
    config: TestsConfig | None = None
    try:
        result.suites, result.not_checked = detect_suites(acceptance)
    except Exception as exc:  # noqa: BLE001 – reported like a failed tool, the run goes on
        (step_dir / "internal-error-detection.txt").write_text(traceback.format_exc(), encoding="utf-8")
        result.errors.append(f"test detection failed: {exc}")
    for suite in result.suites:
        out(f"  {suite.label:<40} {describe(suite)}")
        for note in suite.notes:
            out(f"  {'':<40}   note: {note}")
    for language, reason in result.not_checked.items():
        out(f"  {language:<40} not checked: {reason}")

    tested = [suite for suite in result.suites if suite.tests_found]
    if tested:
        try:
            config = load_config(settings.config_dir)
            result.network = config.network
            runner = runner or DockerRunner(settings.tools_network)
            runner.check_available()
            for image in execute.images(tested):
                runner.ensure_image(image)
        except (ConfigError, DockerError) as exc:
            result.errors.append(str(exc))
        else:
            out("\n  Running the tests " + ("WITH network (config/tests.toml: network = true)"
                                          if config.network else "without network") + " ...")
            ctx = TestContext(acceptance.project, step_dir, acceptance, runner, settings, config, out, result.suites)
            execute.run_suites(ctx, tested, result)
    _write_results(result, acceptance, config, started, now())
    return result


def describe(suite: Suite) -> str:
    if not suite.tests_found:
        return "no tests found"
    files = len(suite.test_files) + len(suite.inline_test_files)
    parts = [", ".join(suite.frameworks) or "tests"]
    if files:
        parts.append(f"{files} file{'' if files == 1 else 's'} with tests")
    return ", ".join(parts)


def languages_without_tests(result: TestingResult) -> list[str]:
    """Languages with suites, none of which has tests ("Tests present? – no")."""
    checked = [group for group in LANGUAGE_GROUPS if any(suite.language == group for suite in result.suites)]
    return [group for group in checked
            if not any(suite.tests_found for suite in result.suites if suite.language == group)]


def _write_results(result: TestingResult, acceptance: AcceptanceResult, config: TestsConfig | None,
                   started: datetime, finished: datetime) -> None:
    assert result.run_dir is not None
    summary: dict[str, object] = {
        "step": "5 – tests",
        "project": str(acceptance.project),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "complete": result.complete,
        "errors": result.errors,
        "detection": {
            "languages_without_tests": languages_without_tests(result),
            "languages_not_checked": result.not_checked,
            "suites": [asdict(suite) for suite in result.suites],
        },
    }
    if config is not None:
        tests: dict[str, object] = {
            "network": config.network,
            "time_limit_minutes": config.time_limit_minutes,
            "runs": [_run_summary(run) for run in result.runs],
        }
        if result.differences:  # only mentioned when the states differ
            tests["differences_to_head"] = [asdict(comparison) for comparison in result.differences]
        summary["tests"] = tests
        summary["coverage"] = [_coverage_summary(coverage) for coverage in result.coverage]
        summary["mutation"] = {
            "mode": config.mutation.mode,
            "sample_files": config.mutation.sample_files,
            "seed": config.mutation.seed,
            "time_limit_minutes": config.mutation.time_limit_minutes,
            "runs": [_mutation_summary(mutation) for mutation in result.mutation],
        }
    step_dir = result.run_dir / STEP_DIR
    _write_json(step_dir / "summary.json", summary)
    if config is not None:
        _write_json(step_dir / "tests.json", [{"suite": run.suite, **asdict(test)}
                                              for run in result.runs for test in run.tests])
        _write_json(step_dir / "coverage.json", [
            {"suite": coverage.suite, "files": [asdict(file) for file in coverage.files],
             "not_loaded": coverage.not_loaded}
            for coverage in result.coverage if coverage.files or coverage.not_loaded])
        _write_json(step_dir / "mutants.json", [{"suite": mutation.suite, **asdict(mutant)}
                                                for mutation in result.mutation for mutant in mutation.mutants])


def _run_summary(run: SuiteRun) -> dict:
    return {
        "suite": run.suite,
        "language": run.language,
        "tool": run.tool,
        "version": run.version,
        "status": run.status.value,
        "duration_seconds": run.duration_seconds,
        "tests": len(run.tests),
        "counts": run.counts,
        "failed_tests": [test.id for test in run.tests if test.outcome in ("failed", "error")],
        "commands": [
            {
                "command": list(command.command),
                "exit_code": command.exit_code,
                "duration_seconds": round(command.duration_seconds, 1),
                "timed_out": command.timed_out,
            }
            for command in run.commands
        ],
        "notes": run.notes,
        "output_dir": run.output_dir,
    }


def _coverage_summary(coverage: CoverageRun) -> dict:
    totals = coverage.totals
    return {
        "suite": coverage.suite,
        "language": coverage.language,
        "tool": coverage.tool,
        "version": coverage.version,
        "status": coverage.status.value,
        "duration_seconds": coverage.duration_seconds,
        **totals,
        "line_rate": round(totals["lines_covered"] / totals["lines_total"], 4) if totals["lines_total"] else None,
        "branch_rate": (round(totals["branches_covered"] / totals["branches_total"], 4)
                        if totals["branches_total"] else None),
        "commands": [
            {"command": list(command.command), "exit_code": command.exit_code,
             "duration_seconds": round(command.duration_seconds, 1), "timed_out": command.timed_out}
            for command in coverage.commands
        ],
        "notes": coverage.notes,
        "output_dir": coverage.output_dir,
    }


def _mutation_summary(mutation: MutationRun) -> dict:
    return {
        "suite": mutation.suite,
        "language": mutation.language,
        "tool": mutation.tool,
        "version": mutation.version,
        "status": mutation.status.value,
        "duration_seconds": mutation.duration_seconds,
        "files": mutation.files,
        "sampled": mutation.sampled,
        "candidate_files": mutation.candidates,
        "excluded_tests": mutation.excluded_tests,
        "mutants": len(mutation.mutants),
        "counts": mutation.counts,
        "score": mutation.score,
        "commands": [
            {"command": list(command.command), "exit_code": command.exit_code,
             "duration_seconds": round(command.duration_seconds, 1), "timed_out": command.timed_out}
            for command in mutation.commands
        ],
        "notes": mutation.notes,
        "output_dir": mutation.output_dir,
    }


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

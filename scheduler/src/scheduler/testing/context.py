"""What the language modules of step 5 share: Docker, the step folder, the settings."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.linting.docker import ToolRunner
from scheduler.linting.model import CommandResult, Status
from scheduler.linting.session import last_lines
from scheduler.testing.config import TestsConfig
from scheduler.testing.model import Suite, SuiteRun, TestCase

# Downloading dependencies (online phase); the test runs use the configured limit.
PREPARE_TIMEOUT_SECONDS = 60 * 60


@dataclass
class TestContext:
    __test__ = False  # not a pytest test class

    project: Path
    step_dir: Path  # <run>/step5-tests; the only folder mounted into the containers
    acceptance: AcceptanceResult
    runner: ToolRunner
    settings: Settings
    config: TestsConfig
    out: Callable[[str], None] = print

    @property
    def cache_dir(self) -> Path:
        """Package caches, shared by the online and the offline phase and by both states."""
        return self.step_dir / "cache"

    def run(self, run: SuiteRun, image: str, command: Sequence[str], *, workdir: Path, log_dir: Path, label: str,
            online: bool, env: Mapping[str, str] | None = None) -> CommandResult:
        """One command in a fresh container; its output goes to <log_dir>/<label>.std{out,err}.txt.

        online: downloading dependencies, always with network and the
        download time limit. Otherwise a test run: with network only if
        config/tests.toml allows it, and with its time limit.
        """
        network = self.settings.tools_network if online or self.config.network else "none"
        timeout = PREPARE_TIMEOUT_SECONDS if online else self.config.time_limit_seconds
        result = self.runner.run(image, command, workdir=workdir, mounts=[self.step_dir], env=env,
                                 timeout=timeout, network=network)
        run.commands.append(result)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{label}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (log_dir / f"{label}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        return result


@dataclass
class Job:
    """One suite in one state: where its copy is and where its raw output goes."""

    suite: Suite
    workspace: Path  # the copy of the project
    out_dir: Path  # raw output of this run
    state: str

    @property
    def folder(self) -> Path:
        return self.workspace / self.suite.root


def new_run(ctx: TestContext, job: Job, tool: str) -> SuiteRun:
    job.out_dir.mkdir(parents=True, exist_ok=True)
    return SuiteRun(suite=job.suite.label, language=job.suite.language, state=job.state, tool=tool,
                    status=Status.FAILED, output_dir=job.out_dir.relative_to(ctx.step_dir).as_posix())


def reason(result: CommandResult) -> str:
    if result.timed_out:
        return "time limit reached"
    if result.exit_code is None:
        return "could not start"
    return f"exit code {result.exit_code}: {last_lines(result.stdout + result.stderr)}"


def note_prepare(run: SuiteRun, result: CommandResult, what: str) -> bool:
    """Note a failed online phase; the test run is tried anyway. True if it worked."""
    if result.exit_code == 0:
        return True
    run.notes.append(f"{what} failed ({reason(result)})")
    return False


def judge(ctx: TestContext, run: SuiteRun, result: CommandResult, tests: list[TestCase], *, completed: bool,
          what: str = "the test run") -> SuiteRun:
    """Status of a test run from its command and the tests read from its report.

    completed: the command ended the way it does when all tests ran (failing
    tests included). Failing tests are a result, not a failure of the run.
    """
    run.tests = tests
    if result.timed_out:
        minutes = ctx.config.time_limit_minutes
        run.notes.append(f"time limit of {minutes} minutes reached; the results are incomplete")
        run.status = Status.PARTIAL if tests else Status.FAILED
    elif completed:
        run.status = Status.OK
        if not tests:
            run.notes.append("no tests were run")
    elif tests:
        run.status = Status.PARTIAL
        run.notes.append(f"{what} did not complete ({reason(result)})")
    else:
        run.status = Status.FAILED
        run.notes.append(f"{what} failed ({reason(result)})")
    return run

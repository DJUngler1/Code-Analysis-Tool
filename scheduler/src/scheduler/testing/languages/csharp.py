"""C#: `dotnet restore` (online), then `dotnet test` offline with a TRX report per target framework."""

from __future__ import annotations

import json
from pathlib import Path

from scheduler.linting.model import Status
from scheduler.testing.context import Job, TestContext, judge, new_run, note_prepare
from scheduler.testing.model import SuiteRun
from scheduler.testing.reports import ReportError, parse_trx

IMAGE = "code-analysis/test-dotnet:latest"
MTP = "Microsoft.Testing.Platform"


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "dotnet test")
    run.version = ctx.runner.image_versions(IMAGE).get("dotnet")
    project = job.workspace / job.suite.manifest
    env = {"NUGET_PACKAGES": str(ctx.cache_dir / "nuget")}
    restored = ctx.run(run, IMAGE, ["dotnet", "restore", str(project)], workdir=project.parent,
                       log_dir=job.out_dir, label="restore", online=True, env=env)
    note_prepare(run, restored, "dotnet restore")

    if uses_testing_platform(project.parent, job.workspace):
        # dotnet test in Microsoft.Testing.Platform mode takes other options;
        # a TRX report needs an extension package the project may not have.
        result = ctx.run(run, IMAGE, ["dotnet", "test", "--project", str(project), "--no-restore"],
                         workdir=project.parent, log_dir=job.out_dir, label="dotnet-test", online=False, env=env)
        run.notes.append("Microsoft.Testing.Platform mode (global.json): only the exit code is recorded")
        judge(ctx, run, result, [], completed=False, what="dotnet test")
        if result.exit_code in (0, 2):  # 2: at least one test failed
            run.status = Status.PARTIAL
        return run

    results = job.out_dir / "trx"
    result = ctx.run(run, IMAGE, ["dotnet", "test", str(project), "--no-restore", "--nologo",
                                  "--logger", "trx;LogFilePrefix=results", "--results-directory", str(results)],
                     workdir=project.parent, log_dir=job.out_dir, label="dotnet-test", online=False, env=env)
    reports = sorted(results.glob("*.trx"))
    tests = []
    try:
        tests = parse_trx(reports)
    except ReportError as exc:
        run.notes.append(f"a TRX report could not be read: {exc}")
    # dotnet test (VSTest): 0 all passed, 1 a test failed or the build failed.
    return judge(ctx, run, result, tests, completed=result.exit_code in (0, 1) and bool(reports),
                 what="dotnet test")


def uses_testing_platform(folder: Path, workspace: Path) -> bool:
    """True if the nearest global.json selects Microsoft.Testing.Platform for dotnet test."""
    current = folder
    while True:
        candidate = current / "global.json"
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                return False
            test = data.get("test") if isinstance(data, dict) else None
            return isinstance(test, dict) and test.get("runner") == MTP
        if current == workspace or current == current.parent:
            return False
        current = current.parent

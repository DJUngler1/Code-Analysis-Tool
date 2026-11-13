"""C#: `dotnet restore` (online), then `dotnet test` offline with a TRX report per target framework.

Coverage: coverlet's VSTest collector ("XPlat Code Coverage"). A test project
without it gets coverlet.collector added in the copy (online). The coverage
run uses the newest .NET runtime of the image (DOTNET_ROLL_FORWARD=LatestMajor):
coverlet's collector runs on the SDK's runtime and receives no hits from a
test host on an older one (net8.0: "Failed to unload module"). Microsoft Code
Coverage (dotnet-coverage, --collect "Code Coverage") returned empty reports
in the tests.

Mutation testing: Stryker.NET in the test project's folder, on the selected
files of the project under test; tests that failed in 5.2 are left out with
--test-case-filter.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from scheduler.linting.model import Status
from scheduler.testing.context import Job, TestContext, finish_coverage, judge, new_coverage, new_run, note_prepare
from scheduler.testing.coverage_reports import parse_cobertura_files
from scheduler.testing.model import CoverageRun, MutationRun, SuiteRun
from scheduler.testing.mutation import finish_mutation, new_mutation, parse_stryker, skip
from scheduler.testing.reports import ReportError, parse_trx

IMAGE = "code-analysis/test-dotnet:latest"
COVERLET_VERSION = "10.1.0"
_PROJECT_REFERENCE = re.compile(r'<ProjectReference\b[^>]*\bInclude\s*=\s*"([^"]+)"', re.IGNORECASE)
_PARAMETERS = re.compile(r"\(.*\)$| \[[^\]]+\]$")
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


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    coverage = new_coverage(ctx, job, "coverlet (XPlat Code Coverage)")
    project = job.workspace / job.suite.manifest
    if uses_testing_platform(project.parent, job.workspace):
        coverage.status = Status.SKIPPED
        coverage.notes.append("not measured in Microsoft.Testing.Platform mode: coverlet's collector needs VSTest")
        return coverage
    env = {"NUGET_PACKAGES": str(ctx.cache_dir / "nuget")}
    own = re.search(r'Include\s*=\s*"coverlet\.collector"', project.read_text(encoding="utf-8", errors="replace"),
                    re.IGNORECASE)
    if own:
        coverage.version = "coverlet.collector (the project's version)"
    else:
        coverage.version = f"coverlet.collector {COVERLET_VERSION}"
        added = ctx.run(coverage, IMAGE, ["dotnet", "add", str(project), "package", "coverlet.collector",
                                          "--version", COVERLET_VERSION],
                        workdir=project.parent, log_dir=job.out_dir, label="add-coverlet", online=True, env=env)
        note_prepare(coverage, added, f"adding coverlet.collector {COVERLET_VERSION}")
    results = job.out_dir / "results"
    result = ctx.run(coverage, IMAGE, ["dotnet", "test", str(project), "--no-restore", "--nologo",
                                       "--collect", "XPlat Code Coverage", "--results-directory", str(results)],
                     workdir=project.parent, log_dir=job.out_dir, label="coverage", online=False,
                     env={**env, "DOTNET_ROLL_FORWARD": "LatestMajor"})
    coverage.notes.append("measured on the newest .NET runtime of the image (DOTNET_ROLL_FORWARD=LatestMajor)")
    reports = sorted(results.glob("*/coverage.cobertura.xml"))
    files = None
    if reports:
        try:
            files = parse_cobertura_files(reports, job.workspace)
        except ReportError as exc:
            coverage.notes.append(f"a coverage report could not be read: {exc}")
    return finish_coverage(ctx, coverage, job, files, result, what="coverlet")


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    mutation = new_mutation(ctx, job, "Stryker.NET")
    mutation.version = ctx.runner.image_versions(IMAGE).get("dotnet-stryker")
    project = job.workspace / job.suite.manifest
    references = [(project.parent / reference.replace("\\", "/")).resolve()
                  for reference in _PROJECT_REFERENCE.findall(project.read_text(encoding="utf-8", errors="replace"))]
    by_project: dict[Path, list[str]] = {}
    for file in files:
        owner = next((reference for reference in references
                      if (job.workspace / file).resolve().is_relative_to(reference.parent)), None)
        if owner is not None:
            by_project.setdefault(owner, []).append(file)
    if not by_project:
        return skip(mutation, "none of the selected files belongs to a project the test project references")
    target, selected = max(by_project.items(), key=lambda item: len(item[1]))
    if len(by_project) > 1:
        mutation.notes.append(f"only {target.name} was mutated; the test project references several projects")
    mutation.files = sorted(selected)
    command = ["dotnet", "stryker", "--output", str(job.out_dir), "--reporter", "json", "--reporter", "progress"]
    if len(references) > 1:
        command += ["--project", target.name]
    for file in mutation.files:
        command += ["--mutate", "**/" + (job.workspace / file).resolve().relative_to(target.parent).as_posix()]
    names = sorted({_PARAMETERS.sub("", _PARAMETERS.sub("", test)) for test in failing})
    if names:
        command += ["--test-case-filter", "&".join(f"FullyQualifiedName!={name}" for name in names)]
        mutation.excluded_tests = names
    result = ctx.run(mutation, IMAGE, command, workdir=project.parent, log_dir=job.out_dir, label="stryker",
                     online=False, env={"NUGET_PACKAGES": str(ctx.cache_dir / "nuget")}, timeout="mutation")
    report = job.out_dir / "reports" / "mutation-report.json"
    mutants = None
    if report.is_file():
        try:
            mutants = parse_stryker(report, job.workspace)
        except ReportError as exc:
            mutation.notes.append(f"the Stryker report could not be read: {exc}")
    return finish_mutation(ctx, mutation, job, mutants, result, what="Stryker.NET")


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

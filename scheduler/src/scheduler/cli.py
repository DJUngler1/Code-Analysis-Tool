"""Entry point: ask for the project, check it (step 1), run linters and formatters (step 2),
run the static code analysis (step 3), check the dependencies (step 4) and the tests (step 5)."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from scheduler.acceptance import AcceptanceResult, run_acceptance
from scheduler.config import Settings
from scheduler.dependencies import DependencyResult, run_dependencies
from scheduler.dependencies.run import STEP_DIR as DEPENDENCIES_STEP_DIR
from scheduler.dependencies.run import scope_counts
from scheduler.linting import LintingResult, Status, run_linting
from scheduler.ollama import OllamaStatus, check_ollama
from scheduler.paths import PathError, resolve_project_path
from scheduler.static_analysis import AnalysisResult, run_static_analysis
from scheduler.static_analysis.run import STEP_DIR as ANALYSIS_STEP_DIR
from scheduler.testing import TestingResult, run_tests
from scheduler.testing.run import STEP_DIR as TESTS_STEP_DIR
from scheduler.testing.run import languages_without_tests

EXIT_INVALID_INPUT = 1
# Step 2 could not run completely: OUTPUT_DIR, Docker or a tool image missing,
# a tool failed, or project files could not be moved back.
EXIT_LINTING_INCOMPLETE = 5
# Step 3 could not run completely: rule set configuration invalid, Docker or
# the OpenGrep image missing or outdated, or OpenGrep failed.
EXIT_ANALYSIS_INCOMPLETE = 6
# Step 4 could not run completely: Docker or a tool image missing, a lock file
# command or a dependency check failed.
EXIT_DEPENDENCIES_INCOMPLETE = 7
# Step 5 could not run completely: config/tests.toml invalid, Docker or a test image missing,
# or the tests of a suite could not be run.
EXIT_TESTS_INCOMPLETE = 8
MAX_LISTED_MANIFESTS = 5
MAX_LISTED_MODELS = 5
MAX_LISTED_DIFFERENCES = 10


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-analysis", description="Code-Analysis-Tool")
    parser.add_argument("--path", help="project to analyze (skips the interactive prompt)")
    parser.add_argument(
        "--check-only", action="store_true", help="only run the acceptance check (step 1), change nothing"
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    try:
        return _run(args.path, settings, check_only=args.check_only)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_INVALID_INPUT


def _run(path_arg: str | None, settings: Settings, *, check_only: bool = False) -> int:
    print("Code-Analysis-Tool\n")
    print(describe_ollama(check_ollama(settings.ollama_base_url)))
    if settings.projects_dir is not None:
        print(f"Projects folder: {settings.projects_dir}")
    print()

    if path_arg is not None:
        try:
            project = resolve_project_path(path_arg, settings)
        except PathError as exc:
            print(f"Error: {exc}")
            return EXIT_INVALID_INPUT
    else:
        project = prompt_for_project(settings)
        if project is None:
            print("No project selected – exiting.")
            return EXIT_INVALID_INPUT

    print(f"\nStep 1 – checking {project} ...\n")
    result = run_acceptance(project)
    print(format_result(result))
    if not result.accepted or check_only:
        return int(result.outcome)

    print("\nStep 2 – linters and formatters (the formatters change files in the project) ...\n")
    linting = run_linting(result, settings)
    print(format_linting(linting))
    if linting.run_dir is None:
        print("\nStep 3 skipped: there is no results folder (see the error above).")
        return EXIT_LINTING_INCOMPLETE

    # Runs even if a linter failed: the analysis does not depend on step 2.
    print("\nStep 3 – static code analysis (OpenGrep and SonarQube, in parallel) ...\n")
    analysis = run_static_analysis(result, settings, linting.run_dir)
    print(format_analysis(analysis))

    print("\nStep 4 – dependencies (OSV-Scanner and the package manager audits; "
          "package names and versions are sent to OSV.dev and the registries) ...\n")
    dependencies = run_dependencies(result, settings, linting.run_dir)
    print(format_dependencies(dependencies))

    print("\nStep 5 – tests (finding and running them; the project's tests run in containers) ...\n")
    tests = run_tests(result, settings, linting.run_dir)
    print(format_tests(tests))
    if not linting.complete:
        return EXIT_LINTING_INCOMPLETE
    if not analysis.complete:
        return EXIT_ANALYSIS_INCOMPLETE
    if not dependencies.complete:
        return EXIT_DEPENDENCIES_INCOMPLETE
    return int(result.outcome) if tests.complete else EXIT_TESTS_INCOMPLETE


def prompt_for_project(settings: Settings) -> Path | None:
    """Ask until a usable path is entered. Empty input, Ctrl-C or Ctrl-D give None."""
    while True:
        try:
            raw = input("Path to the project to analyze: ")
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not raw.strip():
            return None
        try:
            return resolve_project_path(raw, settings)
        except PathError as exc:
            print(f"  {exc}\n")


def describe_ollama(status: OllamaStatus) -> str:
    if not status.reachable:
        return (
            f"Warning: Ollama is not reachable at {status.base_url} ({status.error}).\n"
            "         The acceptance check works without it; the LLM evaluation (step 6) will need it."
        )
    if not status.models:
        return f"Ollama: reachable at {status.base_url} (no models installed)"
    names = ", ".join(status.models[:MAX_LISTED_MODELS])
    if len(status.models) > MAX_LISTED_MODELS:
        names += ", ..."
    return f"Ollama: reachable at {status.base_url} ({len(status.models)} models: {names})"


def format_result(result: AcceptanceResult) -> str:
    report = result.languages
    lines: list[str] = []

    if report.primary is not None:
        shown = (report.primary, *report.others)
        languages = ", ".join(f"{language} {percent(report.share(language))}" for language in shown)
        lines.append(f"Languages:        {languages} (primary: {report.primary})")
    if report.excluded:
        kinds = Counter(reason.split(":")[0] for reason in report.excluded.values())
        listed = ", ".join(f"{count} {kind}" for kind, count in kinds.most_common())
        lines.append(f"Not counted:      {len(report.excluded)} files ({listed}), not linted either")

    for language, hits in result.package_managers.items():
        if hits:
            listed = ", ".join(f"{hit.manager} ({hit.manifest.as_posix()})" for hit in hits[:MAX_LISTED_MANIFESTS])
            if len(hits) > MAX_LISTED_MANIFESTS:
                listed += f", ... and {len(hits) - MAX_LISTED_MANIFESTS} more"
            lines.append(f"Package managers: {language}: {listed}")

    if result.git is not None and result.git.clean:
        lines.append("Git:              clean working tree")

    warnings = [
        f"{language} ({percent(report.share(language))}) is not supported and will be skipped in the analysis."
        for language in report.unsupported_others
    ]
    if result.accepted:
        warnings += [
            f"No package manager found for {language} – its dependency checks will be skipped."
            for language, hits in result.package_managers.items()
            if not hits
        ]
    lines += [f"Warning: {warning}" for warning in warnings]

    if lines:
        lines.append("")
    lines.append(result.message)
    return "\n".join(lines)


def percent(share: float) -> str:
    """Share of the source code; small but present languages show as "<1%"."""
    return "<1%" if 0 < share < 0.005 else f"{share:.0%}"


def format_linting(result: LintingResult) -> str:
    lines: list[str] = []
    if result.tool_runs:
        findings = len(result.findings)
        failed = [f"{run.language} {run.tool}" for run in result.tool_runs if run.status is Status.FAILED]
        lines.append(
            f"\n{findings} findings, {len(result.changed_files)} files changed by the tools."
        )
        if failed:
            lines.append(f"Failed: {', '.join(failed)} (see the notes above and summary.json).")
    lines += [f"Error: {error}" for error in result.errors]
    if result.run_dir is not None:
        lines.append(f"Results: {result.run_dir}")
    if result.changed_files:
        lines.append("Review the changes in the project with `git diff`, then commit or discard them.")
    lines.append("Step 2 complete." if result.complete else "Step 2 incomplete.")
    return "\n".join(lines)


def format_analysis(result: AnalysisResult) -> str:
    lines: list[str] = []
    if result.tool_runs:
        findings = result.findings
        by_severity = Counter(finding.severity for finding in findings)
        detail = ", ".join(f"{count} {severity}" for severity, count in by_severity.most_common())
        lines.append(f"\n{len(findings)} findings" + (f" ({detail})." if detail else "."))
        for run in result.tool_runs:
            if run.rulesets:
                lines.append(f"Rule sets of {run.tool}: " + ", ".join(_ruleset_label(entry) for entry in run.rulesets))
            if web := run.details.get("web_interface"):
                lines.append(f"SonarQube web interface: {web}")
        failed = [run.tool for run in result.tool_runs if run.status is Status.FAILED]
        if failed:
            lines.append(f"Failed: {', '.join(failed)} (see the notes above and summary.json).")
    lines += [f"Error: {error}" for error in result.errors]
    if result.run_dir is not None:
        lines.append(f"Results: {result.run_dir / ANALYSIS_STEP_DIR}")
    lines.append("Step 3 complete." if result.complete else "Step 3 incomplete.")
    return "\n".join(lines)


def _ruleset_label(entry: dict) -> str:
    """"opengrep-rules (f1d2b56)" for OpenGrep, "Sonar way (py)" for SonarQube's quality profiles."""
    detail = str(entry["commit"])[:7] if entry.get("commit") else entry.get("language", "")
    return f"{entry['name']} ({detail})" if detail else str(entry["name"])


def format_dependencies(result: DependencyResult) -> str:
    lines: list[str] = []
    if result.coverage:
        generated = [entry.manifest for entry in result.coverage if entry.lockfile_state == "generated"]
        uncovered = [f"{entry.manager} ({entry.manifest})" for entry in result.coverage
                     if not entry.osv and entry.audit is None]
        if generated:
            lines.append(f"\nLock files generated for: {', '.join(generated)}")
        if uncovered:
            lines.append(f"Not checked by any tool: {', '.join(uncovered)}")
        for entry in result.incomplete_lockfiles:
            state = "failed" if entry.lockfile_state == "failed" else "incomplete"
            lines.append(f"Lock file {state}: {entry.manager} ({entry.manifest}): {entry.notes[-1]}")
    if result.tool_runs:
        counts = Counter(finding.severity for finding in result.findings)
        order = ("critical", "high", "medium", "low", "unknown")
        detail = ", ".join(f"{counts[severity]} {severity}" for severity in order if counts[severity])
        advisories = {(finding.package, finding.id) for finding in result.findings}
        lines.append(f"\n{len(result.findings)} findings" + (f" ({detail})" if detail else "")
                     + f", {len(advisories)} distinct package/advisory pairs.")
        scopes = scope_counts(result.findings)
        if scopes:
            detail = ", ".join(f"{count} {scope}" for scope, count in scopes.items())
            lines.append(f"Gradle findings by scope: {detail}")
        failed = [f"{run.tool} ({run.manifest})" if run.manifest else run.tool
                  for run in result.tool_runs if run.status is Status.FAILED]
        if failed:
            lines.append(f"Failed: {', '.join(failed)} (see the notes above and summary.json).")
    lines += [f"Error: {error}" for error in result.errors]
    if result.run_dir is not None:
        lines.append(f"Results: {result.run_dir / DEPENDENCIES_STEP_DIR}")
    lines.append("Step 4 complete." if result.complete else "Step 4 incomplete.")
    return "\n".join(lines)


def format_tests(result: TestingResult) -> str:
    lines: list[str] = []
    if result.suites:
        found = sum(suite.tests_found for suite in result.suites)
        lines.append(f"\nTests found in {found} of {len(result.suites)} checked build units.")
    lines += [f"No tests found for {language} – noted in the report." for language in languages_without_tests(result)]
    lines += [f"Not checked: {language} ({reason})." for language, reason in result.not_checked.items()]
    for run in result.runs:
        counts = ", ".join(f"{count} {outcome}" for outcome, count in run.counts.items() if count)
        lines.append(f"{run.suite}: {run.status.value}" + (f", {counts}" if counts else ""))
    failing = sum(run.counts["failed"] + run.counts["error"] for run in result.runs)
    if failing and not result.network:
        lines.append(f"{failing} tests failed. The tests ran without network (config/tests.toml); "
                     "tests that need it fail for that reason.")
    if result.network and result.runs:
        lines.append("Warning: the tests ran with network (config/tests.toml: network = true).")
    for comparison in result.differences:
        lines.append(f"Differs from HEAD (last commit, without the changes of step 2): {comparison.suite}")
        if comparison.status_after_step_2 != comparison.status_head or not comparison.tests:
            lines.append(f"  after step 2: {_run_state(comparison.status_after_step_2, comparison.counts_after_step_2)}"
                         f"; at HEAD: {_run_state(comparison.status_head, comparison.counts_head)}")
        for test in comparison.tests[:MAX_LISTED_DIFFERENCES]:
            lines.append(f"  {test.id}: {test.after_step_2} after step 2, {test.head} at HEAD")
        if len(comparison.tests) > MAX_LISTED_DIFFERENCES:
            lines.append(f"  ... and {len(comparison.tests) - MAX_LISTED_DIFFERENCES} more (summary.json)")
    if result.differences:
        lines.append("Differences can come from the changes of step 2 or from flaky tests.")
    failed = [run.suite for run in result.runs if run.status is Status.FAILED]
    if failed:
        lines.append(f"Failed: {', '.join(failed)} (see the notes above and summary.json).")
    lines += [f"Error: {error}" for error in result.errors]
    if result.run_dir is not None:
        lines.append(f"Results: {result.run_dir / TESTS_STEP_DIR}")
    lines.append("Step 5 complete." if result.complete else "Step 5 incomplete.")
    return "\n".join(lines)


def _run_state(status: str, counts: dict[str, int]) -> str:
    total = sum(counts.values())
    if not total:
        return f"{status}, no test ran"
    return f"{status}, {total} tests (" + ", ".join(f"{count} {outcome}" for outcome, count in counts.items()
                                                    if count) + ")"

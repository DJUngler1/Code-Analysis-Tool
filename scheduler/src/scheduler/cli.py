"""Entry point: ask for the project, check it (step 1), run linters and formatters (step 2),
run the static code analysis (step 3)."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from scheduler.acceptance import AcceptanceResult, run_acceptance
from scheduler.config import Settings
from scheduler.linting import LintingResult, Status, run_linting
from scheduler.ollama import OllamaStatus, check_ollama
from scheduler.paths import PathError, resolve_project_path
from scheduler.static_analysis import AnalysisResult, run_static_analysis
from scheduler.static_analysis.run import STEP_DIR as ANALYSIS_STEP_DIR

EXIT_INVALID_INPUT = 1
# Step 2 could not run completely: OUTPUT_DIR, Docker or a tool image missing,
# a tool failed, or project files could not be moved back.
EXIT_LINTING_INCOMPLETE = 5
# Step 3 could not run completely: rule set configuration invalid, Docker or
# the OpenGrep image missing or outdated, or OpenGrep failed.
EXIT_ANALYSIS_INCOMPLETE = 6
MAX_LISTED_MANIFESTS = 5
MAX_LISTED_MODELS = 5


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
    if not linting.complete:
        return EXIT_LINTING_INCOMPLETE
    return int(result.outcome) if analysis.complete else EXIT_ANALYSIS_INCOMPLETE


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

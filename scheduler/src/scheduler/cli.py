"""Entry point: ask for the project, check it (step 1), make sure there is a confirmed threat model of the project
(scheduler.threat_model), run linters and formatters (step 2),
run the static code analysis (step 3), check the dependencies (step 4) and the tests (step 5),
evaluate the results with local LLMs and write the audit (step 6).

`--evaluate <run folder>` runs only step 6 on the results of an earlier run; with `--resume`, it reuses the
decisions of that run's earlier evaluation and writes only the reports again."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

from scheduler.acceptance import AcceptanceResult, run_acceptance
from scheduler.config import Settings
from scheduler.dependencies import DependencyResult, run_dependencies
from scheduler.dependencies.run import STEP_DIR as DEPENDENCIES_STEP_DIR
from scheduler.dependencies.run import scope_counts
from scheduler.evaluation import EvaluationResult, run_evaluation
from scheduler.evaluation.config import ConfigError as LlmConfigError
from scheduler.evaluation.config import load_config as load_llm_config
from scheduler.evaluation.items import project_of, read_json
from scheduler.evaluation.ollama import model_key
from scheduler.evaluation.run import AREA_NAMES, plural
from scheduler.evaluation.run import STEP_DIR as EVALUATION_STEP_DIR
from scheduler.linting import LintingResult, Status, run_linting
from scheduler.linting.run import acceptance_facts, project_folder_name
from scheduler.ollama import OllamaStatus, check_ollama
from scheduler.paths import PathError, resolve_project_path, resolve_run_folder
from scheduler.static_analysis import AnalysisResult, run_static_analysis
from scheduler.static_analysis.run import STEP_DIR as ANALYSIS_STEP_DIR
from scheduler.testing import TestingResult, run_tests
from scheduler.testing.execute import describe_coverage, describe_mutation
from scheduler.testing.run import STEP_DIR as TESTS_STEP_DIR
from scheduler.testing.run import languages_without_tests
from scheduler.threat_model import ThreatModelError, copy_to_run, ensure_threat_model, project_facts

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
# Step 5 could not run completely: code-analysis.toml ([tests]) invalid, Docker or a test image missing,
# or the tests of a suite could not be run.
EXIT_TESTS_INCOMPLETE = 8
# Step 6 could not run completely: code-analysis.toml ([llm]) invalid, Ollama unreachable or
# a model missing, or an area got no labels or texts from the models.
EXIT_EVALUATION_INCOMPLETE = 9
# No confirmed threat model of the project, and none could be made: no terminal to answer the questions,
# aborted by the user, OUTPUT_DIR missing, or no draft from the reasoning model.
EXIT_THREAT_MODEL_MISSING = 10
MAX_LISTED_MANIFESTS = 5
MAX_LISTED_MODELS = 5
MAX_LISTED_DIFFERENCES = 10


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-analysis", description="Code-Analysis-Tool")
    parser.add_argument("--path", help="project to analyze (skips the interactive prompt)")
    parser.add_argument(
        "--check-only", action="store_true", help="only run the acceptance check (step 1), change nothing"
    )
    parser.add_argument(
        "--evaluate", metavar="RUN_FOLDER",
        help="only run step 6 (LLM evaluation and audit) on the results folder of an earlier run",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="with --evaluate: reuse the decisions of the run's earlier evaluation (same decision model, settings "
             "and questions) and write only the reports again, e.g. with another reasoning model",
    )
    args = parser.parse_args(argv)
    if args.evaluate is not None and (args.path is not None or args.check_only):
        parser.error("--evaluate cannot be combined with --path or --check-only")
    if args.resume and args.evaluate is None:
        parser.error("--resume needs --evaluate <run folder>")
    settings = Settings.from_env()

    try:
        if args.evaluate is not None:
            return _evaluate_only(args.evaluate, settings, resume=args.resume)
        return _run(args.path, settings, check_only=args.check_only)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_INVALID_INPUT


def _run(path_arg: str | None, settings: Settings, *, check_only: bool = False) -> int:
    print("Code-Analysis-Tool\n")
    print(describe_ollama(check_ollama(settings.ollama_base_url), required_models(settings)))
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

    print("\nThreat model – what an attacker controls and what is shipped (used by step 6) ...\n")
    folder = settings.output_dir / project_folder_name(project) if settings.output_dir else None
    try:
        threat_model = ensure_threat_model(project, settings, folder=folder, interactive=_interactive(),
                                           facts=project_facts(acceptance_facts(result), project))
    except ThreatModelError as exc:
        print(f"Error: {exc}")
        return EXIT_THREAT_MODEL_MISSING

    print("\nStep 2 – linters and formatters (the formatters change files in the project) ...\n")
    linting = run_linting(result, settings)
    print(format_linting(linting))
    if linting.run_dir is None:
        print("\nStep 3 skipped: there is no results folder (see the error above).")
        return EXIT_LINTING_INCOMPLETE
    copy_to_run(linting.run_dir, threat_model)

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

    print(f"\nStep 6 – LLM evaluation and audit (local models through Ollama at {settings.ollama_base_url}) ...\n")
    evaluation = run_evaluation(linting.run_dir, settings)
    print(format_evaluation(evaluation))
    if not linting.complete:
        return EXIT_LINTING_INCOMPLETE
    if not analysis.complete:
        return EXIT_ANALYSIS_INCOMPLETE
    if not dependencies.complete:
        return EXIT_DEPENDENCIES_INCOMPLETE
    if not tests.complete:
        return EXIT_TESTS_INCOMPLETE
    return int(result.outcome) if evaluation.complete else EXIT_EVALUATION_INCOMPLETE


def _evaluate_only(raw: str, settings: Settings, *, resume: bool = False) -> int:
    print("Code-Analysis-Tool – step 6 on the results of an earlier run"
          + (", reusing its decisions where possible (--resume)" if resume else "") + "\n")
    print(describe_ollama(check_ollama(settings.ollama_base_url), required_models(settings)))
    try:
        run_dir = resolve_run_folder(raw, settings)
    except PathError as exc:
        print(f"Error: {exc}")
        return EXIT_INVALID_INPUT
    print("\nThreat model – what an attacker controls and what is shipped (used by step 6) ...\n")
    project = project_of(run_dir)
    project = project if project is not None and project.is_dir() else None
    try:  # the project's current threat model; a changed one makes --resume label security again
        threat_model = ensure_threat_model(project, settings, folder=run_dir.parent, interactive=_interactive(),
                                           facts=project_facts(read_json(run_dir / "run.json", {}), project))
    except ThreatModelError as exc:
        print(f"Error: {exc}")
        return EXIT_THREAT_MODEL_MISSING
    copy_to_run(run_dir, threat_model)
    print(f"\nStep 6 – LLM evaluation and audit of {run_dir} ...\n")
    evaluation = run_evaluation(run_dir, settings, resume=resume)
    print(format_evaluation(evaluation))
    return 0 if evaluation.complete else EXIT_EVALUATION_INCOMPLETE


def _interactive() -> bool:
    """A terminal to answer the threat model questions (bin/code-analysis passes -t only when there is one)."""
    return sys.stdin.isatty()


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


def required_models(settings: Settings) -> tuple[str, ...]:
    """The models step 6 needs ([llm.decision] and [llm.report]); none if [llm] is invalid (step 6 reports it)."""
    try:
        config = load_llm_config(settings.config_file)
    except LlmConfigError:
        return ()
    return (config.decision.model, config.report.model)


def describe_ollama(status: OllamaStatus, required: Sequence[str] = ()) -> str:
    if not status.reachable:
        return (
            f"Warning: Ollama is not reachable at {status.base_url} ({status.error}).\n"
            "         The acceptance check works without it; the LLM evaluation (step 6) needs it."
        )
    if not status.models:
        text = f"Ollama: reachable at {status.base_url} (no models installed)"
    else:
        names = ", ".join(status.models[:MAX_LISTED_MODELS])
        if len(status.models) > MAX_LISTED_MODELS:
            names += ", ..."
        text = f"Ollama: reachable at {status.base_url} ({len(status.models)} models: {names})"
    installed = {model_key(name) for name in status.models}
    missing = [model for model in dict.fromkeys(required) if model_key(model) not in installed]
    if missing:
        commands = "; ".join(f"ollama pull {model}" for model in missing)
        text += (f"\nWarning: step 6 needs {', '.join(missing)}, not installed in Ollama.\n"
                 f"         Install on the host with: {commands}")
    return text


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
    for coverage in result.coverage:
        lines.append(f"{coverage.suite}: coverage {describe_coverage(coverage)}")
    for mutation in result.mutation:
        lines.append(f"{mutation.suite}: mutation testing {describe_mutation(mutation)}")
    failing = sum(run.counts["failed"] + run.counts["error"] for run in result.runs)
    if failing and not result.network:
        lines.append(f"{failing} tests failed. The tests ran without network (code-analysis.toml, [tests]); "
                     "tests that need it fail for that reason.")
    if result.network and result.runs:
        lines.append("Warning: the tests ran with network (code-analysis.toml: [tests] network = true).")
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
    failed += [f"coverage of {coverage.suite}" for coverage in result.coverage if coverage.status is Status.FAILED]
    failed += [f"mutation testing of {mutation.suite}" for mutation in result.mutation
               if mutation.status is Status.FAILED]
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


def format_evaluation(result: EvaluationResult) -> str:
    lines: list[str] = [""]
    for area in result.areas:
        name = AREA_NAMES[area.area]
        if area.status is Status.SKIPPED:
            lines.append(f"{name}: skipped" + (f" ({' '.join(area.notes)})" if area.notes else ""))
            continue
        text = f"{name}: {_describe_area(area.area, area.statistics)}" if area.statistics else f"{name}:"
        if area.status is not Status.OK:
            text += f" – {area.status.value}" + (f" ({' '.join(area.notes)})" if area.notes else "")
        lines.append(text)
    lines += [f"Error: {error}" for error in result.errors]
    if result.audit is not None:
        lines.append(f"Audit: {result.audit}")
    if result.run_dir is not None:
        lines.append(f"Results: {result.run_dir / EVALUATION_STEP_DIR}")
    lines.append("Step 6 complete." if result.complete else "Step 6 incomplete.")
    return "\n".join(lines)


def _describe_area(area: str, statistics: dict) -> str:
    low = statistics.get("low_confidence", 0)
    review = f"; {plural(low, 'decision', 'decisions')} with low confidence (see the review list)" if low else ""
    if area == "security":
        labels = statistics.get("by_label", {})
        severity = ", ".join(f"{count} {name}" for name, count in statistics.get("real_by_severity", {}).items())
        return (f"{labels.get('REAL', 0)} REAL of {statistics.get('input_findings', 0)} findings"
                + (f" ({severity})" if severity else "") + review)
    if area == "code_quality":
        return (f"{statistics.get('groups_by_label', {}).get('REAL', 0)} REAL groups "
                f"({statistics.get('findings_by_label', {}).get('REAL', 0)} findings) of "
                f"{statistics.get('groups', 0)} groups ({statistics.get('input_findings', 0)} findings)" + review)
    parts = []
    for key, singular, several in (("failed_test_groups", "group of failed tests", "groups of failed tests"),
                                   ("weakly_tested_files", "weakly tested file", "weakly tested files"),
                                   ("surviving_mutants", "surviving mutant", "surviving mutants")):
        counts = statistics.get(key) or {}
        if counts:
            parts.append(f"{plural(sum(counts.values()), singular, several)} ("
                         + ", ".join(f"{count} {name}" for name, count in counts.items()) + ")")
    return ("; ".join(parts) or "nothing to label") + review

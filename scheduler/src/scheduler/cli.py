"""Entry point: ask for the project and run the acceptance check (step 1)."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from scheduler.acceptance import AcceptanceResult, run_acceptance
from scheduler.config import Settings
from scheduler.ollama import OllamaStatus, check_ollama
from scheduler.paths import PathError, resolve_project_path

EXIT_INVALID_INPUT = 1
MAX_LISTED_MANIFESTS = 5
MAX_LISTED_MODELS = 5


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="code-analysis", description="Code-Analysis-Tool – project acceptance check")
    parser.add_argument("--path", help="project to analyze (skips the interactive prompt)")
    args = parser.parse_args(argv)
    settings = Settings.from_env()

    try:
        return _run(args.path, settings)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return EXIT_INVALID_INPUT


def _run(path_arg: str | None, settings: Settings) -> int:
    print("Code-Analysis-Tool – project acceptance check\n")
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

    print(f"\nChecking {project} ...\n")
    result = run_acceptance(project)
    print(format_result(result))
    return int(result.outcome)


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
        languages = ", ".join(f"{language} {report.share(language):.0%}" for language in shown)
        lines.append(f"Languages:        {languages} (primary: {report.primary})")

    for language, hits in result.package_managers.items():
        if hits:
            listed = ", ".join(f"{hit.manager} ({hit.manifest.as_posix()})" for hit in hits[:MAX_LISTED_MANIFESTS])
            if len(hits) > MAX_LISTED_MANIFESTS:
                listed += f", ... and {len(hits) - MAX_LISTED_MANIFESTS} more"
            lines.append(f"Package managers: {language}: {listed}")

    if result.git is not None and result.git.clean:
        lines.append("Git:              clean working tree")

    warnings = [
        f"{language} ({report.share(language):.0%}) is not supported and will be skipped in the analysis."
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

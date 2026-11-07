"""Step 1 of the workflow: decide whether a project is accepted for analysis."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from scheduler.acceptance.filetree import scan_project
from scheduler.acceptance.git_state import GitState, check_git_state
from scheduler.acceptance.languages import SUPPORTED_LANGUAGES, LanguageReport, detect_languages
from scheduler.acceptance.package_managers import PackageManagerHit, detect_package_managers, marker_files

MAX_LISTED_CHANGES = 10


class Outcome(IntEnum):
    """Result of the acceptance check; the value is the process exit code."""

    ACCEPTED = 0
    LANGUAGE_NOT_SUPPORTED = 2
    NO_PACKAGE_MANAGER = 3
    GIT_NOT_CLEAN = 4


@dataclass(frozen=True)
class AcceptanceResult:
    project: Path
    outcome: Outcome
    message: str
    languages: LanguageReport
    # Per supported language (primary first). Filled once the language check passed.
    package_managers: Mapping[str, tuple[PackageManagerHit, ...]] = field(default_factory=dict)
    git: GitState | None = None

    @property
    def accepted(self) -> bool:
        return self.outcome is Outcome.ACCEPTED


def run_acceptance(project: Path) -> AcceptanceResult:
    """Run the checks in flowchart order and stop at the first failure."""
    files = scan_project(project)
    languages = detect_languages(files, root=project)
    primary = languages.primary
    supported = ", ".join(SUPPORTED_LANGUAGES)

    if primary is None:
        return AcceptanceResult(
            project,
            Outcome.LANGUAGE_NOT_SUPPORTED,
            f"Abort – language not supported: no source code found (supported: {supported}).",
            languages,
        )
    if not languages.primary_supported:
        return AcceptanceResult(
            project,
            Outcome.LANGUAGE_NOT_SUPPORTED,
            f"Abort – language not supported: {primary} (supported: {supported}).",
            languages,
        )

    package_managers = {
        language: detect_package_managers(project, files, language)
        for language in (primary, *languages.supported_others)
    }
    if not package_managers[primary]:
        return AcceptanceResult(
            project,
            Outcome.NO_PACKAGE_MANAGER,
            f"Abort – project is not accepted: no package manager found for {primary} "
            f"(looked for: {', '.join(marker_files(primary))}).",
            languages,
            package_managers,
        )

    git = check_git_state(project)
    if not git.clean:
        return AcceptanceResult(
            project, Outcome.GIT_NOT_CLEAN, _git_message(git), languages, package_managers, git
        )

    return AcceptanceResult(project, Outcome.ACCEPTED, "Project accepted.", languages, package_managers, git)


def _git_message(git: GitState) -> str:
    prefix = "Abort – project is not accepted:"
    if git.error:
        return f"{prefix} git check failed: {git.error}"
    if not git.is_repo:
        return (
            f"{prefix} not a git repository. The tool modifies files (formatter), "
            "so the project must be under git to review and revert every change."
        )
    listed = "\n".join(f"    {line}" for line in git.changes[:MAX_LISTED_CHANGES])
    more = len(git.changes) - MAX_LISTED_CHANGES
    if more > 0:
        listed += f"\n    ... and {more} more"
    return (
        f"{prefix} uncommitted changes. Commit or stash them first, "
        f"so the tool's changes can be reviewed and reverted:\n{listed}"
    )

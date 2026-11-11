"""What the tools of step 3 share: the project, the run folder, Docker, settings."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.acceptance.languages import EXTENSION_TO_LANGUAGE, header_language
from scheduler.config import Settings
from scheduler.linting.docker import ToolRunner
from scheduler.static_analysis.sonar_api import SonarApi


@dataclass
class AnalysisContext:
    project: Path
    # Mounted read-only: the git repository root if the project is a subfolder
    # of one (inside PROJECTS_DIR), otherwise the project.
    mount_root: Path
    step_dir: Path  # <run>/step3-static-analysis
    acceptance: AcceptanceResult
    runner: ToolRunner
    settings: Settings
    out: Callable[[str], None] = print
    # Tests replace the SonarQube Web API and the waiting.
    sonar_api: Callable[..., SonarApi] | None = None
    sleep: Callable[[float], None] = time.sleep

    @property
    def languages(self) -> tuple[str, ...]:
        """Supported languages of step 1, primary first."""
        report = self.acceptance.languages
        return tuple(language for language in (report.primary, *report.supported_others) if language)

    def manifests(self, *languages: str) -> list[Path]:
        """Package manager manifests found by step 1, relative to the project."""
        return [hit.manifest for language in languages for hit in self.acceptance.package_managers.get(language, ())]


def language_of(file: str | None, project: Path) -> str | None:
    """Language of step 1 for a file, from its extension (".h": by content)."""
    if file is None:
        return None
    suffix = Path(file).suffix.lower()
    if suffix == ".h":
        path = project / file
        return header_language(path) if path.is_file() else "C/C++"
    return EXTENSION_TO_LANGUAGE.get(suffix)

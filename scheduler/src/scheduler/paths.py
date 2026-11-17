"""Normalize and validate the project path entered by the user."""

from __future__ import annotations

import re
from pathlib import Path

from scheduler.config import Settings
from scheduler.evaluation.items import DEPENDENCIES_DIR, LINT_DIR, STATIC_DIR, TESTS_DIR

# Contents of a run folder (<OUTPUT_DIR>/<project>/<date_time>/), one is enough.
RUN_FOLDER_MARKERS = ("run.json", LINT_DIR, STATIC_DIR, DEPENDENCIES_DIR, TESTS_DIR)


class PathError(ValueError):
    """The entered path cannot be used as a project path."""


def normalize(raw: str, host_home: Path | None = None) -> Path:
    """Turn user input into a path.

    Handles what typically ends up in the prompt: surrounding whitespace,
    quotes, shell escapes from dragging a folder into the macOS Terminal
    (``My\\ Project``) and a leading ``~``.
    """
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1]
    else:
        text = re.sub(r"\\(.)", r"\1", text)

    if text == "~" or text.startswith("~/"):
        home = host_home if host_home is not None else Path.home()
        text = str(home) + text[1:]
    return Path(text)


def resolve_project_path(raw: str, settings: Settings) -> Path:
    """Return the absolute project directory or raise PathError."""
    if not raw.strip():
        raise PathError("No path entered.")

    path = normalize(raw, settings.host_home)
    if not path.is_absolute():
        raise PathError(f"'{path}' is not an absolute path. Please enter the full path, e.g. /Users/me/projects/my-app.")
    path = path.resolve()

    # Checked before existence: inside the container, folders outside the
    # mounted projects folder are invisible, so "does not exist" would mislead.
    if settings.projects_dir is not None and not path.is_relative_to(settings.projects_dir):
        raise PathError(
            f"{path} is outside the projects folder {settings.projects_dir}. "
            "Only folders below it are visible to the tool. "
            "To analyze projects elsewhere, change PROJECTS_DIR in docker/.env."
        )
    if not path.exists():
        raise PathError(f"{path} does not exist.")
    if not path.is_dir():
        raise PathError(f"{path} is not a directory.")
    return path


def resolve_run_folder(raw: str, settings: Settings) -> Path:
    """Return the results folder of an earlier run (for --evaluate) or raise PathError."""
    if not raw.strip():
        raise PathError("No run folder entered.")
    path = normalize(raw, settings.host_home)
    if not path.is_absolute():
        raise PathError(f"'{path}' is not an absolute path. Please enter the full path of the run folder, "
                        "e.g. /Users/me/code-analysis-results/my-app/2026-01-31_120000Z.")
    path = path.resolve()
    if settings.output_dir is not None and not path.is_relative_to(settings.output_dir):
        raise PathError(f"{path} is outside the results folder {settings.output_dir} (OUTPUT_DIR in docker/.env). "
                        "Only folders below it are visible to the tool.")
    if not path.is_dir():
        raise PathError(f"{path} does not exist or is not a folder.")
    if not any((path / marker).exists() for marker in RUN_FOLDER_MARKERS):
        raise PathError(f"{path} is not the folder of a run: it has no run.json and no step folders "
                        "(expected <OUTPUT_DIR>/<project>/<date_time>).")
    return path

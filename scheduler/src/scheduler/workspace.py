"""A copy of the project for tools that build or resolve it (steps 3 and 4)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scheduler.linting.files import source_files


def prepare_workspace(project: Path, workspace: Path) -> int:
    """Copy the files git knows (tracked, or untracked and not ignored) into the workspace.

    All of them, also vendored and generated ones: builds may need them. The
    copy becomes a git repository of its own (nothing committed), so that tools
    that rely on git (SonarQube's text sensor) see all files.
    Returns the number of files copied.
    """
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    copied = 0
    for relative in source_files(project, None, skip_folders=False):
        source = project / relative
        if source.is_symlink() or not source.is_file():
            continue
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    for args in (["init", "--quiet"], ["add", "--all"]):
        subprocess.run(["git", "-c", "safe.directory=*", "-C", str(workspace), *args],
                       capture_output=True, check=False, timeout=600)
    return copied

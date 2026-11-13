"""A copy of the project for tools that build or resolve it (steps 3 to 5)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from scheduler.linting.files import source_files
from scheduler.linting.snapshots import git_toplevel

GIT_TIMEOUT_SECONDS = 600


class WorkspaceError(Exception):
    """The copy could not be created."""


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
    _init_git(workspace)
    return copied


def prepare_head_workspace(project: Path, workspace: Path) -> int:
    """Write the project as of its last commit (HEAD) into the workspace.

    Uses a temporary index file next to the workspace, so the project's own
    repository is not changed (unlike `git worktree`), and every committed file
    is written (unlike `git archive`, which leaves out files marked
    export-ignore). Submodules are not included. Like prepare_workspace, the
    copy becomes a git repository of its own. Returns the number of files.
    """
    toplevel = git_toplevel(project)
    if toplevel is None:
        raise WorkspaceError(f"{project} is not in a git repository")
    shutil.rmtree(workspace, ignore_errors=True)
    staging = workspace.with_name(workspace.name + ".checkout")
    index = workspace.with_name(workspace.name + ".index")
    shutil.rmtree(staging, ignore_errors=True)
    staging.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    git = ["git", "-c", "safe.directory=*", "-C", str(toplevel)]
    try:
        for args in (["read-tree", "HEAD"], ["checkout-index", "--all", f"--prefix={staging}/"]):
            result = subprocess.run([*git, *args], env=env, capture_output=True, text=True,
                                    timeout=GIT_TIMEOUT_SECONDS, check=False)
            if result.returncode != 0:
                raise WorkspaceError(f"git {args[0]} failed: {result.stderr.strip()}")
        source = staging / project.resolve().relative_to(toplevel.resolve())
        if source.is_dir():
            source.rename(workspace)
        else:  # the project folder has no committed files
            workspace.mkdir(parents=True)
    finally:
        index.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)
    _init_git(workspace)
    return sum(1 for path in workspace.rglob("*") if path.is_file() and ".git" not in path.relative_to(workspace).parts)


def _init_git(workspace: Path) -> None:
    for args in (["init", "--quiet"], ["add", "--all"]):
        subprocess.run(["git", "-c", "safe.directory=*", "-C", str(workspace), *args],
                       capture_output=True, check=False, timeout=GIT_TIMEOUT_SECONDS)

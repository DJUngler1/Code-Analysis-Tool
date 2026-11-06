"""Check that the project is a git repository without uncommitted changes.

Later steps (formatter) modify project files. Requiring a clean working tree
makes every change by the tool visible in `git diff` and revertible.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class GitState:
    is_repo: bool
    # `git status --porcelain` lines, e.g. " M src/app.py" or "?? notes.txt"
    changes: tuple[str, ...] = ()
    error: str | None = None

    @property
    def clean(self) -> bool:
        return self.is_repo and not self.changes and self.error is None


def check_git_state(project: Path) -> GitState:
    """Inspect the working tree. For a subfolder of a repo, only that subfolder counts."""
    try:
        inside = _git(project, "rev-parse", "--is-inside-work-tree")
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return GitState(is_repo=False)

        # --untracked-files=normal overrides a repo config that hides untracked
        # files: a formatter could change those without any way to revert.
        status = _git(project, "status", "--porcelain", "--untracked-files=normal", "--", ".")
    except FileNotFoundError:
        return GitState(is_repo=False, error="git is not installed")
    except subprocess.TimeoutExpired:
        return GitState(is_repo=True, error=f"git did not answer within {GIT_TIMEOUT_SECONDS} s")

    if status.returncode != 0:
        return GitState(is_repo=True, error=status.stderr.strip() or "git status failed")
    return GitState(is_repo=True, changes=tuple(line for line in status.stdout.splitlines() if line))


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # safe.directory: the mounted project belongs to the host user, which git
    # would otherwise reject as "dubious ownership" inside the container.
    return subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(project), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )

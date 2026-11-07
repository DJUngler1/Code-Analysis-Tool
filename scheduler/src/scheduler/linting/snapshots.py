"""Record which tool changed which files, without touching the project's git data.

A snapshot is a git tree of the tracked files' current content. It is written
with a temporary index and a temporary object directory (the repository's own
objects are only read, as alternates), so the project's .git stays unchanged.
Untracked files are left out on purpose: formatters only change tracked files,
while builds (obj/, target/) create untracked ones.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

GIT_TIMEOUT_SECONDS = 600


class SnapshotError(Exception):
    pass


class Snapshotter:
    def __init__(self, project: Path) -> None:
        self.project = project
        self._tmp = tempfile.TemporaryDirectory(prefix="code-analysis-snapshots-")
        self._env: dict[str, str] | None = None
        git_dir = Path(self._git("rev-parse", "--git-common-dir").strip())
        if not git_dir.is_absolute():
            git_dir = (project / git_dir).resolve()
        # git only accepts an object directory that exists.
        (Path(self._tmp.name) / "objects").mkdir()
        self._env = {
            **os.environ,
            "GIT_INDEX_FILE": str(Path(self._tmp.name) / "index"),
            "GIT_OBJECT_DIRECTORY": str(Path(self._tmp.name) / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(git_dir / "objects"),
        }

    def base(self) -> str:
        """Tree of the last commit (the state accepted by step 1)."""
        return self._git("rev-parse", "HEAD^{tree}").strip()

    def snapshot(self) -> str:
        """Tree of the tracked files as they are now.

        The index starts from HEAD every time: files moved out for a while
        (hidden configuration) count as tracked again once they are back.
        """
        self._git("read-tree", "HEAD")
        self._git("add", "--update", "--", ".")
        return self._git("write-tree").strip()

    def changed_files(self, before: str, after: str) -> list[str]:
        if before == after:
            return []
        output = self._git("diff", "--name-only", "--relative", before, after, "--", ".")
        return [line for line in output.splitlines() if line]

    def patch(self, before: str, after: str) -> str:
        if before == after:
            return ""
        return self._git("diff", "--binary", "--relative", before, after, "--", ".")

    def close(self) -> None:
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        # Before __init__ sets self._env, git runs with the normal environment.
        result = subprocess.run(
            ["git", "-c", "safe.directory=*", "-c", "core.quotePath=false", "-C", str(self.project), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT_SECONDS,
            env=self._env,
        )
        if result.returncode != 0:
            raise SnapshotError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout


def git_toplevel(path: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 and result.stdout.strip() else None


def head_commit(path: Path) -> str | None:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def untracked_files(path: Path) -> list[str]:
    """Untracked, not ignored files below path (e.g. build output a tool created)."""
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-c", "core.quotePath=false", "-C", str(path),
         "ls-files", "--others", "--exclude-standard", "--", "."],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    return [line for line in result.stdout.splitlines() if line] if result.returncode == 0 else []

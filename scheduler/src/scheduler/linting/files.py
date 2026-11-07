"""Source files a tool works on: tracked by git, outside vendored or generated folders."""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from pathlib import Path

from scheduler.acceptance.filetree import is_skipped_dir

GIT_TIMEOUT_SECONDS = 120


def source_files(project: Path, suffixes: Iterable[str]) -> list[Path]:
    """Project files with one of the suffixes, relative to the project.

    Only files git knows (tracked or untracked but not ignored), and none in
    the folders step 1 skips (vendor/, third_party/, build/, hidden folders, ...).
    """
    wanted = {suffix.lower() for suffix in suffixes}
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(project),
         "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
        capture_output=True,
        timeout=GIT_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        return []
    files: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        path = Path(raw.decode("utf-8", errors="surrogateescape"))
        if path.suffix.lower() not in wanted:
            continue
        if any(is_skipped_dir(part) for part in path.parts[:-1]):
            continue
        if (project / path).is_file():
            files.append(path)
    return sorted(set(files))


def write_file_list(paths: Iterable[Path], target: Path) -> Path:
    """NUL-separated list of absolute paths, read in the containers with `xargs -0`."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"".join(str(path).encode("utf-8", errors="surrogateescape") + b"\0" for path in paths))
    return target

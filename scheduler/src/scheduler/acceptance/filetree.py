"""Single walk over the project tree, shared by all acceptance checks."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

# Dependencies, virtual environments and build output. Hidden directories
# (.git, .venv, .idea, .gradle, .next, ...) are skipped as well.
SKIP_DIRS = frozenset(
    {
        "node_modules",
        "venv",
        "env",
        "__pycache__",
        "site-packages",
        "target",
        "build",
        "dist",
        "out",
        "bin",
        "obj",
        "vendor",
        "third_party",
        "coverage",
    }
)

# Marks a Python virtual environment, whatever the folder is called.
VENV_MARKER = "pyvenv.cfg"


@dataclass(frozen=True)
class ProjectFile:
    path: Path  # relative to the project root
    size: int  # bytes


def is_skipped_dir(name: str) -> bool:
    return name.startswith(".") or name in SKIP_DIRS


def scan_project(root: Path) -> list[ProjectFile]:
    """Return all regular files of the project, without skipped directories.

    Symlinks are neither followed nor reported.
    """
    files: list[ProjectFile] = []
    for dirpath, dirnames, filenames in os.walk(root):
        if VENV_MARKER in filenames and Path(dirpath) != root:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(d))

        relative_dir = Path(dirpath).relative_to(root)
        for name in sorted(filenames):
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                files.append(ProjectFile(relative_dir / name, info.st_size))
    return files

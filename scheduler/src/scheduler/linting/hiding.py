"""Move project configuration files out of the project while the tools run.

Some tools have no option to ignore a project's configuration (.editorconfig
for dotnet format, nested biome.json for Biome, .clang-format-ignore for
clang-format). These files are moved into the run folder for the duration of
the run and moved back afterwards, also when a tool fails.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

MANIFEST = "hidden.json"
# Never searched for configuration files: git internals and installed packages.
_NOT_SEARCHED = frozenset({".git", "node_modules"})


def find_files(root: Path, names: Iterable[str]) -> list[Path]:
    """Files with one of the given names below root, relative to root."""
    wanted = set(names)
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _NOT_SEARCHED)
        found += [Path(dirpath, name).relative_to(root) for name in sorted(filenames) if name in wanted]
    return found


@contextmanager
def hidden_files(root: Path, relative_paths: Iterable[Path], store: Path, errors: list[str]) -> Iterator[list[Path]]:
    """Move the files from root into store; move them back on exit.

    Problems while moving back are appended to errors (the run is then
    incomplete) instead of raised, so they never hide a tool's own error.
    """
    moved: list[Path] = []
    manifest = store / MANIFEST
    try:
        for relative in relative_paths:
            target = store / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(root / relative, target)
            moved.append(relative)
            _write_manifest(manifest, root, moved)
        yield moved
    finally:
        for relative in reversed(moved):
            try:
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.move(store / relative, root / relative)
            except OSError as exc:
                errors.append(
                    f"could not move {store / relative} back to {root / relative} ({exc}). "
                    "Move it back by hand."
                )
        if not any(error.startswith("could not move") for error in errors):
            manifest.unlink(missing_ok=True)


def _write_manifest(manifest: Path, root: Path, moved: list[Path]) -> None:
    manifest.write_text(
        json.dumps(
            {"project": str(root), "files": [path.as_posix() for path in moved]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

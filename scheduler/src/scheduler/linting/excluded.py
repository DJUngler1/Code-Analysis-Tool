"""Generated files (acceptance/generated.py): neither formatted nor linted.

Tools that get a file list (clang-format, clang-tidy, google-java-format) or
an exclude list (PMD) never see these files. Tools that work on whole folders
(Biome, Ruff, cargo clippy/fmt, dotnet format) cannot skip single files, so
after each tool its findings in these files are dropped and its changes to
them are undone from a copy made before the first tool ran.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path

from scheduler.acceptance.generated import exclusion_reason
from scheduler.linting.files import source_files


class ExcludedFiles:
    def __init__(self, project: Path | None = None, reasons: Mapping[str, str] | None = None,
                 backup_dir: Path | None = None) -> None:
        self.project = project
        self.reasons = dict(reasons or {})  # path relative to the project ("/"-separated) -> reason
        self.backup_dir = backup_dir
        self._stats: dict[str, tuple[int, int, int] | None] = {}
        self._restore_failed = False

    @classmethod
    def detect(cls, project: Path, suffixes: Iterable[str], backup_dir: Path) -> ExcludedFiles:
        reasons = {
            path.as_posix(): reason
            for path in source_files(project, suffixes)
            if (reason := exclusion_reason(project / path))
        }
        return cls(project, reasons, backup_dir)

    def __contains__(self, file: object) -> bool:
        return isinstance(file, (str, Path)) and Path(file).as_posix() in self.reasons

    def __len__(self) -> int:
        return len(self.reasons)

    def write_list(self, target: Path) -> None:
        entries = [{"file": file, "reason": reason} for file, reason in sorted(self.reasons.items())]
        target.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def save(self) -> None:
        """Copy the files before the first tool runs."""
        assert self.project is not None and self.backup_dir is not None
        for file in self.reasons:
            target = self.backup_dir / file
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.project / file, target)
            self._stats[file] = self._stat(file)

    def restore_changed(self, errors: list[str]) -> list[str]:
        """Undo a tool's changes to the files; returns the files restored."""
        restored: list[str] = []
        for file, before in self._stats.items():
            if self._stat(file) == before:
                continue
            assert self.project is not None and self.backup_dir is not None
            current, original = self.project / file, self.backup_dir / file
            try:
                if not current.is_file() or current.read_bytes() != original.read_bytes():
                    shutil.copyfile(original, current)
                    restored.append(file)
            except OSError as exc:
                self._restore_failed = True
                errors.append(f"could not undo the changes to the generated file {file}: {exc} "
                              f"(original: {original})")
                continue
            self._stats[file] = self._stat(file)
        return restored

    def discard_backup(self) -> None:
        """Remove the copies, unless a restore failed (they are then still needed)."""
        if self.backup_dir is not None and self.backup_dir.exists() and not self._restore_failed:
            shutil.rmtree(self.backup_dir, ignore_errors=True)

    def _stat(self, file: str) -> tuple[int, int, int] | None:
        assert self.project is not None
        try:
            info = os.stat(self.project / file)
        except OSError:
            return None
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns

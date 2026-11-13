"""Read coverage reports into FileCoverage per project file (5.3).

- Cobertura XML: coverage.py, Jest and c8 (Istanbul), Vitest (v8), coverlet, cargo-llvm-cov, gcovr.
- JaCoCo XML: JaCoCo (Java); its paths are relative to the source folders.

Only files inside the copy of the project are kept; their paths become
relative to the project (the copy has the project's layout).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from scheduler.testing.model import FileCoverage
from scheduler.testing.reports import ReportError

_CONDITION = re.compile(r"\((\d+)/(\d+)\)")


def ranges(numbers: Iterable[int]) -> str:
    """[3, 4, 5, 9] -> "3-5, 9"."""
    parts: list[str] = []
    start = previous = None
    for number in sorted(set(numbers)):
        if previous is not None and number == previous + 1:
            previous = number
            continue
        if start is not None:
            parts.append(f"{start}-{previous}" if previous != start else str(start))
        start = previous = number
    if start is not None:
        parts.append(f"{start}-{previous}" if previous != start else str(start))
    return ", ".join(parts)


def _parse(path: Path) -> ET.Element:
    try:
        return ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc


def parse_cobertura(path: Path, workspace: Path) -> dict[str, FileCoverage]:
    """Cobertura XML -> project-relative file -> coverage (line and branch counts)."""
    return parse_cobertura_files([path], workspace)


def parse_cobertura_files(paths: list[Path], workspace: Path) -> dict[str, FileCoverage]:
    """Several Cobertura reports of one run (e.g. one per target framework), merged line by line."""
    lines: dict[str, dict[int, tuple[int, int, int]]] = {}  # file -> line -> (hits, branches covered, total)
    branch_files: set[str] = set()
    for path in paths:
        root = _parse(path)
        sources = [Path(source.text.strip()) for source in root.iter("source") if source.text and source.text.strip()]
        for element in root.iter("class"):
            file = _project_file(element.get("filename") or "", sources, workspace)
            if file is None:
                continue
            per_line = lines.setdefault(file, {})
            for line in element.iter("line"):
                try:
                    number, hits = int(line.get("number") or ""), int(float(line.get("hits") or "0"))
                except ValueError:
                    continue
                covered = total = 0
                branch = (line.get("branch") or "").lower() == "true"  # coverlet writes "True"
                if branch and (match := _CONDITION.search(line.get("condition-coverage") or "")):
                    covered, total = int(match.group(1)), int(match.group(2))
                    branch_files.add(file)
                old = per_line.get(number, (0, 0, 0))
                per_line[number] = (max(old[0], hits), max(old[1], covered), max(old[2], total))
    return {file: _file_coverage(file, per_line, file in branch_files) for file, per_line in lines.items()}


def _project_file(name: str, sources: list[Path], workspace: Path) -> str | None:
    candidates = [Path(name)] if Path(name).is_absolute() else [source / name for source in sources] or [Path(name)]
    for candidate in candidates:
        if not candidate.is_absolute():
            candidate = workspace / candidate
        try:
            relative = candidate.resolve().relative_to(workspace.resolve())
        except ValueError:
            continue
        if (workspace / relative).is_file():
            return relative.as_posix()
    return None


def _file_coverage(file: str, per_line: dict[int, tuple[int, int, int]], branches: bool) -> FileCoverage:
    return FileCoverage(
        file=file,
        lines_total=len(per_line),
        lines_covered=sum(1 for hits, _, _ in per_line.values() if hits > 0),
        branches_total=sum(total for _, _, total in per_line.values()) if branches else None,
        branches_covered=sum(covered for _, covered, _ in per_line.values()) if branches else None,
        uncovered_lines=ranges(number for number, (hits, _, _) in per_line.items() if hits == 0),
    )


def parse_jacoco(path: Path, java_files: Iterable[str]) -> dict[str, FileCoverage]:
    """JaCoCo XML -> project-relative file -> coverage.

    JaCoCo names a file by package and file name ("calc/Calc.java"); it is
    matched to the project's Java files by that path ending.
    """
    root = _parse(path)
    by_suffix: dict[str, list[str]] = {}
    for file in java_files:
        parts = file.split("/")
        for start in range(len(parts)):
            by_suffix.setdefault("/".join(parts[start:]), []).append(file)
    result: dict[str, FileCoverage] = {}
    for package in root.iter("package"):
        for source in package.iter("sourcefile"):
            name = "/".join(filter(None, [package.get("name"), source.get("name")]))
            matches = by_suffix.get(name, [])
            if len(matches) != 1:
                continue
            per_line: dict[int, tuple[int, int, int]] = {}
            for line in source.iter("line"):
                try:
                    number = int(line.get("nr") or "")
                    instructions_covered = int(line.get("ci") or "0")
                    branches_missed, branches_covered = int(line.get("mb") or "0"), int(line.get("cb") or "0")
                except ValueError:
                    continue
                per_line[number] = (instructions_covered, branches_covered, branches_missed + branches_covered)
            result[matches[0]] = _file_coverage(matches[0], per_line, True)
    return result

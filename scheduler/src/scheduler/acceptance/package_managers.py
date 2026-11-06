"""Detect which package managers a project uses, from its manifest and lock files."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

from scheduler.acceptance.filetree import ProjectFile


@dataclass(frozen=True)
class PackageManagerHit:
    manager: str
    manifest: Path  # relative to the project root


@dataclass(frozen=True)
class _Rule:
    manager: str
    patterns: tuple[str, ...]  # in order of preference


_NATIVE_RULES = (
    _Rule("Conan", ("conanfile.txt", "conanfile.py")),
    _Rule("vcpkg", ("vcpkg.json",)),
)

# Each matching rule yields at most one hit per directory.
_RULES: dict[str, tuple[_Rule, ...]] = {
    "Java": (
        _Rule("Maven", ("pom.xml",)),
        _Rule("Gradle", ("settings.gradle.kts", "settings.gradle", "build.gradle.kts", "build.gradle")),
    ),
    "C#": (
        _Rule("NuGet", ("*.sln", "*.slnx", "*.csproj", "Directory.Packages.props", "packages.config")),
    ),
    "Rust": (_Rule("Cargo", ("Cargo.toml",)),),
    "C": _NATIVE_RULES,
    "C++": _NATIVE_RULES,
    # Tools with their own lock or manifest file. Plain pip is the fallback below.
    "Python": (
        _Rule("Poetry", ("poetry.lock",)),
        _Rule("uv", ("uv.lock",)),
        _Rule("PDM", ("pdm.lock",)),
        _Rule("Pipenv", ("Pipfile",)),
        _Rule("Conda", ("environment.yml", "environment.yaml")),
    ),
}

_PIP_RULE = _Rule("pip", ("pyproject.toml", "setup.py", "setup.cfg", "requirements*.txt"))
_POETRY_SECTION = re.compile(r"^\[tool\.poetry[\].]", re.MULTILINE)

_NODE_LANGUAGES = ("JavaScript", "TypeScript")
_NODE_LOCKFILES = (
    ("package-lock.json", "npm"),
    ("npm-shrinkwrap.json", "npm"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "Yarn"),
    ("bun.lock", "Bun"),
    ("bun.lockb", "Bun"),
)


def marker_files(language: str) -> tuple[str, ...]:
    """File names that count as package manager markers for a language."""
    if language in _NODE_LANGUAGES:
        return ("package.json",)
    rules = _RULES.get(language, ())
    if language == "Python":
        rules = (_PIP_RULE, *rules)
    return tuple(pattern for rule in rules for pattern in rule.patterns)


def detect_package_managers(root: Path, files: Iterable[ProjectFile], language: str) -> tuple[PackageManagerHit, ...]:
    """Find the package managers of a language, root directory hits first."""
    by_dir: dict[Path, set[str]] = defaultdict(set)
    for file in files:
        by_dir[file.path.parent].add(file.path.name)

    if language in _NODE_LANGUAGES:
        hits = _detect_node(by_dir)
    elif language == "Python":
        hits = _detect_python(root, by_dir)
    else:
        hits = [
            PackageManagerHit(rule.manager, directory / name)
            for directory, names in by_dir.items()
            for rule in _RULES.get(language, ())
            if (name := _first_match(names, rule.patterns))
        ]
    return tuple(sorted(hits, key=lambda hit: (len(hit.manifest.parts), hit.manifest.as_posix(), hit.manager)))


def _first_match(names: set[str], patterns: tuple[str, ...]) -> str | None:
    for pattern in patterns:
        matches = sorted(name for name in names if fnmatchcase(name, pattern))
        if matches:
            return matches[0]
    return None


def _detect_python(root: Path, by_dir: dict[Path, set[str]]) -> list[PackageManagerHit]:
    hits: list[PackageManagerHit] = []
    for directory, names in by_dir.items():
        found = [
            PackageManagerHit(rule.manager, directory / name)
            for rule in _RULES["Python"]
            if (name := _first_match(names, rule.patterns))
        ]
        if (
            "pyproject.toml" in names
            and not any(hit.manager == "Poetry" for hit in found)
            and _declares_poetry(root / directory / "pyproject.toml")
        ):
            found.append(PackageManagerHit("Poetry", directory / "pyproject.toml"))

        if not found:
            name = _first_match(names, _PIP_RULE.patterns)
            # e.g. requirements/base.txt, requirements/dev.txt
            if name is None and directory.name == "requirements":
                name = _first_match(names, ("*.txt",))
            if name:
                found.append(PackageManagerHit("pip", directory / name))
        hits.extend(found)
    return hits


def _declares_poetry(pyproject: Path) -> bool:
    try:
        return bool(_POETRY_SECTION.search(pyproject.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def _detect_node(by_dir: dict[Path, set[str]]) -> list[PackageManagerHit]:
    return [
        PackageManagerHit(_nearest_node_manager(directory, by_dir), directory / "package.json")
        for directory, names in by_dir.items()
        if "package.json" in names
    ]


def _nearest_node_manager(directory: Path, by_dir: dict[Path, set[str]]) -> str:
    """Manager of the nearest lockfile in the directory or above (workspaces); npm if none."""
    current = directory
    while True:
        names = by_dir.get(current, set())
        for lockfile, manager in _NODE_LOCKFILES:
            if lockfile in names:
                return manager
        if current == current.parent:
            return "npm"
        current = current.parent

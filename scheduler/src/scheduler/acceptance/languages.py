"""Detect the programming languages of a project by the size of its source files."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scheduler.acceptance.filetree import ProjectFile

SUPPORTED_LANGUAGES = ("C", "C++", "C#", "Java", "JavaScript", "TypeScript", "Python", "Rust")

_SUPPORTED_EXTENSIONS = {
    "C": (".c",),
    "C++": (".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".ipp"),
    "C#": (".cs",),
    "Java": (".java",),
    "JavaScript": (".js", ".mjs", ".cjs", ".jsx"),
    "TypeScript": (".ts", ".tsx", ".mts", ".cts"),
    "Python": (".py", ".pyi"),
    "Rust": (".rs",),
}

# Recognised so the abort message can name the language. Shell, HTML, CSS,
# SQL, config and markup files are deliberately not counted as languages.
_UNSUPPORTED_EXTENSIONS = {
    "Go": (".go",),
    "Kotlin": (".kt",),
    "Scala": (".scala",),
    "Swift": (".swift",),
    "Objective-C": (".m", ".mm"),
    "PHP": (".php",),
    "Ruby": (".rb",),
    "Dart": (".dart",),
    "Elixir": (".ex", ".exs"),
    "Haskell": (".hs",),
    "Lua": (".lua",),
    "R": (".r",),
    "Perl": (".pl", ".pm"),
}

EXTENSION_TO_LANGUAGE = {
    ext: language
    for table in (_SUPPORTED_EXTENSIONS, _UNSUPPORTED_EXTENSIONS)
    for language, extensions in table.items()
    for ext in extensions
}

# Usually generated or bundled code, not written by the project.
MAX_FILE_BYTES = 1_000_000
GENERATED_SUFFIXES = (".min.js", ".d.ts", ".d.mts", ".d.cts")

# Extensions shared with XML formats: Qt translation files (.ts) and tilesets
# of the Tiled map editor (.tsx). Such files are not TypeScript.
XML_SUFFIXES = (".ts", ".tsx")
XML_START = (b"<?xml", b"<!DOCTYPE", b"<TS", b"<tileset")


@dataclass(frozen=True)
class LanguageReport:
    # Bytes of source code per language, largest first.
    bytes_by_language: Mapping[str, int]

    @property
    def total_bytes(self) -> int:
        return sum(self.bytes_by_language.values())

    @property
    def primary(self) -> str | None:
        return next(iter(self.bytes_by_language), None)

    @property
    def primary_supported(self) -> bool:
        return self.primary in SUPPORTED_LANGUAGES

    @property
    def others(self) -> tuple[str, ...]:
        """All non-primary languages with source code, however small their share."""
        return tuple(language for language in self.bytes_by_language if language != self.primary)

    @property
    def supported_others(self) -> tuple[str, ...]:
        return tuple(language for language in self.others if language in SUPPORTED_LANGUAGES)

    @property
    def unsupported_others(self) -> tuple[str, ...]:
        return tuple(language for language in self.others if language not in SUPPORTED_LANGUAGES)

    def share(self, language: str) -> float:
        total = self.total_bytes
        return self.bytes_by_language.get(language, 0) / total if total else 0.0


def detect_languages(files: Iterable[ProjectFile], root: Path | None = None) -> LanguageReport:
    """Bytes of source code per language. With root, .ts/.tsx files are checked for XML."""
    totals: Counter[str] = Counter()
    header_bytes = 0
    for file in files:
        name = file.path.name.lower()
        if file.size > MAX_FILE_BYTES or name.endswith(GENERATED_SUFFIXES):
            continue
        suffix = file.path.suffix.lower()
        if root is not None and suffix in XML_SUFFIXES and _is_xml(root / file.path):
            continue
        if suffix == ".h":
            header_bytes += file.size
        elif language := EXTENSION_TO_LANGUAGE.get(suffix):
            totals[language] += file.size

    # .h is shared by C, C++ and Objective-C: attribute it to whichever has sources.
    if header_bytes:
        for language in ("C++", "Objective-C", "C"):
            if totals[language] or language == "C":
                totals[language] += header_bytes
                break

    ordered = sorted((+totals).items(), key=lambda item: (-item[1], item[0]))
    return LanguageReport(dict(ordered))


def _is_xml(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            start = handle.read(256).lstrip(b"\xef\xbb\xbf \t\r\n")
    except OSError:
        return False
    return start.startswith(XML_START)

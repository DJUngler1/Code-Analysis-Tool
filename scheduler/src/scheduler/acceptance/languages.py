"""Detect the programming languages of a project by the size of its source files."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scheduler.acceptance.filetree import ProjectFile
from scheduler.acceptance.generated import exclusion_reason

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

# .h is shared by C, C++ and Objective-C. Decided per file with GitHub
# Linguist's heuristic (heuristics.yml: named patterns "objectivec" and "cpp",
# otherwise C).
_OBJECTIVE_C = re.compile(
    r"^\s*(@(interface|class|protocol|property|end|synchronised|selector|implementation)\b|#import\s+.+\.h[\">])",
    re.MULTILINE,
)
_CPP = re.compile(
    "|".join(
        (
            r"^\s*#\s*include <(cstdint|string|vector|map|list|array|bitset|queue|stack|forward_list"
            r"|unordered_map|unordered_set|(i|o|io)stream)>",
            r"^\s*template\s*<",
            r"^[ \t]*(try|constexpr)",
            r"^[ \t]*catch\s*\(",
            r"^[ \t]*(class|(using[ \t]+)?namespace)\s+\w+",
            r"^[ \t]*(private|public|protected):$",
            r"__has_cpp_attribute|__cplusplus >",
            r"std::\w+",
        )
    ),
    re.MULTILINE,
)


@dataclass(frozen=True)
class LanguageReport:
    # Bytes of source code per language, largest first.
    bytes_by_language: Mapping[str, int]
    # Source files not counted (generated, minified, ...): path -> reason.
    excluded: Mapping[str, str] = field(default_factory=dict)

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


def detect_languages(files: Iterable[ProjectFile], root: Path) -> LanguageReport:
    """Bytes of source code per language, without generated files (acceptance/generated.py)."""
    totals: Counter[str] = Counter()
    excluded: dict[str, str] = {}
    for file in files:
        suffix = file.path.suffix.lower()
        if suffix != ".h" and suffix not in EXTENSION_TO_LANGUAGE:
            continue
        if reason := exclusion_reason(root / file.path):
            excluded[file.path.as_posix()] = reason
            continue
        language = header_language(root / file.path) if suffix == ".h" else EXTENSION_TO_LANGUAGE[suffix]
        totals[language] += file.size

    ordered = sorted((+totals).items(), key=lambda item: (-item[1], item[0]))
    return LanguageReport(dict(ordered), excluded)


def header_language(path: Path) -> str:
    """Objective-C, C++ or C for a .h file (Linguist's heuristic)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "C"
    if _OBJECTIVE_C.search(text):
        return "Objective-C"
    if _CPP.search(text):
        return "C++"
    return "C"

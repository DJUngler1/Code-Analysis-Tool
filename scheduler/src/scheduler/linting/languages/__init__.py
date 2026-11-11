"""One module per language group; each exposes run(ctx) -> list[ToolRun]."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scheduler.linting.languages import cpp, csharp, java, javascript, python, rust
from scheduler.linting.model import ToolRun
from scheduler.linting.session import LintContext


@dataclass(frozen=True)
class LanguageGroup:
    name: str
    languages: tuple[str, ...]  # language names of step 1
    image: str
    suffixes: tuple[str, ...]  # files the group's tools work on
    run: Callable[[LintContext], list[ToolRun]]


GROUPS = (
    LanguageGroup(cpp.LANGUAGE, ("C", "C++"), cpp.IMAGE, cpp.SUFFIXES, cpp.run),
    LanguageGroup(csharp.LANGUAGE, ("C#",), csharp.IMAGE, csharp.SUFFIXES, csharp.run),
    LanguageGroup(java.LANGUAGE, ("Java",), java.IMAGE, java.SUFFIXES, java.run),
    LanguageGroup(javascript.LANGUAGE, ("JavaScript", "TypeScript"), javascript.IMAGE, javascript.SUFFIXES,
                  javascript.run),
    LanguageGroup(python.LANGUAGE, ("Python",), python.IMAGE, python.SUFFIXES, python.run),
    LanguageGroup(rust.LANGUAGE, ("Rust",), rust.IMAGE, rust.SUFFIXES, rust.run),
)


def groups_for(languages: tuple[str, ...]) -> list[LanguageGroup]:
    """Groups of the given step-1 languages, in the order of the languages (primary first)."""
    selected: list[LanguageGroup] = []
    for language in languages:
        group = next((group for group in GROUPS if language in group.languages), None)
        if group is not None and group not in selected:
            selected.append(group)
    return selected

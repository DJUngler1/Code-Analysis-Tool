"""The prompts of step 6 (Markdown files in evaluation/prompts/).

Each file starts with an introduction for humans that is not sent. The `## `
sections below it are the parts the tool uses; placeholders are written
`{{NAME}}`.

Decision prompts: `## Context` (shared input), `## Question` (per item),
`## Labels` with one `### LABEL` section per label (the first is the default).
Report prompts: `## System` (system message), `## Entries` and `## Summary`
(user messages for the two kinds of request).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class PromptError(Exception):
    """A prompt file is missing a section or a placeholder has no value."""


@dataclass(frozen=True)
class DecisionPrompt:
    name: str
    context: str
    question: str
    labels: dict[str, str]

    @property
    def default(self) -> str:
        return next(iter(self.labels))


def read_prompt(name: str) -> str:
    try:
        return resources.files("scheduler.evaluation").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PromptError(f"prompt {name}.md is missing") from exc


def sections(text: str, level: int = 2) -> dict[str, str]:
    """`## Title` -> body, for the given heading level; the text before the first heading is left out."""
    marker = "#" * level + " "
    result: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(marker):
            if current is not None:
                result[current] = "\n".join(lines).strip()
            current, lines = line[len(marker):].strip(), []
        elif current is not None:
            lines.append(line)
    if current is not None:
        result[current] = "\n".join(lines).strip()
    return result


def decision_prompt(name: str) -> DecisionPrompt:
    parts = sections(read_prompt(name))
    missing = [title for title in ("Context", "Question", "Labels") if title not in parts]
    if missing:
        raise PromptError(f"prompt {name}.md has no section {', '.join(missing)}")
    labels = {label: " ".join(body.split()) for label, body in sections(parts["Labels"], 3).items()}
    if len(labels) < 2:
        raise PromptError(f"prompt {name}.md needs at least two labels under ## Labels")
    return DecisionPrompt(name, parts["Context"], parts["Question"], labels)


def report_prompt(name: str) -> dict[str, str]:
    parts = sections(read_prompt(name))
    missing = [title for title in ("System", "Entries", "Summary") if title not in parts]
    if missing:
        raise PromptError(f"prompt {name}.md has no section {', '.join(missing)}")
    return parts


def render(template: str, **values: str) -> str:
    def value(match: re.Match) -> str:
        if match.group(1) not in values:
            raise PromptError(f"no value for {{{{{match.group(1)}}}}}")
        return values[match.group(1)]

    return _PLACEHOLDER.sub(value, template)

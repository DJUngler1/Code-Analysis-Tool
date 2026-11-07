"""Data model of step 2: tool runs, findings and the overall result."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Status(StrEnum):
    OK = "ok"  # the tool ran; findings may exist
    PARTIAL = "partial"  # the tool ran, but some files were not processed
    FAILED = "failed"  # the tool could not run
    SKIPPED = "skipped"  # nothing to do, e.g. no files of this language


@dataclass(frozen=True)
class Finding:
    language: str
    tool: str
    rule: str
    severity: str  # "error", "warning" or "note"
    message: str
    file: str | None  # relative to the project, "/"-separated
    line: int | None = None
    column: int | None = None
    # True when the tool already applied the fix (dotnet format reports applied
    # fixes). All other findings are what remains after the automatic fixes.
    fixed: bool = False


@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    exit_code: int | None  # None: timed out or could not be started
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    """What a tool body reports back; run_tool adds timing and changes."""

    status: Status
    findings: tuple[Finding, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass
class ToolRun:
    language: str
    tool: str
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    changed_files: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder


@dataclass
class LintingResult:
    run_dir: Path | None
    tool_runs: list[ToolRun] = field(default_factory=list)
    # Problems that prevented (part of) the step: Docker or an image missing,
    # config files that could not be moved back, ...
    errors: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[Finding]:
        return [finding for run in self.tool_runs for finding in run.findings]

    @property
    def complete(self) -> bool:
        return not self.errors and all(run.status is not Status.FAILED for run in self.tool_runs)

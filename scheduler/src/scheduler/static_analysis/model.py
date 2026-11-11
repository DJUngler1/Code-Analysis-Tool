"""Data model of step 3: analysis tool runs and their findings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scheduler.linting.model import CommandResult, Status


@dataclass(frozen=True)
class AnalysisFinding:
    tool: str
    ruleset: str | None  # rule set of config/opengrep.toml the rule comes from
    rule: str  # rule id within the rule set, e.g. "python.flask.security.injection.subprocess-injection"
    severity: str  # "error", "warning" or "note", as in step 2
    tool_severity: str  # as the tool reports it, e.g. "ERROR", "WARNING", "INFO"
    message: str
    language: str | None  # language of step 1, from the file extension
    file: str | None  # relative to the project, "/"-separated
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    # From the rule's metadata (Semgrep rule format), for step 6.
    category: str | None = None  # "security", "correctness", "best-practice", ...
    subcategory: tuple[str, ...] = ()  # "vuln" (likely exploitable) or "audit" (needs review)
    cwe: tuple[str, ...] = ()
    owasp: tuple[str, ...] = ()
    confidence: str | None = None
    likelihood: str | None = None
    impact: str | None = None
    fingerprint: str | None = None


@dataclass
class AnalysisRun:
    tool: str
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    findings: list[AnalysisFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder
    # Rule sets used: name, repository, commit, license, folders.
    rulesets: list[dict] = field(default_factory=list)
    rules_run: int | None = None  # rules that applied to the scanned files
    files_scanned: int | None = None


@dataclass
class AnalysisResult:
    run_dir: Path | None
    tool_runs: list[AnalysisRun] = field(default_factory=list)
    # Problems that prevented the step: configuration invalid, Docker or the
    # image missing, ...
    errors: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[AnalysisFinding]:
        return [finding for run in self.tool_runs for finding in run.findings]

    @property
    def complete(self) -> bool:
        return not self.errors and all(run.status is not Status.FAILED for run in self.tool_runs)

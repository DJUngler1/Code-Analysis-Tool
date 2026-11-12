"""Data model of step 4: dependency checks, their findings and the coverage per manifest."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scheduler.linting.model import CommandResult, Status

# CVSS v3.x qualitative severity rating scale (FIRST, CVSS v3.1 specification).
SEVERITIES = ("critical", "high", "medium", "low", "unknown")
# Gradle scopes, from shipped to not shipped (lockfiles.gradle_scope).
SCOPES = ("runtime", "compile", "test", "tooling")


@dataclass(frozen=True)
class DependencyFinding:
    tool: str  # "osv-scanner", "npm audit", "cargo audit", ...
    ecosystem: str  # "npm", "PyPI", "crates.io", "NuGet", "Maven", ...
    package: str
    version: str | None
    id: str  # advisory id, e.g. "GHSA-…", "PYSEC-…", "RUSTSEC-…"
    aliases: tuple[str, ...] = ()  # other ids of the same advisory (CVE, GHSA, …)
    severity: str = "unknown"  # one of SEVERITIES
    cvss_score: float | None = None
    cvss_vector: str | None = None
    summary: str = ""
    fixed_versions: tuple[str, ...] = ()
    url: str | None = None
    # Lock file or manifest the package was found in, relative to the project.
    manifest: str | None = None
    manager: str | None = None  # package manager of step 1, e.g. "npm", "Cargo"
    # Set when the severity comes from another tool's finding of the same advisory.
    severity_source: str | None = None
    # Gradle only: the lock file's configurations that contain the package, and the
    # most shipped of their scopes (one of SCOPES).
    configurations: tuple[str, ...] = ()
    scope: str | None = None


@dataclass
class DependencyRun:
    tool: str
    status: Status
    version: str | None = None
    commands: list[CommandResult] = field(default_factory=list)
    duration_seconds: float = 0.0
    findings: list[DependencyFinding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    output_dir: str | None = None  # raw output, relative to the step folder
    manifest: str | None = None  # the manifest an audit ran for
    manager: str | None = None


@dataclass
class Coverage:
    """How one manifest of step 1 was checked."""

    manager: str
    manifest: str  # relative to the project
    lockfile: str | None  # lock file OSV-Scanner reads, relative to the project
    lockfile_state: str  # "committed", "generated", "partial", "failed", "not needed", "not supported"
    osv: bool  # OSV-Scanner can read it
    audit: str | None  # audit tool, None if the package manager has none
    notes: list[str] = field(default_factory=list)


@dataclass
class DependencyResult:
    run_dir: Path | None
    tool_runs: list[DependencyRun] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def findings(self) -> list[DependencyFinding]:
        return [finding for run in self.tool_runs for finding in run.findings]

    @property
    def incomplete_lockfiles(self) -> list[Coverage]:
        """Lock files that could not be generated, or only for part of the dependencies."""
        return [entry for entry in self.coverage if entry.lockfile_state in ("failed", "partial")]

    @property
    def complete(self) -> bool:
        return (not self.errors and not self.incomplete_lockfiles
                and all(run.status is not Status.FAILED for run in self.tool_runs))


def severity_from_cvss(score: float | None) -> str:
    """CVSS v3.x rating: 0.1–3.9 low, 4.0–6.9 medium, 7.0–8.9 high, 9.0–10.0 critical."""
    if score is None or score <= 0:
        return "unknown"
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


def severity_from_label(label: object) -> str:
    """Advisory labels of npm, GitHub, NuGet, …: "moderate" → "medium", "info" → "low"."""
    text = str(label or "").strip().lower()
    return {"critical": "critical", "high": "high", "moderate": "medium", "medium": "medium",
            "low": "low", "info": "low"}.get(text, "unknown")

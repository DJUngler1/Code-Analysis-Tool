"""Stage 1: OSV-Scanner on the copy of the project, with its JSON report.

OSV-Scanner reads lock files (and a few manifests such as Maven's pom.xml,
which it resolves transitively via deps.dev) and queries the OSV.dev API.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from scheduler.dependencies.context import DependencyContext
from scheduler.dependencies.lockfiles import add_gradle_scopes
from scheduler.dependencies.model import DependencyFinding, DependencyRun, severity_from_cvss, severity_from_label
from scheduler.linting.model import Status
from scheduler.linting.session import last_lines

TOOL = "osv-scanner"
IMAGE = "code-analysis/osv-scanner:latest"
OUTPUT_DIR = "osv-scanner"
REPORT = "osv-scanner.json"
EXIT_NO_PACKAGES = 128  # "No packages found"


def run_osv(ctx: DependencyContext, managers: Mapping[str, str]) -> DependencyRun:
    """`managers` maps lock files (relative to the project) to their package manager."""
    out_dir = ctx.step_dir / OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    report = out_dir / REPORT
    run = DependencyRun(tool=TOOL, status=Status.FAILED, output_dir=OUTPUT_DIR,
                        version=ctx.runner.image_versions(IMAGE).get(TOOL))
    started = time.monotonic()
    result = ctx.run(IMAGE, ["osv-scanner", "scan", "source", "--recursive", "--format", "json",
                             "--output-file", str(report), str(ctx.workspace)],
                     folder=Path("."), log_dir=out_dir, label="osv-scanner")
    run.commands.append(result)
    run.duration_seconds = round(time.monotonic() - started, 1)
    if result.exit_code == EXIT_NO_PACKAGES:
        run.status = Status.SKIPPED
        run.notes.append("OSV-Scanner found no packages (no lock file or manifest it can read)")
        return run
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if result.exit_code not in (0, 1) or not isinstance(data, dict):
        run.notes.append(f"OSV-Scanner failed (exit code {result.exit_code}): {last_lines(result.stderr)}")
        return run
    run.findings = add_gradle_scopes(parse_osv(data, workspace=ctx.workspace, managers=managers), ctx.workspace)
    run.status = Status.OK
    return run


def parse_osv(data: Mapping, *, workspace: Path, managers: Mapping[str, str]) -> list[DependencyFinding]:
    """One finding per vulnerability group (ids OSV knows to be the same advisory) and package."""
    findings: list[DependencyFinding] = []
    for result in data.get("results", []):
        manifest = _relative(result.get("source", {}).get("path"), workspace)
        for entry in result.get("packages", []):
            package = entry.get("package", {})
            records = {record.get("id"): record for record in entry.get("vulnerabilities", [])}
            groups = entry.get("groups") or [{"ids": [key]} for key in records]
            for group in groups:
                ids = [str(key) for key in group.get("ids", []) if key]
                if not ids:
                    continue
                group_records = [records[key] for key in ids if key in records]
                # The GitHub advisory has a summary and a severity label; PYSEC/RUSTSEC often not.
                main = next((record for record in group_records if str(record.get("id")).startswith("GHSA-")),
                            group_records[0] if group_records else {})
                score = _score(group.get("max_severity"))
                label = next((record.get("database_specific", {}).get("severity") for record in group_records
                              if record.get("database_specific", {}).get("severity")), None)
                aliases = {str(alias) for alias in group.get("aliases", [])} | set(ids)
                findings.append(DependencyFinding(
                    tool=TOOL,
                    ecosystem=str(package.get("ecosystem") or "GIT"),
                    package=str(package.get("name") or package.get("commit") or "unknown"),
                    version=package.get("version") or package.get("commit"),
                    id=ids[0],
                    aliases=tuple(sorted(aliases - {ids[0]})),
                    severity=severity_from_cvss(score) if score else severity_from_label(label),
                    cvss_score=score,
                    cvss_vector=_vector(group_records),
                    summary=str(main.get("summary") or "").strip(),
                    fixed_versions=_fixed(group_records, package),
                    url=f"https://osv.dev/vulnerability/{ids[0]}",
                    manifest=manifest,
                    manager=managers.get(manifest or ""),
                ))
    return findings


def _relative(path: object, workspace: Path) -> str | None:
    if not path:
        return None
    try:
        return Path(str(path)).relative_to(workspace).as_posix()
    except ValueError:
        return str(path)


def _score(value: object) -> float | None:
    try:
        score = float(str(value))
    except ValueError:
        return None
    return score if score > 0 else None


def _vector(records: Iterable[Mapping]) -> str | None:
    vectors = [str(entry.get("score")) for record in records for entry in record.get("severity", [])
               if str(entry.get("type", "")).startswith("CVSS")]
    return next((vector for vector in vectors if vector.startswith("CVSS:3")), vectors[0] if vectors else None)


def _fixed(records: Iterable[Mapping], package: Mapping) -> tuple[str, ...]:
    fixed: set[str] = set()
    for record in records:
        for affected in record.get("affected", []):
            if affected.get("package", {}).get("name") != package.get("name"):
                continue
            for version_range in affected.get("ranges", []):
                fixed |= {str(event["fixed"]) for event in version_range.get("events", []) if "fixed" in event}
    return tuple(sorted(fixed))

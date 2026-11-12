"""Step 4 of the workflow: check the dependencies for known vulnerabilities.

Stage 1 is OSV-Scanner, stage 2 the audit of each package manager step 1
found. Both work on a copy of the project, in which missing lock files are
generated first (lockfiles.py). Results go into the run folder:

<OUTPUT_DIR>/<project>/<date_time>/
  step4-dependencies/
    summary.json      per tool: version, commands, status, findings; coverage per manifest
    findings.json     normalized findings of all tools
    osv-scanner/      OSV-Scanner's JSON report and logs
    audits/<nn>-<manager>/   raw output of each audit
    lockfiles/        lock files generated for the analysis
    lockfile-logs/    logs of the lock commands

The tools query online services (OSV.dev, deps.dev, the package registries):
package names and versions of the project leave the machine.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.dependencies import audits, lockfiles, osv
from scheduler.dependencies.context import DependencyContext
from scheduler.dependencies.model import SCOPES, SEVERITIES, DependencyFinding, DependencyResult, DependencyRun
from scheduler.linting.docker import DockerError, DockerRunner, ToolRunner
from scheduler.linting.model import Status
from scheduler.workspace import prepare_workspace

STEP_DIR = "step4-dependencies"
MAX_PARALLEL = 4


def run_dependencies(
    acceptance: AcceptanceResult,
    settings: Settings,
    run_dir: Path,
    *,
    runner: ToolRunner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out: Callable[[str], None] = print,
) -> DependencyResult:
    started = now()
    step_dir = run_dir / STEP_DIR
    step_dir.mkdir(parents=True, exist_ok=True)
    result = DependencyResult(run_dir)
    lock = threading.Lock()

    def locked_out(line: str) -> None:
        with lock:
            out(line)

    workspace = step_dir / "workspace"
    try:
        runner = runner or DockerRunner(settings.tools_network)
        runner.check_available()
        ctx = DependencyContext(acceptance.project, workspace, step_dir, acceptance, runner, settings, locked_out)
        for image in _images(ctx, settings):
            runner.ensure_image(image)
    except DockerError as exc:
        result.errors.append(str(exc))
        _write_results(result, acceptance, started, now())
        return result

    try:
        prepare_workspace(acceptance.project, workspace)
        token = settings.conan_audit_token
        result.coverage = lockfiles.prepare_lockfiles(ctx, conan_token=bool(token))
        managers = {}
        for entry in result.coverage:  # manifest and lock files -> package manager
            for path in (entry.manifest, *(entry.lockfile or "").split(", ")):
                if path:
                    managers[path] = entry.manager
        jobs = audits.plan_audits(ctx, result.coverage, token)
        tasks: list[tuple[str, Callable[[], DependencyRun]]] = [("OSV-Scanner", lambda: osv.run_osv(ctx, managers))]
        for index, job in enumerate(jobs, start=1):
            tasks.append((f"{job.tool.split(' --')[0]} ({job.unit.manifest.as_posix()})",
                          lambda job=job, index=index: audits.run_audit(ctx, job, index)))
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            futures = [pool.submit(_run_tool, ctx, label, body) for label, body in tasks]
            result.tool_runs = [future.result() for future in futures]
        add_osv_details(result.tool_runs)
        for entry in result.coverage:
            if entry.manager == "Conan" and entry.audit is None:
                entry.notes.append("conan audit needs CONAN_AUDIT_TOKEN (free registration at audit.conan.io)")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    _write_results(result, acceptance, started, now())
    return result


def add_osv_details(runs: list[DependencyRun]) -> None:
    """Complete audit findings from OSV's finding of the same advisory and package.

    - aliases: npm audit, for example, names only the GitHub advisory, OSV also the CVE;
    - severity: pip-audit and cargo-audit report none.
    """
    known: dict[tuple[str, str], DependencyFinding] = {}
    for run in runs:
        if run.tool != osv.TOOL:
            continue
        for finding in run.findings:
            for advisory in (finding.id, *finding.aliases):
                known.setdefault((finding.package.lower(), advisory), finding)
    for run in runs:
        if run.tool == osv.TOOL:
            continue
        updated = []
        for finding in run.findings:
            match = next((known[(finding.package.lower(), advisory)] for advisory in (finding.id, *finding.aliases)
                          if (finding.package.lower(), advisory) in known), None)
            if match is not None:
                aliases = set(finding.aliases) | set(match.aliases) | {match.id}
                finding = replace(finding, aliases=tuple(sorted(aliases - {finding.id})))
                if finding.severity == "unknown" and match.severity != "unknown":
                    finding = replace(finding, severity=match.severity, cvss_score=match.cvss_score,
                                      cvss_vector=finding.cvss_vector or match.cvss_vector,
                                      severity_source=f"{osv.TOOL} ({match.id})")
            updated.append(finding)
        run.findings = updated


def _images(ctx: DependencyContext, settings: Settings) -> list[str]:
    managers = {manager for manager, _ in ctx.hits()}
    images = [osv.IMAGE]
    for wanted, image in (({"npm", "Yarn", "pnpm", "Bun"}, lockfiles.NODE_IMAGE),
                          ({"Cargo"}, lockfiles.RUST_IMAGE),
                          ({"NuGet"}, lockfiles.DOTNET_IMAGE),
                          ({"Gradle"}, lockfiles.JAVA_IMAGE),
                          ({"Conan"}, lockfiles.CPP_IMAGE),
                          ({"pip", "Poetry", "uv", "PDM", "Pipenv"}, lockfiles.PYTHON_IMAGE)):
        if managers & wanted:
            images.append(image)
    return images


def _run_tool(ctx: DependencyContext, label: str, body: Callable[[], DependencyRun]) -> DependencyRun:
    ctx.out(f"  {label:<40} running ...")
    started = time.monotonic()
    try:
        run = body()
    except Exception as exc:  # noqa: BLE001 – a broken tool must not stop the others
        (ctx.step_dir / f"internal-error-{abs(hash(label))}.txt").write_text(traceback.format_exc(), encoding="utf-8")
        run = DependencyRun(tool=label, status=Status.FAILED, notes=[f"internal error: {exc}"])
    if not run.duration_seconds:
        run.duration_seconds = round(time.monotonic() - started, 1)
    ctx.out(f"  {label:<40} {describe(run)}")
    for note in run.notes:
        ctx.out(f"  {'':<40}   note: {note}")
    return run


def describe(run: DependencyRun) -> str:
    parts = [run.status.value]
    if run.status is not Status.SKIPPED:
        parts.append(f"{len(run.findings)} findings")
        parts.append(f"{run.duration_seconds:.0f} s")
    return ", ".join(parts)


def _write_results(result: DependencyResult, acceptance: AcceptanceResult, started: datetime,
                   finished: datetime) -> None:
    assert result.run_dir is not None
    step_dir = result.run_dir / STEP_DIR
    summary = {
        "step": "4 – dependencies",
        "project": str(acceptance.project),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "complete": result.complete,
        "errors": result.errors,
        "coverage": [asdict(entry) for entry in result.coverage],
        "tools": [_tool_summary(run) for run in result.tool_runs],
        "findings_by_severity": _count(finding.severity for finding in result.findings),
        "gradle_findings_by_scope": scope_counts(result.findings),
    }
    _write_json(step_dir / "summary.json", summary)
    _write_json(step_dir / "findings.json", [asdict(finding) for finding in result.findings])


def _tool_summary(run: DependencyRun) -> dict:
    return {
        "tool": run.tool,
        "manager": run.manager,
        "manifest": run.manifest,
        "version": run.version,
        "status": run.status.value,
        "duration_seconds": run.duration_seconds,
        "commands": [
            {
                "command": list(command.command),
                "exit_code": command.exit_code,
                "duration_seconds": round(command.duration_seconds, 1),
                "timed_out": command.timed_out,
            }
            for command in run.commands
        ],
        "findings": len(run.findings),
        "findings_by_severity": _count(finding.severity for finding in run.findings),
        "notes": run.notes,
        "output_dir": run.output_dir,
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    counts = {severity: 0 for severity in SEVERITIES}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return {severity: count for severity, count in counts.items() if count}


def scope_counts(findings: Iterable[DependencyFinding]) -> dict[str, int]:
    """Gradle findings per scope (runtime, compile, test, tooling); others have no scope."""
    counts = Counter(finding.scope for finding in findings if finding.scope)
    return {scope: counts[scope] for scope in SCOPES if counts[scope]}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

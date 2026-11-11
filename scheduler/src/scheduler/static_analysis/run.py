"""Step 3 of the workflow: static code analysis, save the results.

Runs on the working tree as step 2 left it (formatted, uncommitted), so line
numbers match `git diff` and the findings of step 2. Results go into the run
folder of step 2:

<OUTPUT_DIR>/<project>/<date_time>/
  step3-static-analysis/
    summary.json      one entry per tool: version, command, status, rule sets, ...
    findings.json     normalized findings of all tools
    opengrep/         OpenGrep's JSON and SARIF reports, stdout/stderr
    sonarqube/        SonarQube: scanner parameters and logs, Web API answers (api/)

OpenGrep and SonarQube run in parallel; a tool that fails doesn't stop the other.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.linting.docker import DockerError, DockerRunner, ToolRunner
from scheduler.linting.model import Status
from scheduler.linting.run import mount_root
from scheduler.static_analysis import opengrep, sonarqube
from scheduler.static_analysis.context import AnalysisContext
from scheduler.static_analysis.model import AnalysisResult, AnalysisRun

STEP_DIR = "step3-static-analysis"
# Label, tool name (summary.json) and implementation; they run in parallel.
TOOLS: tuple[tuple[str, Callable[[AnalysisContext], AnalysisRun]], ...] = (
    ("OpenGrep", opengrep.run_opengrep),
    ("SonarQube", sonarqube.run_sonarqube),
)


def run_static_analysis(
    acceptance: AcceptanceResult,
    settings: Settings,
    run_dir: Path,
    *,
    runner: ToolRunner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out: Callable[[str], None] = print,
    tools: Sequence[str] | None = None,
    sonar_api: Callable | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AnalysisResult:
    """Run the tools of step 3 in parallel; `tools` selects some by label (tests)."""
    started = now()
    step_dir = run_dir / STEP_DIR
    step_dir.mkdir(parents=True, exist_ok=True)
    result = AnalysisResult(run_dir)
    try:
        runner = runner or DockerRunner(settings.tools_network)
        runner.check_available()
    except DockerError as exc:
        result.errors.append(str(exc))
        _write_results(result, acceptance, started, now())
        return result

    lock = threading.Lock()

    def locked_out(line: str) -> None:
        with lock:
            out(line)

    ctx = AnalysisContext(
        project=acceptance.project,
        mount_root=mount_root(acceptance.project, settings.projects_dir),
        step_dir=step_dir,
        acceptance=acceptance,
        runner=runner,
        settings=settings,
        out=locked_out,
        sonar_api=sonar_api,
        sleep=sleep,
    )
    selected = [(label, body) for label, body in TOOLS if tools is None or label in tools]
    with ThreadPoolExecutor(max_workers=len(selected) or 1) as pool:
        futures = [pool.submit(_run_tool, ctx, label, body) for label, body in selected]
        result.tool_runs = [future.result() for future in futures]
    _write_results(result, acceptance, started, now())
    return result


def _run_tool(ctx: AnalysisContext, label: str, body: Callable[[AnalysisContext], AnalysisRun]) -> AnalysisRun:
    ctx.out(f"  {label:<22} running ...")
    started = time.monotonic()
    try:
        run = body(ctx)
    except Exception as exc:  # noqa: BLE001 – a broken tool must not stop the others
        (ctx.step_dir / f"{label.lower()}-internal-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        run = AnalysisRun(tool=label.lower(), status=Status.FAILED, notes=[f"internal error: {exc}"])
    if not run.duration_seconds:
        run.duration_seconds = round(time.monotonic() - started, 1)
    ctx.out(f"  {label:<22} {describe(run)}")
    for note in run.notes:
        ctx.out(f"  {'':<22}   note: {note}")
    return run


def describe(run: AnalysisRun) -> str:
    parts = [run.status.value]
    if run.status is not Status.SKIPPED:
        parts.append(f"{len(run.findings)} findings")
        if run.files_scanned is not None:
            parts.append(f"{run.files_scanned} files")
        parts.append(f"{run.duration_seconds:.0f} s")
    return ", ".join(parts)


def _write_results(result: AnalysisResult, acceptance: AcceptanceResult, started: datetime,
                   finished: datetime) -> None:
    assert result.run_dir is not None
    step_dir = result.run_dir / STEP_DIR
    languages = acceptance.languages
    summary = {
        "step": "3 – static code analysis",
        "project": str(acceptance.project),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "languages": [language for language in (languages.primary, *languages.supported_others) if language],
        "complete": result.complete,
        "errors": result.errors,
        "tools": [_tool_summary(run) for run in result.tool_runs],
    }
    _write_json(step_dir / "summary.json", summary)
    _write_json(step_dir / "findings.json", [asdict(finding) for finding in result.findings])


def _tool_summary(run: AnalysisRun) -> dict:
    return {
        "tool": run.tool,
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
        "rulesets": run.rulesets,
        "rules_run": run.rules_run,
        "files_scanned": run.files_scanned,
        "findings": len(run.findings),
        "findings_by_severity": _count(finding.severity for finding in run.findings),
        "findings_by_category": _count(finding.category or "none" for finding in run.findings),
        "measures": run.measures,
        "details": run.details,
        "notes": run.notes,
        "output_dir": run.output_dir,
    }


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

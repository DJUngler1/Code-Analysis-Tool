"""Step 2 of the workflow: run the linters and formatters, save the results.

The formatters change the project's files; the changes stay uncommitted in the
working tree (step 1 guaranteed a clean tree, so `git diff` shows exactly what
the tools did). Everything else goes into a run folder for step 6:

<OUTPUT_DIR>/<project>/<date_time>/
  run.json                 project, commit, start time, step 1 result
  step2-linting/
    summary.json           one entry per tool
    findings.json          normalized findings of all tools
    changes.patch          all changes made by the tools
    excluded-files.json    generated files, neither formatted nor linted (with the reason)
    <language>/<tool>/     raw output and the tool's own changes.patch
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.linting.docker import DockerError, DockerRunner, ToolRunner
from scheduler.linting.excluded import ExcludedFiles
from scheduler.linting.languages import groups_for
from scheduler.linting.model import LintingResult, ToolRun
from scheduler.linting.session import LintContext
from scheduler.linting.snapshots import SnapshotError, Snapshotter, git_toplevel, head_commit, untracked_files

STEP_DIR = "step2-linting"


def run_linting(
    acceptance: AcceptanceResult,
    settings: Settings,
    *,
    runner: ToolRunner | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out: Callable[[str], None] = print,
) -> LintingResult:
    if settings.output_dir is None:
        return LintingResult(None, errors=["OUTPUT_DIR is not set (see docker/.env.example)."])

    project = acceptance.project
    started = now()
    run_dir = _new_run_dir(settings.output_dir, project, started)
    step_dir = run_dir / STEP_DIR
    step_dir.mkdir(parents=True)
    _write_json(run_dir / "run.json", _run_info(acceptance, started))

    result = LintingResult(run_dir)
    groups = groups_for((acceptance.languages.primary or "", *acceptance.languages.supported_others))
    group_names = [group.name for group in groups]
    runner = runner or DockerRunner(settings.tools_network)
    try:
        runner.check_available()
        for group in groups:
            runner.ensure_image(group.image)
        snapshots = Snapshotter(project)
    except (DockerError, SnapshotError) as exc:
        result.errors.append(str(exc))
        _write_results(result, project, started, now(), group_names)
        return result

    untracked_before = set(untracked_files(project))
    suffixes = tuple(suffix for group in groups for suffix in group.suffixes)
    excluded = ExcludedFiles.detect(project, suffixes, step_dir / "generated-originals")
    excluded.write_list(step_dir / "excluded-files.json")
    try:
        excluded.save()
    except OSError as exc:
        snapshots.close()
        result.errors.append(f"could not copy the generated files before the tools ran: {exc}")
        _write_results(result, project, started, now(), group_names)
        return result
    if len(excluded):
        out(f"  {len(excluded)} generated files are neither formatted nor linted (excluded-files.json)")
    ctx = LintContext(
        project=project,
        mount_root=mount_root(project, settings.projects_dir),
        step_dir=step_dir,
        acceptance=acceptance,
        runner=runner,
        snapshots=snapshots,
        errors=result.errors,
        out=out,
        excluded=excluded,
    )
    try:
        base = snapshots.base()
        for group in groups:
            result.tool_runs += group.run(ctx)
        final = snapshots.snapshot()
        result.changed_files = snapshots.changed_files(base, final)
        if result.changed_files:
            (step_dir / "changes.patch").write_text(snapshots.patch(base, final), encoding="utf-8")
    except SnapshotError as exc:
        result.errors.append(f"could not record the changes: {exc}")
    finally:
        snapshots.close()
        excluded.discard_backup()

    _remove_if_empty(step_dir / "hidden")  # all moved-out configuration files are back
    created = sorted(set(untracked_files(project)) - untracked_before)
    _write_results(result, project, started, now(), group_names, created, len(excluded))
    return result


def _new_run_dir(output_dir: Path, project: Path, started: datetime) -> Path:
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", project.name) or "project"
    # The container has no local time zone: name the folder in UTC, marked "Z".
    stamp = started.strftime("%Y-%m-%d_%H%M%S") + ("Z" if started.utcoffset() == timezone.utc.utcoffset(None) else "")
    base = output_dir / name / stamp
    candidate, counter = base, 1
    while candidate.exists():
        counter += 1
        candidate = base.with_name(f"{base.name}_{counter}")
    return candidate


def _remove_if_empty(folder: Path) -> None:
    if folder.is_dir() and not any(path.is_file() for path in folder.rglob("*")):
        shutil.rmtree(folder)


def mount_root(project: Path, projects_dir: Path | None) -> Path:
    """The git root if it lies inside PROJECTS_DIR (tools then see .git), else the project."""
    top = git_toplevel(project)
    if top is None:
        return project
    if projects_dir is not None and not top.is_relative_to(projects_dir):
        return project
    return top


def _run_info(acceptance: AcceptanceResult, started: datetime) -> dict:
    languages = acceptance.languages
    return {
        "project": str(acceptance.project),
        "commit": head_commit(acceptance.project),
        "started": started.isoformat(timespec="seconds"),
        "languages": {
            "primary": languages.primary,
            "others": list(languages.others),
            "shares": {language: round(languages.share(language), 4) for language in languages.bytes_by_language},
            "not_counted_files": len(languages.excluded),  # generated, minified, ...
        },
        "package_managers": {
            language: [{"manager": hit.manager, "manifest": hit.manifest.as_posix()} for hit in hits]
            for language, hits in acceptance.package_managers.items()
        },
    }


def _write_results(
    result: LintingResult,
    project: Path,
    started: datetime,
    finished: datetime,
    languages: list[str],
    created_files: list[str] | None = None,
    excluded_files: int = 0,
) -> None:
    assert result.run_dir is not None
    step_dir = result.run_dir / STEP_DIR
    summary = {
        "step": "2 – linters and formatters",
        "project": str(project),
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "languages": languages,
        "complete": result.complete,
        "errors": result.errors,
        "changed_files": result.changed_files,
        # Untracked files the tools created, e.g. build output that is not
        # covered by .gitignore (obj/, target/). Not part of changes.patch.
        "created_files": created_files or [],
        # Generated files: neither formatted nor linted, listed in excluded-files.json.
        "excluded_files": excluded_files,
        "tools": [_tool_summary(run) for run in result.tool_runs],
    }
    _write_json(step_dir / "summary.json", summary)
    _write_json(step_dir / "findings.json", [asdict(finding) for finding in result.findings])


def _tool_summary(run: ToolRun) -> dict:
    severities: dict[str, int] = {}
    for finding in run.findings:
        severities[finding.severity] = severities.get(finding.severity, 0) + 1
    return {
        "language": run.language,
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
        "findings": len(run.findings),
        "findings_by_severity": severities,
        "fixed_findings": sum(1 for finding in run.findings if finding.fixed),
        "changed_files": run.changed_files,
        "notes": run.notes,
        "output_dir": run.output_dir,
    }


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

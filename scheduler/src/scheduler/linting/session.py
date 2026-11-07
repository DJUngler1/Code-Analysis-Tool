"""Shared machinery of the language modules: context, one tool run, raw output."""

from __future__ import annotations

import re
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.linting.docker import ToolRunner
from scheduler.linting.model import CommandResult, Status, ToolOutcome, ToolRun
from scheduler.linting.snapshots import Snapshotter

DEFAULT_TIMEOUT_SECONDS = 30 * 60


@dataclass
class LintContext:
    project: Path
    # Mounted into the tool containers: the git repository root if the project
    # is a subfolder of one (inside PROJECTS_DIR), otherwise the project.
    mount_root: Path
    step_dir: Path  # <run>/step2-linting
    acceptance: AcceptanceResult
    runner: ToolRunner
    snapshots: Snapshotter
    errors: list[str] = field(default_factory=list)
    out: Callable[[str], None] = print

    @property
    def mounts(self) -> list[Path]:
        return [self.mount_root, self.step_dir]

    def manifests(self, *languages: str) -> list[Path]:
        """Package manager manifests found by step 1, relative to the project."""
        return [
            hit.manifest
            for language in languages
            for hit in self.acceptance.package_managers.get(language, ())
        ]


class ToolSession:
    """One tool of one language: runs its commands and keeps their raw output."""

    def __init__(self, ctx: LintContext, language: str, tool: str, image: str) -> None:
        self.ctx = ctx
        self.language = language
        self.tool = tool
        self.image = image
        self.dir = ctx.step_dir / slug(language) / slug(tool)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.commands: list[CommandResult] = []

    def run(
        self,
        command: Sequence[str],
        *,
        workdir: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        label: str | None = None,
    ) -> CommandResult:
        result = self.ctx.runner.run(
            self.image,
            command,
            workdir=workdir or self.ctx.project,
            mounts=self.ctx.mounts,
            env=env,
            timeout=timeout,
        )
        self.commands.append(result)
        name = label or f"command-{len(self.commands)}"
        (self.dir / f"{name}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (self.dir / f"{name}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        return result


def run_tool(
    ctx: LintContext, language: str, tool: str, image: str, body: Callable[[ToolSession], ToolOutcome]
) -> ToolRun:
    """Run one tool and record status, findings, duration and the files it changed."""
    ctx.out(f"  {language:<22} {tool:<20} running ...")
    session = ToolSession(ctx, language, tool, image)
    started = time.monotonic()
    before = ctx.snapshots.snapshot()
    try:
        outcome = body(session)
    except Exception as exc:  # noqa: BLE001 – a broken tool must not stop the others
        (session.dir / "internal-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        outcome = ToolOutcome(Status.FAILED, notes=(f"internal error: {exc}",))
    after = ctx.snapshots.snapshot()

    versions = ctx.runner.image_versions(image)
    changed = ctx.snapshots.changed_files(before, after)
    if changed:
        (session.dir / "changes.patch").write_text(ctx.snapshots.patch(before, after), encoding="utf-8")
    run = ToolRun(
        language=language,
        tool=tool,
        status=outcome.status,
        version=versions.get(tool) or versions.get(tool.split()[0]),
        commands=session.commands,
        duration_seconds=round(time.monotonic() - started, 1),
        changed_files=changed,
        findings=list(outcome.findings),
        notes=list(outcome.notes),
        output_dir=session.dir.relative_to(ctx.step_dir).as_posix(),
    )
    ctx.out(f"  {language:<22} {tool:<20} {describe(run)}")
    for note in run.notes:
        ctx.out(f"  {'':<22} {'':<20}   note: {note}")
    return run


def skipped(language: str, tool: str, reason: str) -> ToolRun:
    return ToolRun(language=language, tool=tool, status=Status.SKIPPED, notes=[reason])


def describe(run: ToolRun) -> str:
    parts = [run.status.value]
    if run.status is not Status.SKIPPED:
        parts.append(f"{len(run.findings)} findings")
        parts.append(f"{len(run.changed_files)} files changed")
        parts.append(f"{run.duration_seconds:.0f} s")
    return ", ".join(parts)


def last_lines(text: str, count: int = 3) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " | ".join(lines[-count:]) if lines else "no output"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower().replace("#", "sharp").replace("+", "p")).strip("-")

"""Rust: Clippy (`cargo clippy --fix`), then rustfmt (`cargo fmt`).

Clippy runs first: after `cargo fmt` changed files, `cargo clippy --fix`
refuses to run on the dirty working tree (docs/linterConventions/README.md).
"""

from __future__ import annotations

import re
from pathlib import Path

from scheduler.linting.model import Finding, Status, ToolOutcome, ToolRun
from scheduler.linting.reports import parse_clippy
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped

LANGUAGE = "Rust"
IMAGE = "code-analysis/lint-rust:latest"
CLIPPY_ENV = {"CLIPPY_CONF_DIR": "/opt/code-analysis/clippy"}
RUSTFMT_CONFIG_DIR = "/opt/code-analysis/rustfmt"
CLIPPY_TIMEOUT_SECONDS = 60 * 60
_LINTS_TABLE = re.compile(r"^\s*\[(workspace\.)?lints[\].]", re.MULTILINE)


def run(ctx: LintContext) -> list[ToolRun]:
    roots = _workspace_roots(ctx.manifests(LANGUAGE))
    if not roots:
        reason = "no Cargo.toml"
        return [skipped(LANGUAGE, "clippy", reason), skipped(LANGUAGE, "rustfmt", reason)]
    return [
        run_tool(ctx, LANGUAGE, "clippy", IMAGE, lambda session: _clippy(session, roots)),
        run_tool(ctx, LANGUAGE, "rustfmt", IMAGE, lambda session: _rustfmt(session, roots)),
    ]


def _workspace_roots(manifests: list[Path]) -> list[Path]:
    """Folders of the top-most Cargo.toml files (a workspace covers its members)."""
    folders = sorted(
        {path.parent for path in manifests if path.name == "Cargo.toml"},
        key=lambda folder: (len(folder.parts), folder.as_posix()),
    )
    roots: list[Path] = []
    for folder in folders:
        if not any(folder.parts[: len(root.parts)] == root.parts for root in roots):
            roots.append(folder)
    return roots


def _clippy(session: ToolSession, roots: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    findings: list[Finding] = []
    notes = _lints_notes(project, session.ctx.manifests(LANGUAGE))
    failed: list[str] = []
    for index, root in enumerate(roots, start=1):
        workdir = project / root
        fix = session.run(
            ["cargo", "clippy", "--fix", "--allow-dirty", "--allow-no-vcs"],
            workdir=workdir, env=CLIPPY_ENV, timeout=CLIPPY_TIMEOUT_SECONDS, label=f"clippy-fix-{index}",
        )
        # A second run collects what remains after the fixes (cached, so fast).
        collect = session.run(
            ["cargo", "clippy", "--message-format=json"],
            workdir=workdir, env=CLIPPY_ENV, timeout=CLIPPY_TIMEOUT_SECONDS, label=f"clippy-{index}",
        )
        findings += parse_clippy(collect.stdout.splitlines(), project=project, workspace=workdir)
        if fix.exit_code != 0:
            failed.append(f"{(root / 'Cargo.toml').as_posix()}: {last_lines(fix.stderr)}")

    if failed:
        status = Status.FAILED if len(failed) == len(roots) and not findings else Status.PARTIAL
        return ToolOutcome(status, tuple(findings), (*notes, *(f"cargo clippy --fix failed for {item}" for item in failed)))
    return ToolOutcome(Status.OK, tuple(findings), tuple(notes))


def _rustfmt(session: ToolSession, roots: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    failed: list[str] = []
    for index, root in enumerate(roots, start=1):
        result = session.run(
            ["cargo", "fmt", "--", "--config-path", RUSTFMT_CONFIG_DIR],
            workdir=project / root, label=f"cargo-fmt-{index}",
        )
        if result.exit_code != 0:
            failed.append(f"{(root / 'Cargo.toml').as_posix()}: {last_lines(result.stderr)}")
    if failed:
        status = Status.FAILED if len(failed) == len(roots) else Status.PARTIAL
        return ToolOutcome(status, notes=tuple(f"cargo fmt failed for {item}" for item in failed))
    return ToolOutcome(Status.OK)


def _lints_notes(project: Path, manifests: list[Path]) -> list[str]:
    return [
        f"project setting not ignored (applies to Clippy): [lints] table in {manifest.as_posix()}"
        for manifest in manifests
        if manifest.name == "Cargo.toml"
        and _LINTS_TABLE.search((project / manifest).read_text(encoding="utf-8", errors="replace"))
    ]

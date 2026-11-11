"""Python: Ruff (`ruff check --fix`, then `ruff format`), project configuration ignored."""

from __future__ import annotations

from scheduler.linting.model import Status, ToolOutcome, ToolRun
from scheduler.linting.reports import files_with_errors, parse_sarif
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped
from scheduler.linting.versions import python_version, ruff_target_version

LANGUAGE = "Python"
IMAGE = "code-analysis/lint-python:latest"
SUFFIXES = (".py", ".pyi")


def run(ctx: LintContext) -> list[ToolRun]:
    if not ctx.own_files(SUFFIXES):
        reason = "no Python files known to git (outside skipped folders, not generated)"
        return [skipped(LANGUAGE, "ruff check", reason), skipped(LANGUAGE, "ruff format", reason)]
    version = python_version(ctx.project, ctx.manifests(LANGUAGE))
    target = ruff_target_version(version)
    if target:
        version_note = f"--target-version {target} (requires-python in {version.source})"
    else:
        version_note = "no Python version found in pyproject.toml: Ruff's default target version"
    state = {"target": target, "notes": [version_note]}
    return [
        run_tool(ctx, LANGUAGE, "ruff check", IMAGE, lambda session: _check(session, state)),
        run_tool(ctx, LANGUAGE, "ruff format", IMAGE, lambda session: _format(session, state)),
    ]


def _target_args(state: dict) -> list[str]:
    return ["--target-version", state["target"]] if state["target"] else []


def _check(session: ToolSession, state: dict) -> ToolOutcome:
    report = session.dir / "ruff.sarif"
    notes = list(state["notes"])

    def check():
        return session.run(
            ["ruff", "check", "--fix", "--isolated", *_target_args(state),
             "--output-format", "sarif", "--output-file", str(report), "."],
            label="ruff-check",
        )

    result = check()
    if result.exit_code == 2 and state["target"] and "--target-version" in result.stderr:
        notes.append(f"Ruff does not know {state['target']}; ran with its default target version")
        state["target"] = None
        state["notes"] = [notes[-1]]
        result = check()

    # Exit code 1 means findings remain after the fixes; that is the normal case.
    if result.exit_code not in (0, 1) or not report.is_file():
        return ToolOutcome(Status.FAILED, notes=(*notes, f"ruff check failed: {last_lines(result.stderr)}"))
    sarif = parse_sarif(report, language=LANGUAGE, tool="ruff check", project=session.ctx.project)
    return ToolOutcome(Status.OK, tuple(sarif.findings), tuple(notes))


def _format(session: ToolSession, state: dict) -> ToolOutcome:
    result = session.run(["ruff", "format", "--isolated", *_target_args(state), "."], label="ruff-format")
    if result.exit_code == 0:
        return ToolOutcome(Status.OK, notes=tuple(state["notes"]))
    unparsed = files_with_errors(result.stderr, project=session.ctx.project, kind="ruff")
    if result.exit_code == 2 and unparsed and all(file in session.ctx.excluded for file in unparsed):
        return ToolOutcome(Status.OK, notes=tuple(state["notes"]))  # only generated files failed
    unparsed = [file for file in unparsed if file not in session.ctx.excluded]
    if result.exit_code == 2 and unparsed:
        return ToolOutcome(
            Status.PARTIAL,
            notes=(*state["notes"], f"not formatted (syntax errors): {', '.join(unparsed)}"),
        )
    return ToolOutcome(Status.FAILED, notes=(*state["notes"], f"ruff format failed: {last_lines(result.stderr)}"))

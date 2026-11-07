"""JavaScript and TypeScript: Biome (`biome check --write`), project configuration ignored.

Biome reads .gitignore only relative to its configuration file, and crashes
when that file lies outside the project (Biome 2.6.6). The tool's empty
configuration is therefore written into the project for the run, and all of
the project's own biome.json/biome.jsonc files are moved out meanwhile.
"""

from __future__ import annotations

from scheduler.linting.files import source_files
from scheduler.linting.hiding import find_files, hidden_files
from scheduler.linting.model import Status, ToolOutcome, ToolRun
from scheduler.linting.reports import parse_sarif
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped

LANGUAGE = "JavaScript/TypeScript"
IMAGE = "code-analysis/lint-node:latest"
PROJECT_CONFIGS = ("biome.json", "biome.jsonc")
TOOL_CONFIG = ".code-analysis-biome.json"
SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts")


def run(ctx: LintContext) -> list[ToolRun]:
    # Biome also formats JSON and CSS: without any JS/TS file it must not run.
    if not source_files(ctx.project, SUFFIXES):
        return [skipped(LANGUAGE, "biome", "no JavaScript/TypeScript files known to git (outside skipped folders)")]
    configs = find_files(ctx.project, PROJECT_CONFIGS)
    with hidden_files(ctx.project, configs, ctx.step_dir / "hidden" / "javascript", ctx.errors):
        return [run_tool(ctx, LANGUAGE, "biome", IMAGE, lambda session: _biome(session, len(configs)))]


def _biome(session: ToolSession, hidden: int) -> ToolOutcome:
    project = session.ctx.project
    config = project / TOOL_CONFIG
    report = session.dir / "biome.sarif"
    config.write_text("{}\n", encoding="utf-8")
    try:
        result = session.run(
            ["biome", "check", "--write", f"--config-path={config}",
             "--vcs-enabled=true", "--vcs-client-kind=git", "--vcs-use-ignore-file=true",
             "--reporter=sarif", f"--reporter-file={report}", "."],
            label="biome-check",
        )
    finally:
        config.unlink(missing_ok=True)

    notes = [f"{hidden} project Biome configuration file(s) ignored"] if hidden else []
    # Biome exits with 1 when errors remain, but also when it fails. Only the
    # report tells the two apart: it is written only if Biome ran.
    if not report.is_file():
        return ToolOutcome(
            Status.FAILED,
            notes=(*notes, f"Biome wrote no report (exit code {result.exit_code}): {last_lines(result.stderr)}"),
        )
    sarif = parse_sarif(report, language=LANGUAGE, tool="biome", project=project)
    return ToolOutcome(Status.OK, tuple(sarif.findings), tuple(notes))

"""Java: google-java-format (`--replace`), then PMD with the quickstart rule set."""

from __future__ import annotations

import os
from pathlib import Path

from scheduler.linting.files import source_files, write_file_list
from scheduler.linting.model import Status, ToolOutcome, ToolRun
from scheduler.linting.reports import files_with_errors, parse_sarif
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped
from scheduler.linting.versions import java_version, pmd_language_version

LANGUAGE = "Java"
IMAGE = "code-analysis/lint-java:latest"
BUILD_OUTPUT_DIRS = frozenset({"target", "build"})
PMD_EXIT_PROCESSING_ERROR = 5
PMD_EXIT_USAGE_ERROR = 2
XARGS_EXIT_COMMAND_FAILED = 123


def run(ctx: LintContext) -> list[ToolRun]:
    files = source_files(ctx.project, (".java",))
    if not files:
        reason = "no tracked .java files"
        return [skipped(LANGUAGE, "google-java-format", reason), skipped(LANGUAGE, "pmd", reason)]
    return [
        run_tool(ctx, LANGUAGE, "google-java-format", IMAGE, lambda session: _format(session, files)),
        run_tool(ctx, LANGUAGE, "pmd", IMAGE, _pmd),
    ]


def _format(session: ToolSession, files: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    file_list = write_file_list((project / path for path in files), session.dir / "files.txt")
    result = session.run(
        ["sh", "-c", 'xargs -0 google-java-format --replace < "$1"', "sh", str(file_list)],
        label="google-java-format",
    )
    if result.exit_code == 0:
        return ToolOutcome(Status.OK)
    unparsed = files_with_errors(result.stderr, project=project, kind="java")
    if result.exit_code == XARGS_EXIT_COMMAND_FAILED and unparsed:
        return ToolOutcome(Status.PARTIAL, notes=(f"not formatted (syntax errors): {', '.join(unparsed)}",))
    return ToolOutcome(Status.FAILED, notes=(f"google-java-format failed: {last_lines(result.stderr)}",))


def _pmd(session: ToolSession) -> ToolOutcome:
    project = session.ctx.project
    version = java_version(project, session.ctx.manifests(LANGUAGE))
    use_version = pmd_language_version(version)
    notes = [
        f"--use-version {use_version} (from {version.source})"
        if use_version
        else "no Java version found in the build files: PMD's default version"
    ]
    report = session.dir / "pmd.sarif"

    def pmd(language_version: str | None):
        report.unlink(missing_ok=True)
        excludes = [arg for directory in _build_output_dirs(project) for arg in ("--exclude", str(directory))]
        version_args = ["--use-version", language_version] if language_version else []
        return session.run(
            ["pmd", "check", "-d", str(project), *excludes,
             "-R", "rulesets/java/quickstart.xml", *version_args,
             "--no-fail-on-violation", "--no-cache", "--no-progress",
             "-f", "sarif", "-r", str(report)],
            label="pmd" if language_version == use_version else "pmd-default-version",
        )

    result = pmd(use_version)
    if result.exit_code == PMD_EXIT_USAGE_ERROR and use_version:
        notes.append(f"PMD does not know {use_version}; ran with its default version")
        result = pmd(None)

    if result.exit_code not in (0, PMD_EXIT_PROCESSING_ERROR) or not report.is_file():
        return ToolOutcome(Status.FAILED, notes=(*notes, f"PMD failed: {last_lines(result.stderr)}"))
    sarif = parse_sarif(report, language=LANGUAGE, tool="pmd", project=project)
    if sarif.problems or result.exit_code == PMD_EXIT_PROCESSING_ERROR:
        notes.append(f"not analysed: {'; '.join(sarif.problems) or 'see stderr'}")
        return ToolOutcome(Status.PARTIAL, tuple(sarif.findings), tuple(notes))
    return ToolOutcome(Status.OK, tuple(sarif.findings), tuple(notes))


def _build_output_dirs(project: Path) -> list[Path]:
    """All target/ and build/ folders (Maven/Gradle output, also in sub-modules)."""
    found: list[Path] = []
    for dirpath, dirnames, _ in os.walk(project):
        for name in sorted(dirnames):
            if name in BUILD_OUTPUT_DIRS:
                found.append(Path(dirpath, name))
        dirnames[:] = sorted(d for d in dirnames if d not in BUILD_OUTPUT_DIRS and not d.startswith("."))
    return found

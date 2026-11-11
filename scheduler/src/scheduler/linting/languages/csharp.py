"""C#: dotnet format, with the project's .editorconfig files moved out during the run."""

from __future__ import annotations

import re
from pathlib import Path

from scheduler.linting.hiding import find_files, hidden_files
from scheduler.linting.model import Finding, Status, ToolOutcome, ToolRun
from scheduler.linting.reports import parse_dotnet_report
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped

LANGUAGE = "C#"
IMAGE = "code-analysis/lint-dotnet:latest"
SUFFIXES = (".cs",)
PROJECT_CONFIGS = (".editorconfig",)
SOLUTION_SUFFIXES = (".sln", ".slnx")
# Project settings dotnet format follows and the tool cannot switch off
# without editing the project files (docs/linterConventions/README.md).
_NOT_IGNORABLE = re.compile(r"<(AnalysisMode|AnalysisLevel|EnforceCodeStyleInBuild)\b[^>]*>")
_ANALYZER_PACKAGES = re.compile(
    r'Include="(StyleCop\.Analyzers|Roslynator\.Analyzers|SonarAnalyzer\.CSharp|Meziantou\.Analyzer'
    r'|Microsoft\.CodeAnalysis\.NetAnalyzers)"'
)


def run(ctx: LintContext) -> list[ToolRun]:
    targets = _targets(ctx.manifests(LANGUAGE))
    if not targets:
        return [skipped(LANGUAGE, "dotnet format", "no solution or project file (.sln, .slnx, .csproj)")]
    # dotnet format also reads .editorconfig files above the project folder.
    configs = find_files(ctx.mount_root, PROJECT_CONFIGS)
    with hidden_files(ctx.mount_root, configs, ctx.step_dir / "hidden" / "csharp", ctx.errors):
        return [run_tool(ctx, LANGUAGE, "dotnet format", IMAGE, lambda session: _format(session, targets, configs))]


def _targets(manifests: list[Path]) -> list[Path]:
    """Each solution file; without solutions, each project file."""
    solutions = [path for path in manifests if path.suffix in SOLUTION_SUFFIXES]
    return solutions or [path for path in manifests if path.suffix == ".csproj"]


def _format(session: ToolSession, targets: list[Path], hidden: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    notes = [f"{len(hidden)} .editorconfig file(s) ignored"] if hidden else []
    notes += _not_ignorable_settings(project)
    findings: list[Finding] = []
    failed: list[str] = []
    for index, target in enumerate(targets, start=1):
        report_dir = session.dir / f"report-{index}"
        result = session.run(
            ["dotnet", "format", str(project / target), "--report", str(report_dir), "-v", "minimal"],
            label=f"dotnet-format-{index}",
        )
        report = report_dir / "format-report.json"
        if result.exit_code != 0:
            failed.append(f"{target.as_posix()}: {last_lines(result.stdout + result.stderr)}")
        if report.is_file():
            findings += parse_dotnet_report(report, project=project)

    if failed:
        status = Status.FAILED if len(failed) == len(targets) else Status.PARTIAL
        return ToolOutcome(status, tuple(findings), (*notes, *(f"dotnet format failed for {item}" for item in failed)))
    return ToolOutcome(Status.OK, tuple(findings), tuple(notes))


def _not_ignorable_settings(project: Path) -> list[str]:
    notes: list[str] = []
    for pattern in ("*.csproj", "*.props", "*.targets"):
        for path in sorted(project.rglob(pattern)):
            if any(part in {"bin", "obj", "node_modules"} or part.startswith(".") for part in path.relative_to(project).parts[:-1]):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            found = sorted({match.group(1) for match in _NOT_IGNORABLE.finditer(text)})
            found += sorted({match.group(1) for match in _ANALYZER_PACKAGES.finditer(text)})
            if found:
                notes.append(
                    f"project setting not ignored (applies to dotnet format): {', '.join(found)} "
                    f"in {path.relative_to(project).as_posix()}"
                )
    return notes

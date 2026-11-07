"""Turn the tools' reports into Finding objects (findings.json)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

from scheduler.linting.model import Finding

# Formatting entries of dotnet format's report. The changes themselves are in
# the patch; only analyzer and code-style fixes become findings.
DOTNET_FORMATTING_IDS = frozenset({"WHITESPACE", "FINALNEWLINE", "ENDOFLINE", "CHARSET"})

_SARIF_LEVELS = {"error": "error", "warning": "warning", "note": "note", "none": "note"}


@dataclass
class SarifReport:
    findings: list[Finding] = field(default_factory=list)
    # Files the tool could not process ("path: message"), e.g. PMD parse errors.
    problems: list[str] = field(default_factory=list)


def relative_file(value: str | None, project: Path, base: Path | None = None) -> str | None:
    """A path or file:// URI as a path relative to the project, if inside it."""
    if not value:
        return None
    if value.startswith("file:"):
        value = unquote(urlparse(value).path)
    path = Path(value)
    if not path.is_absolute():
        path = (base or project) / path
    path = Path(_normalize(path))
    try:
        return path.relative_to(project).as_posix()
    except ValueError:
        return None


def _normalize(path: Path) -> str:
    # Resolve ".." without following symlinks (paths may not exist here).
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return str(Path(*parts)) if parts else str(path)


def parse_sarif(path: Path, *, language: str, tool: str, project: Path) -> SarifReport:
    report = SarifReport()
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    for run in data.get("runs", []):
        for result in run.get("results", []):
            location = (result.get("locations") or [{}])[0].get("physicalLocation", {})
            region = location.get("region", {})
            uri = location.get("artifactLocation", {}).get("uri")
            file = relative_file(uri, project)
            if uri and file is None:
                continue  # outside the project
            report.findings.append(
                Finding(
                    language=language,
                    tool=tool,
                    rule=str(result.get("ruleId") or "unknown"),
                    severity=_SARIF_LEVELS.get(str(result.get("level", "warning")), "warning"),
                    message=str(result.get("message", {}).get("text", "")).strip(),
                    file=file,
                    line=region.get("startLine"),
                    column=region.get("startColumn"),
                )
            )
        for invocation in run.get("invocations", []):
            for note in invocation.get("toolExecutionNotifications", []):
                uri = (
                    (note.get("locations") or [{}])[0].get("physicalLocation", {}).get("artifactLocation", {}).get("uri")
                )
                text = str(note.get("message", {}).get("text", "")).strip().splitlines()[0:1]
                where = relative_file(uri, project) or uri or "unknown file"
                report.problems.append(f"{where}: {text[0] if text else 'could not be processed'}")
    return report


# --- clang-tidy ---------------------------------------------------------------

_CLANG_TIDY_FINDING = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+): (?P<severity>warning|error): (?P<message>.*) \[(?P<rule>[^\]\s]+)\]$"
)
_RUN_CLANG_TIDY_FILE = re.compile(r"^\[\s*\d+/\d+\]\[[^\]]*\] .*\s(?P<file>/\S+)$")
_SUPPRESSED = re.compile(r"^Suppressed (?P<total>\d+) warnings \((?P<details>[^)]*)\)")
_NOLINT = re.compile(r"(\d+) NOLINT")


@dataclass
class ClangTidyReport:
    findings: list[Finding] = field(default_factory=list)
    suppressed: int = 0
    nolint: int = 0


def parse_clang_tidy(
    output: str, *, project: Path, directories: dict[str, Path] | None = None, default_dir: Path | None = None
) -> ClangTidyReport:
    """Findings from clang-tidy / run-clang-tidy output.

    Relative paths in clang-tidy's messages are relative to the compile
    command's directory. run-clang-tidy prints a line per translation unit
    before its messages; `directories` maps those files to their directory.
    """
    report = ClangTidyReport()
    seen: set[tuple[object, ...]] = set()
    current = default_dir or project
    for line in output.splitlines():
        if header := _RUN_CLANG_TIDY_FILE.match(line):
            current = (directories or {}).get(header.group("file"), Path(header.group("file")).parent)
            continue
        if suppressed := _SUPPRESSED.match(line):
            report.suppressed += int(suppressed.group("total"))
            report.nolint += sum(int(n) for n in _NOLINT.findall(suppressed.group("details")))
            continue
        match = _CLANG_TIDY_FINDING.match(line)
        if not match:
            continue
        file = relative_file(match.group("file"), project, current)
        if file is None:
            continue  # system or third-party header
        key = (file, match.group("line"), match.group("column"), match.group("rule"), match.group("message"))
        if key in seen:
            continue  # a header's finding is reported once per including file
        seen.add(key)
        report.findings.append(
            Finding(
                language="C/C++",
                tool="clang-tidy",
                rule=match.group("rule"),
                severity=match.group("severity"),
                message=match.group("message"),
                file=file,
                line=int(match.group("line")),
                column=int(match.group("column")),
            )
        )
    return report


# --- Clippy ----------------------------------------------------------------------


def parse_clippy(lines: Iterable[str], *, project: Path, workspace: Path) -> list[Finding]:
    """Findings from `cargo clippy --message-format=json` (one JSON object per line)."""
    findings: list[Finding] = []
    seen: set[tuple[object, ...]] = set()
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("reason") != "compiler-message":
            continue
        message = entry.get("message", {})
        spans = [span for span in message.get("spans", []) if span.get("is_primary")]
        if not spans:
            continue  # summaries such as "generated 2 warnings"
        span = spans[0]
        file = relative_file(span.get("file_name"), project, workspace)
        if file is None:
            continue  # dependency code
        rule = (message.get("code") or {}).get("code") or message.get("level", "warning")
        key = (file, span.get("line_start"), span.get("column_start"), rule, message.get("message"))
        if key in seen:
            continue  # the same message for several targets (bin, test, ...)
        seen.add(key)
        level = message.get("level", "warning")
        findings.append(
            Finding(
                language="Rust",
                tool="clippy",
                rule=rule,
                severity="error" if level.startswith("error") else "warning" if level == "warning" else "note",
                message=str(message.get("message", "")),
                file=file,
                line=span.get("line_start"),
                column=span.get("column_start"),
            )
        )
    return findings


# --- dotnet format ---------------------------------------------------------------


def parse_dotnet_report(path: Path, *, project: Path) -> list[Finding]:
    """Fixes dotnet format applied (format-report.json); formatting-only entries are skipped."""
    findings: list[Finding] = []
    for document in json.loads(path.read_text(encoding="utf-8-sig", errors="replace")):
        file = relative_file(document.get("FilePath"), project)
        for change in document.get("FileChanges", []):
            rule = str(change.get("DiagnosticId", "unknown"))
            if rule in DOTNET_FORMATTING_IDS:
                continue
            findings.append(
                Finding(
                    language="C#",
                    tool="dotnet format",
                    rule=rule,
                    severity="warning",
                    message=str(change.get("FormatDescription", "")),
                    file=file,
                    line=change.get("LineNumber"),
                    column=change.get("CharNumber"),
                    fixed=True,
                )
            )
    return findings


# --- google-java-format / ruff format --------------------------------------------

_JAVA_ERROR = re.compile(r"^(?P<file>/.+?\.java):\d+:\d+: error: ")
_RUFF_FORMAT_ERROR = re.compile(r"^error: Failed to parse (?P<file>.+?):\d+:\d+: ")


def files_with_errors(output: str, *, project: Path, kind: str) -> list[str]:
    """Files a formatter could not parse, from its error output ("java" or "ruff")."""
    pattern = _JAVA_ERROR if kind == "java" else _RUFF_FORMAT_ERROR
    files: list[str] = []
    for line in output.splitlines():
        if match := pattern.match(line):
            file = relative_file(match.group("file"), project) or match.group("file")
            if file not in files:
                files.append(file)
    return files

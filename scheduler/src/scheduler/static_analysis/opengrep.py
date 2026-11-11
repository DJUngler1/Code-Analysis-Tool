"""OpenGrep: run the active rule sets on the project, read its JSON report.

OpenGrep runs in a fresh container of code-analysis/opengrep:
- without network: the rules are part of the image, nothing leaves the machine;
- with the project mounted read-only: it cannot change the code;
- with OpenGrep's own default ignore list (tests/, build/, vendor/,
  node_modules/, *.min.js, ...) instead of the project's .semgrepignore, which
  would replace that list (same principle as step 2: project configuration
  is ignored). .gitignore still applies: OpenGrep scans the files git knows.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.acceptance.languages import EXTENSION_TO_LANGUAGE, header_language
from scheduler.linting.docker import ToolRunner
from scheduler.linting.model import Status
from scheduler.linting.reports import relative_file
from scheduler.linting.session import last_lines
from scheduler.static_analysis.model import AnalysisFinding, AnalysisRun
from scheduler.static_analysis.rulesets import OpenGrepConfig, RuleSet

TOOL = "opengrep"
IMAGE = "code-analysis/opengrep:latest"
BUILD_HINT = "docker compose -f docker/docker-compose.yml --profile tools build opengrep"
# OpenGrep's default .semgrepignore of the pinned version, stored in the image.
DEFAULT_IGNORE_FILE = "/opt/code-analysis/opengrep/default.semgrepignore"
# Makes OpenGrep read the ignore patterns from that file instead of the
# .semgrepignore in the working directory (cli/src/semgrep/run_scan.py).
IGNORE_FILE_ENV = "SEMGREP_R2C_INTERNAL_EXPLICIT_SEMGREPIGNORE"
TIMEOUT_SECONDS = 60 * 60
MAX_LISTED_FILES = 10

_SEVERITIES = {
    "ERROR": "error", "CRITICAL": "error", "HIGH": "error",
    "WARNING": "warning", "MEDIUM": "warning",
    "INFO": "note", "LOW": "note", "INVENTORY": "note", "EXPERIMENT": "note",
}
_SUMMARY = re.compile(r"Ran (?P<rules>\d+) rules? on (?P<files>\d+) files?")


@dataclass
class ScanContext:
    project: Path
    # Mounted read-only: the git repository root if the project is a subfolder
    # of one (inside PROJECTS_DIR), otherwise the project.
    mount_root: Path
    step_dir: Path  # <run>/step3-static-analysis
    acceptance: AcceptanceResult
    runner: ToolRunner
    config: OpenGrepConfig
    out: Callable[[str], None] = print

    @property
    def languages(self) -> tuple[str, ...]:
        report = self.acceptance.languages
        return (report.primary or "", *report.supported_others)


@dataclass
class OpenGrepReport:
    findings: list[AnalysisFinding]
    # Files OpenGrep could not (fully) analyse and other errors it reported.
    problems: list[str]
    dropped: int  # findings in generated files
    files_scanned: int | None


def run_opengrep(ctx: ScanContext) -> AnalysisRun:
    languages = [language for language in ctx.languages if language]
    used = [(ruleset, ruleset.folders_for(languages)) for ruleset in ctx.config.active]
    run = AnalysisRun(
        tool=TOOL,
        status=Status.SKIPPED,
        rulesets=[_ruleset_info(ruleset, folders) for ruleset, folders in used if folders],
    )
    uncovered = [language for language in languages if not any(ruleset.folders_for([language])
                                                                for ruleset in ctx.config.active)]
    names = ", ".join(ruleset.name for ruleset in ctx.config.active)
    if uncovered:
        run.notes.append(f"no rules for {', '.join(uncovered)} in the active rule sets ({names})")
    if not run.rulesets:
        return run

    versions = ctx.runner.image_versions(IMAGE)
    run.version = versions.get(TOOL)
    # The image contains only the rule sets that were active when it was built.
    missing = [ruleset.name for ruleset, _ in used if f"ruleset:{ruleset.name}" not in versions]
    outdated = [ruleset.name for ruleset, _ in used
                if ruleset.name not in missing and versions[f"ruleset:{ruleset.name}"] != ruleset.commit]
    if missing or outdated:
        run.status = Status.FAILED
        problems = []
        if missing:
            problems.append(f"{', '.join(missing)} not in the image (rule sets are only downloaded "
                            "while they are active, when the image is built)")
        if outdated:
            problems.append(f"{', '.join(outdated)} in the image at another commit than in {ctx.config.path.name}")
        run.notes.append(
            f"the image {IMAGE} does not match {ctx.config.path.name}: {'; '.join(problems)}. "
            f"Rebuilding downloads the active rule sets, which means accepting their licenses:\n  {BUILD_HINT}"
        )
        return run

    out_dir = ctx.step_dir / TOOL
    out_dir.mkdir(parents=True, exist_ok=True)
    json_report, sarif_report = out_dir / "opengrep.json", out_dir / "opengrep.sarif"
    command = ["opengrep", "scan"]
    for ruleset, folders in used:
        for folder in folders:
            command += ["--config", str(ruleset.image_path(folder))]
    command += [
        "--taint-intrafile",  # taint flows across functions within a file
        "--disable-version-check",
        f"--json-output={json_report}",
        f"--sarif-output={sarif_report}",
        ".",
    ]
    started = time.monotonic()
    result = ctx.runner.run(
        IMAGE,
        command,
        workdir=ctx.project,
        mounts=[ctx.step_dir],
        read_only=[ctx.mount_root],
        env={IGNORE_FILE_ENV: DEFAULT_IGNORE_FILE},
        timeout=TIMEOUT_SECONDS,
        network="none",
    )
    run.duration_seconds = round(time.monotonic() - started, 1)
    run.commands.append(result)
    run.output_dir = out_dir.relative_to(ctx.step_dir).as_posix()
    (out_dir / "opengrep.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (out_dir / "opengrep.stderr.txt").write_text(result.stderr, encoding="utf-8")
    if summary := _SUMMARY.search(result.stderr):
        run.rules_run = int(summary.group("rules"))

    try:
        data = json.loads(json_report.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        data = None
    if result.exit_code != 0 or not isinstance(data, dict):
        run.status = Status.FAILED
        reason = "timed out" if result.timed_out else f"exit code {result.exit_code}"
        errors = _problems(data.get("errors", []), ctx.acceptance.languages.excluded) if isinstance(data, dict) else []
        run.notes += [f"opengrep failed ({reason}): {last_lines(result.stderr)}", *errors[:MAX_LISTED_FILES]]
        return run

    report = parse_report(
        data, project=ctx.project, rulesets=[ruleset for ruleset, _ in used],
        excluded=ctx.acceptance.languages.excluded,
    )
    run.findings = report.findings
    run.files_scanned = report.files_scanned
    if report.dropped:
        run.notes.append(f"{report.dropped} findings in generated files left out")
    if report.problems:
        run.status = Status.PARTIAL
        shown = report.problems[:MAX_LISTED_FILES]
        more = len(report.problems) - len(shown)
        run.notes.append(
            f"not (fully) analysed: {'; '.join(shown)}" + (f"; and {more} more (opengrep.json)" if more else "")
        )
    else:
        run.status = Status.OK
    return run


def parse_report(
    data: Mapping, *, project: Path, rulesets: Iterable[RuleSet], excluded: Mapping[str, str]
) -> OpenGrepReport:
    """Findings and problems from OpenGrep's JSON report (`--json-output`)."""
    prefixes = [(ruleset.name, _rule_prefix(ruleset)) for ruleset in rulesets]
    findings: list[AnalysisFinding] = []
    dropped = 0
    for result in data.get("results", []):
        file = relative_file(result.get("path"), project)
        if file in excluded:
            dropped += 1
            continue
        extra = result.get("extra") or {}
        metadata = extra.get("metadata") or {}
        start, end = result.get("start") or {}, result.get("end") or {}
        ruleset, rule = _split_rule_id(str(result.get("check_id", "unknown")), prefixes)
        tool_severity = str(extra.get("severity", "WARNING")).upper()
        findings.append(
            AnalysisFinding(
                tool=TOOL,
                ruleset=ruleset,
                rule=rule,
                severity=_SEVERITIES.get(tool_severity, "warning"),
                tool_severity=tool_severity,
                message=str(extra.get("message", "")).strip(),
                language=_language(file, project),
                file=file,
                line=start.get("line"),
                column=start.get("col"),
                end_line=end.get("line"),
                end_column=end.get("col"),
                category=_text(metadata.get("category")),
                subcategory=_texts(metadata.get("subcategory")),
                cwe=_texts(metadata.get("cwe")),
                owasp=_texts(metadata.get("owasp")),
                confidence=_text(metadata.get("confidence")),
                likelihood=_text(metadata.get("likelihood")),
                impact=_text(metadata.get("impact")),
                fingerprint=_text(extra.get("fingerprint")),
            )
        )
    scanned = (data.get("paths") or {}).get("scanned")
    return OpenGrepReport(
        findings=findings,
        problems=_problems(data.get("errors", []), excluded),
        dropped=dropped,
        files_scanned=len(scanned) if isinstance(scanned, list) else None,
    )


def _problems(errors: Iterable[Mapping], excluded: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    for error in errors:
        kind = error.get("type") or "error"
        if isinstance(kind, list):  # some error types are reported as ["Kind", details]
            kind = kind[0] if kind else "error"
        kind = str(kind)
        path = error.get("path")
        if path:
            if path in excluded:
                continue  # generated file, not analysed on purpose
            entry = f"{path} ({kind})"
        else:
            message = str(error.get("message") or "").strip().splitlines()
            entry = f"{kind}: {message[0] if message else 'no details'}"
        if entry not in problems:
            problems.append(entry)
    return problems


def _rule_prefix(ruleset: RuleSet) -> str:
    """OpenGrep prefixes rule ids with the path of the rule file: /a/b/x.yaml -> a.b.<id>."""
    return str(ruleset.image_path("")).strip("/").replace("/", ".") + "."


def _split_rule_id(check_id: str, prefixes: list[tuple[str, str]]) -> tuple[str | None, str]:
    for name, prefix in prefixes:
        if check_id.startswith(prefix):
            return name, check_id[len(prefix):]
    return None, check_id


def _language(file: str | None, project: Path) -> str | None:
    if file is None:
        return None
    suffix = Path(file).suffix.lower()
    if suffix == ".h":
        path = project / file
        return header_language(path) if path.is_file() else "C/C++"
    return EXTENSION_TO_LANGUAGE.get(suffix)


def _text(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def _texts(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _ruleset_info(ruleset: RuleSet, folders: list[str]) -> dict:
    return {
        "name": ruleset.name,
        "repository": ruleset.repository,
        "commit": ruleset.commit,
        "license": ruleset.license,
        "folders": folders,
    }

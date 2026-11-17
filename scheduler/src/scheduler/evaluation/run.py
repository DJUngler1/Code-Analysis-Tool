"""Step 6 of the workflow: evaluate the results of steps 2 to 5 with local LLMs, then write the final audit.

Three areas: security (step 4 and the findings of step 3 whose tool category
is security, correctness or reliability, or missing), code quality (step 2 and
the other findings of step 3), test quality (step 5). For each area listed in code-analysis.toml
([llm] areas) whose steps ran:
1. Python prepares the items: ids, duplicates, groups, code context (items.py).
2. The decision model labels every item (decide.py). Then it is unloaded.
3. Python builds the report, the reasoning model writes its texts (report.py).
   Then it is unloaded, too: the two models are never loaded together.
4. Python writes the final audit from the reports (audit.py).

<OUTPUT_DIR>/<project>/<date_time>/
  audit.md, audit.json      the final audit
  step6-llm-evaluation/
    summary.json            models, settings; per area: status, items, requests, durations, tokens, low confidence
    <area>/items.json       the prepared items: ids, duplicates, code context
    <area>/decisions.json   per item: label, probabilities, confidence, review flag (duplicates: duplicate_of)
    <area>/report.json      the area's report: structure by the tool, texts by the reasoning model
    <area>/requests/NNN-decision.json, NNN-report.json   every request and answer (with the model's reasoning)
"""

from __future__ import annotations

import json
import shutil
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from scheduler.config import Settings
from scheduler.evaluation.audit import AUDIT_JSON, AUDIT_MD, write_audit
from scheduler.evaluation.config import AREAS, ConfigError, InputConfig, LlmConfig, load_config
from scheduler.evaluation.decide import DecisionStats, decide_area
from scheduler.evaluation.items import (EVALUATION_DIR, AreaItems, SourceReader, code_quality_items, project_of,
                                        security_items, test_items)
from scheduler.evaluation.ollama import Exchange, OllamaClient, OllamaError, model_key
from scheduler.evaluation.report import ReportStats, TextWriter, build_report, write_texts
from scheduler.linting.model import Status

STEP_DIR = EVALUATION_DIR
AREA_ITEMS: dict[str, Callable[[Path, SourceReader, InputConfig], AreaItems]] = {
    "security": security_items, "code_quality": code_quality_items, "test_quality": test_items}
AREA_NAMES = {"security": "security", "code_quality": "code quality", "test_quality": "test quality"}


@dataclass
class AreaRun:
    area: str
    status: Status = Status.SKIPPED
    items: int = 0
    duplicates: int = 0
    decisions: DecisionStats | None = None
    report: ReportStats | None = None
    statistics: dict = field(default_factory=dict)  # the report's statistics, for the console
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {"area": self.area, "status": self.status.value, "items": self.items, "duplicates": self.duplicates,
                "decisions": self.decisions.to_json() if self.decisions else None,
                "report": self.report.to_json() if self.report else None, "notes": self.notes}


@dataclass
class EvaluationResult:
    run_dir: Path | None
    areas: list[AreaRun] = field(default_factory=list)
    models: dict[str, dict] = field(default_factory=dict)
    audit: Path | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.errors and all(area.status in (Status.OK, Status.SKIPPED) for area in self.areas)


class RequestLog:
    """Writes every request and answer of an area to <area>/requests/NNN-<kind>.json."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.count = 0

    def writer(self, kind: str) -> Callable[[Exchange], None]:
        def write(exchange: Exchange) -> None:
            self.count += 1
            _write_json(self.folder / f"{self.count:03d}-{kind}.json", {
                "endpoint": exchange.endpoint, "attempts": exchange.attempts,
                "duration_seconds": exchange.duration_seconds, "error": exchange.error,
                "request": exchange.request, "response": exchange.response})
        return write


def run_evaluation(
    run_dir: Path,
    settings: Settings,
    *,
    client: OllamaClient | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    out: Callable[[str], None] = print,
) -> EvaluationResult:
    started = now()
    step_dir = run_dir / STEP_DIR
    # Results of an earlier evaluation of this run (--evaluate) are replaced, not mixed.
    shutil.rmtree(step_dir, ignore_errors=True)
    for name in (AUDIT_MD, AUDIT_JSON):
        (run_dir / name).unlink(missing_ok=True)
    step_dir.mkdir(parents=True, exist_ok=True)
    result = EvaluationResult(run_dir)
    config: LlmConfig | None = None
    try:
        config = load_config(settings.config_file)
    except ConfigError as exc:
        result.errors.append(str(exc))
    project = project_of(run_dir)

    prepared: list[tuple[AreaRun, AreaItems]] = []
    if config is not None:
        prepared = _prepare(run_dir, step_dir, config, project, result, out)
    if config is not None and prepared:
        # Time limit, repetitions and keep_alive come with each request, from the model's own settings.
        client = client or OllamaClient(settings.ollama_base_url)
        try:
            _check_models(client, config, result)
            if not result.errors:
                _evaluate(client, config, prepared, run_dir, step_dir, result, out)
        except OllamaError as exc:  # unreachable, or stopped answering
            result.errors.append(str(exc))
            for model in (config.decision.model, config.report.model):  # best effort
                _unload(client, model)
        finally:
            for run, _ in prepared:
                if run.status is Status.SKIPPED:  # prepared, but no model ran for it
                    run.status = Status.FAILED
                    run.notes.append("not evaluated (see the errors of step 6)")

    summary = _summary(result, settings, config, project, started, now())
    if any((step_dir / run.area / "report.json").is_file() for run in result.areas):
        try:
            result.audit = write_audit(run_dir, summary)
            summary["audit"] = [result.audit.name, result.audit.with_suffix(".json").name]
        except Exception as exc:  # noqa: BLE001 – reported, the reports stay
            (step_dir / "internal-error-audit.txt").write_text(traceback.format_exc(), encoding="utf-8")
            result.errors.append(f"the audit could not be written: {exc}")
            summary["complete"] = False
            summary["errors"] = result.errors
    _write_json(step_dir / "summary.json", summary)
    return result


def _prepare(run_dir: Path, step_dir: Path, config: LlmConfig, project: Path | None, result: EvaluationResult,
             out: Callable[[str], None]) -> list[tuple[AreaRun, AreaItems]]:
    reader = SourceReader(project, config.input)
    prepared = []
    for name in AREAS:
        run = AreaRun(name)
        result.areas.append(run)
        if name not in config.areas:
            run.notes.append("not in [llm] areas of code-analysis.toml")
            out(f"  {AREA_NAMES[name]:<14} skipped: not in [llm] areas of code-analysis.toml")
            continue
        try:
            items = AREA_ITEMS[name](run_dir, reader, config.input)
        except Exception as exc:  # noqa: BLE001 – reported like a failed tool, the other areas go on
            (step_dir / f"internal-error-{name}.txt").write_text(traceback.format_exc(), encoding="utf-8")
            run.status = Status.FAILED
            run.notes.append(f"preparing the items failed: {exc}")
            out(f"  {AREA_NAMES[name]:<14} failed: {exc}")
            continue
        items.facts["project"] = str(project) if project else None
        run.notes += items.notes
        if not items.available:
            out(f"  {AREA_NAMES[name]:<14} skipped: {' '.join(items.notes)}")
            continue
        run.items, run.duplicates = len(items.items), len(items.duplicates)
        _write_json(step_dir / name / "items.json", items.to_json())
        out(f"  {AREA_NAMES[name]:<14} {describe_items(items)}")
        prepared.append((run, items))
    return prepared


def describe_items(items: AreaItems) -> str:
    kinds: dict[str, int] = {}
    for item in items.items:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    names = {"static": ("static finding", "static findings"),
             "dependency": ("dependency finding", "dependency findings"),
             "lint_group": ("group of lint findings", "groups of lint findings"),
             "static_group": ("group of static analysis findings", "groups of static analysis findings"),
             "test_failure": ("group of failed tests", "groups of failed tests"),
             "weak_file": ("weakly tested file", "weakly tested files"),
             "mutant": ("surviving mutant", "surviving mutants")}
    parts = [plural(count, *names[kind]) for kind, count in kinds.items()] or ["nothing to label"]
    text = ", ".join(parts)
    if items.duplicates:
        text += f" ({plural(len(items.duplicates), 'duplicate', 'duplicates')} merged by the tool)"
    if items.area == "code_quality" and items.facts.get("input_findings"):
        text += f" from {items.facts['input_findings']} findings"
    return text


def plural(count: int, singular: str, several: str) -> str:
    return f"{count} {singular if count == 1 else several}"


def _check_models(client: OllamaClient, config: LlmConfig, result: EvaluationResult) -> None:
    installed = client.installed()  # raises OllamaError if Ollama is not reachable
    by_key = {model_key(name): (name, digest) for name, digest in installed.items()}
    for role, model in (("decision", config.decision.model), ("report", config.report.model)):
        found = by_key.get(model_key(model))
        if found is None:
            result.errors.append(f"the {role} model {model} is not installed in Ollama. Install it on the host "
                                 f"with `ollama pull {model}`, or change [llm.{role}] model in code-analysis.toml.")
        else:
            result.models[role] = {"name": found[0], "digest": found[1]}


def _evaluate(client: OllamaClient, config: LlmConfig, prepared: list[tuple[AreaRun, AreaItems]], run_dir: Path,
              step_dir: Path, result: EvaluationResult, out: Callable[[str], None]) -> None:
    logs = {run.area: RequestLog(step_dir / run.area / "requests") for run, _ in prepared}
    _unload(client, config.report.model)  # e.g. still loaded from an earlier run
    out(f"\n  Labeling with {config.decision.model} (decision model) ...")
    decisions: dict[str, dict[str, dict]] = {}
    for run, items in prepared:
        labels, run.decisions = decide_area(client, config.decision, items, log=logs[run.area].writer("decision"),
                                            out=out)
        decisions[run.area] = labels
        _write_json(step_dir / run.area / "decisions.json",
                    [{"id": item_id, **decision} for item_id, decision in labels.items()])
        out(f"    {AREA_NAMES[run.area]}: {_describe_labels(run.decisions)}")
    if not _unload(client, config.decision.model):
        result.errors.append(f"the decision model {config.decision.model} could not be unloaded; the reports were "
                             "not written, so that the two models are never loaded together. Stop it with "
                             f"`ollama stop {config.decision.model}` and run `code-analysis --evaluate {run_dir}`.")
        return

    out(f"\n  Writing the reports with {config.report.model} (reasoning model) ...")
    try:
        for run, items in prepared:
            report_config = config.report_for(run.area)  # [llm.report] with the area's own values
            draft = build_report(items, decisions[run.area], run_dir, config.input)
            writer = TextWriter(client, report_config, area=run.area, language=config.report_language,
                                retries=report_config.retries, log=logs[run.area].writer("report"), out=out)
            try:
                write_texts(draft, writer)
            finally:  # also when Ollama stops answering: the report is written with the texts so far
                run.report = writer.stats
                run.statistics = draft.report.get("statistics", {})
                _write_json(step_dir / run.area / "report.json", draft.report)
                run.status = _status(run)
                out(f"    {AREA_NAMES[run.area]}: {_describe_report(run)}")
    finally:
        _unload(client, config.report.model)


def _unload(client: OllamaClient, model: str) -> bool:
    try:
        loaded = [model_key(name) for name in client.loaded()]
    except OllamaError:
        return False
    return model_key(model) not in loaded or client.unload(model)


def _status(run: AreaRun) -> Status:
    decisions, report = run.decisions, run.report
    if decisions and run.items and decisions.defaulted == run.items:
        return Status.FAILED  # no item got a label from the model
    if report and report.requests and report.failed_requests == report.requests:
        return Status.FAILED
    if (decisions and decisions.defaulted) or (report and not report.complete):
        return Status.PARTIAL
    return Status.OK


def _describe_labels(stats: DecisionStats) -> str:
    labels = ", ".join(f"{count} {label}" for label, count in stats.labels.most_common()) or "nothing to label"
    text = f"{labels} ({stats.requests} requests, {stats.duration_seconds:.0f} s)"
    if stats.low_confidence:
        text += f"; {stats.low_confidence} with low confidence"
    if stats.defaulted:
        text += f"; {stats.defaulted} without an answer (default label, flagged for review)"
    return text


def _describe_report(run: AreaRun) -> str:
    report = run.report
    assert report is not None
    text = f"{report.entries} entries ({report.requests} requests, {report.duration_seconds:.0f} s)"
    if report.texts_missing:
        text += f"; texts missing for {report.texts_missing} entries"
    if report.summary_missing:
        text += "; summary missing"
    return text


def _summary(result: EvaluationResult, settings: Settings, config: LlmConfig | None, project: Path | None,
             started: datetime, finished: datetime) -> dict:
    return {
        "step": "6 – LLM evaluation",
        "project": str(project) if project else None,
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "complete": result.complete,
        "errors": result.errors,
        "ollama": settings.ollama_base_url,
        "models": result.models,
        "settings": asdict(config) if config else None,
        "areas": [run.to_json() for run in result.areas],
        "audit": None,
    }


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

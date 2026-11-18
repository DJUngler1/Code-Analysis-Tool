"""Step 6 with --resume: the decisions of an earlier evaluation of the run are reused, the reports written again.

E.g. after the reasoning model failed: the decisions (the long part) are kept, only the reports are written,
also with another reasoning model. The decisions of an area are reused only if nothing that shaped them changed:
- the decision model: the same name and digest;
- the settings of [llm.decision] that change the requests or the review flags: max_questions,
  max_input_kib, review_confidence;
- the items: decisions.json has a decision for exactly the items and duplicates prepared now;
- the questions: every item was asked with the same shared input and question it would be asked now (the
  decision prompts, the items' facts, [llm.input]), checked against the logged requests.
Otherwise the area is labeled again. The reports (report.json, NNN-report.json) are always written again.

<area>/decisions_info.json records the evaluation that made the decisions (start, model, settings,
statistics). Evaluations from before that file have the same facts in summary.json; the resume writes the
file from there first, so it survives even if the resumed evaluation stops early.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from scheduler.evaluation.config import AREAS, DecisionConfig
from scheduler.evaluation.decide import DecisionStats, questions_of
from scheduler.evaluation.items import AreaItems
from scheduler.evaluation.ollama import model_key

DECISIONS = "decisions.json"
DECISIONS_INFO = "decisions_info.json"
REPORT = "report.json"
REQUESTS = "requests"
# Settings of [llm.decision] that change what the model is asked or which decisions are flagged for review.
DECISION_SETTINGS = ("max_questions", "max_input_kib", "review_confidence")
_LOG_NUMBER = re.compile(r"^(\d+)-")


@dataclass
class EarlierDecisions:
    labels: dict[str, dict]  # id -> decision, as in decisions.json
    stats: DecisionStats
    started: str | None  # start of the evaluation that made the decisions
    last_request: int  # number of the last request log in <area>/requests/


def write_decisions_info(folder: Path, model: dict, config: DecisionConfig, started: str,
                         stats: DecisionStats) -> None:
    _write_json(folder / DECISIONS_INFO, {"started": started, "model": model, "settings": asdict(config),
                                          "stats": stats.to_json()})


def prepare_resume(step_dir: Path) -> None:
    """Before a resumed evaluation: remove what is written again, keep the decisions and their requests."""
    summary = _read(step_dir / "summary.json")
    for area in AREAS:
        folder = step_dir / area
        if (folder / DECISIONS).is_file() and not (folder / DECISIONS_INFO).is_file():
            info = _info_from_summary(summary if isinstance(summary, dict) else {}, area)
            if info is not None:
                _write_json(folder / DECISIONS_INFO, info)
        (folder / REPORT).unlink(missing_ok=True)
        for path in _logs(folder, "report"):
            path.unlink()
    for path in step_dir.glob("internal-error-*.txt") if step_dir.is_dir() else ():
        path.unlink()
    (step_dir / "summary.json").unlink(missing_ok=True)


def earlier_decisions(folder: Path, area: AreaItems, config: DecisionConfig,
                      model: dict) -> EarlierDecisions | str:
    """The area's decisions of the earlier evaluation if they can be reused, else the reason why not."""
    info, saved = _read(folder / DECISIONS_INFO), _read(folder / DECISIONS)
    if not isinstance(info, dict) or not isinstance(saved, list):
        return "there are no complete decisions of an earlier evaluation"
    earlier = info.get("model") or {}
    if model_key(str(earlier.get("name", ""))) != model_key(model["name"]) or earlier.get("digest") != model["digest"]:
        return (f"they were made by another decision model ({earlier.get('name')}, "
                f"{str(earlier.get('digest') or '?').split(':')[-1][:12]})")
    settings = info.get("settings") or {}
    changed = [key for key in DECISION_SETTINGS if settings.get(key) != getattr(config, key)]
    if changed:
        return f"[llm.decision] {', '.join(changed)} changed since"
    labels = {entry["id"]: {key: value for key, value in entry.items() if key != "id"}
              for entry in saved if isinstance(entry, dict) and "id" in entry}
    if (set(labels) != {item["id"] for item in area.items} | set(area.duplicates)
            or any(labels[duplicate].get("duplicate_of") != canonical
                   for duplicate, canonical in area.duplicates.items())):
        return "the items changed since"
    asked, last_request = _asked(folder)
    expected = json.loads(json.dumps(questions_of(area), ensure_ascii=False))  # as logged: lists, not tuples
    if any(asked.get(item_id) != question for item_id, question in expected.items()):
        return "the questions changed since (decision prompts, the items' facts or [llm.input])"
    return EarlierDecisions(labels, DecisionStats.from_json(info.get("stats") or {}), info.get("started"),
                            last_request)


def discard_decisions(folder: Path) -> None:
    """Remove decisions of an earlier evaluation that are not reused, with their requests."""
    for name in (DECISIONS, DECISIONS_INFO):
        (folder / name).unlink(missing_ok=True)
    for path in _logs(folder, "decision"):
        path.unlink()


def _info_from_summary(summary: dict, area: str) -> dict | None:
    model = (summary.get("models") or {}).get("decision")
    settings = (summary.get("settings") or {}).get("decision")
    stats = next((entry.get("decisions") for entry in summary.get("areas") or []
                  if isinstance(entry, dict) and entry.get("area") == area), None)
    if not model or not settings or not stats:
        return None
    return {"started": summary.get("started"), "model": model, "settings": settings, "stats": stats}


def _asked(folder: Path) -> tuple[dict[str, list], int]:
    """Per item: [shared input, question] of the logged decision requests; and the number of the last log."""
    asked: dict[str, list] = {}
    last = 0
    for path in sorted(_logs(folder, "*")):
        match = _LOG_NUMBER.match(path.name)
        if match:
            last = max(last, int(match.group(1)))
        if not path.name.endswith("-decision.json"):
            continue
        logged = _read(path)
        request = logged.get("request") if isinstance(logged, dict) else None
        if not isinstance(request, dict):
            continue
        for item_id, question in (request.get("questions") or {}).items():
            asked[item_id] = [request.get("state"), question]
    return asked, last


def _logs(folder: Path, kind: str) -> list[Path]:
    requests = folder / REQUESTS
    return list(requests.glob(f"*-{kind}.json")) if requests.is_dir() else []


def _read(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

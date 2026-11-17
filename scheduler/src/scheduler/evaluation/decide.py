"""Step 6, decisions: the decision model labels every item.

One request to /v1/systemone holds a batch of items: the prompt's context is
the shared input (`state`), each item is one `choice` question with the
prompt's labels as options. Ollama scores every question with its own copy of
the shared input, so the memory a request needs grows with the number of
questions. A batch therefore ends at `max_questions` or when the text the
model reads (shared input once per question, plus the questions) would exceed
`max_input_kib`.

A failed request with several questions is split in half and sent again; a
single question is retried `retries` times. Items without an answer get the
prompt's default label (REAL, PROJECT, CRITICAL, TEST_GAP) and are flagged for
review, like answers with a confidence below `review_confidence`.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from scheduler.evaluation.config import MAX_REQUEST_KIB, DecisionConfig
from scheduler.evaluation.items import AreaItems
from scheduler.evaluation.ollama import Exchange, OllamaClient
from scheduler.evaluation.prompts import DecisionPrompt, decision_prompt, render

PROMPTS = {
    "static": "security_decision",
    "dependency": "security_decision",
    "lint_group": "lint_decision",
    "test_failure": "test_failure_decision",
    "weak_file": "test_file_decision",
    "mutant": "test_mutant_decision",
}
BODY_LIMIT = (MAX_REQUEST_KIB - 4) * 1024  # the endpoint's limit, with room for the JSON around the text
PROGRESS_STEPS = 10


@dataclass
class DecisionStats:
    items: int = 0
    requests: int = 0
    failed_requests: int = 0
    split_requests: int = 0
    duration_seconds: float = 0.0
    input_tokens: int = 0
    defaulted: int = 0
    low_confidence: int = 0
    labels: Counter = field(default_factory=Counter)

    def to_json(self) -> dict:
        return {"items": self.items, "requests": self.requests, "failed_requests": self.failed_requests,
                "split_requests": self.split_requests, "duration_seconds": round(self.duration_seconds, 1),
                "input_tokens": self.input_tokens, "defaulted": self.defaulted,
                "low_confidence": self.low_confidence, "labels": dict(self.labels)}


@dataclass
class Batch:
    prompt: DecisionPrompt
    state: str
    questions: dict[str, dict]


def question_for(prompt: DecisionPrompt, item: dict) -> dict:
    shown = json.dumps({"id": item["id"], **item["data"]}, ensure_ascii=False)
    return {"type": "choice", "instructions": render(prompt.question, ITEM=shown), "criteria": prompt.labels}


def pack(items: list[dict], prompt: DecisionPrompt, state: str, config: DecisionConfig) -> list[Batch]:
    """Consecutive items in batches within the limits (at least one item per batch)."""
    state_bytes = len(state.encode())
    limit = config.max_input_kib * 1024
    batches: list[Batch] = []
    current: dict[str, dict] = {}
    read = body = 0
    for item in items:
        question = question_for(prompt, item)
        size = len(json.dumps(question, ensure_ascii=False).encode())
        if current and (len(current) >= config.max_questions or read + state_bytes + size > limit
                        or body + size + state_bytes > BODY_LIMIT):
            batches.append(Batch(prompt, state, current))
            current, read, body = {}, 0, 0
        current[item["id"]] = question
        read += state_bytes + size
        body += size
    if current:
        batches.append(Batch(prompt, state, current))
    return batches


def decide_area(client: OllamaClient, config: DecisionConfig, area: AreaItems, *,
                log: Callable[[Exchange], None] = lambda exchange: None,
                out: Callable[[str], None] = print) -> tuple[dict[str, dict], DecisionStats]:
    """Decisions for every item of the area and every duplicate, keyed by id."""
    stats = DecisionStats(items=len(area.items))
    decisions: dict[str, dict] = {}
    batches: list[Batch] = []
    for name in dict.fromkeys(PROMPTS[item["kind"]] for item in area.items):
        prompt = decision_prompt(name)
        state = render(prompt.context, RUN_CONTEXT=json.dumps(area.context, ensure_ascii=False))
        batches += pack([item for item in area.items if PROMPTS[item["kind"]] == name], prompt, state, config)
    done = 0
    next_report = 1
    for batch in batches:
        _decide(client, config, batch, decisions, stats, log)
        done += len(batch.questions)
        if stats.items and done * PROGRESS_STEPS >= next_report * stats.items and done < stats.items:
            out(f"    {area.area.replace('_', ' ')}: {done} of {stats.items} labeled ({stats.requests} requests)")
            next_report = done * PROGRESS_STEPS // stats.items + 1
    for duplicate, canonical in area.duplicates.items():
        decisions[duplicate] = {"label": "DUPLICATE", "duplicate_of": canonical, "decided_by": "tool"}
        stats.labels["DUPLICATE"] += 1
    return decisions, stats


def _decide(client: OllamaClient, config: DecisionConfig, batch: Batch, decisions: dict[str, dict],
            stats: DecisionStats, log: Callable[[Exchange], None]) -> None:
    single = len(batch.questions) == 1
    exchange = client.decide(config.model, batch.state, batch.questions, retries=None if single else 0)
    stats.requests += 1
    stats.duration_seconds += exchange.duration_seconds
    log(exchange)
    if exchange.response is None:
        stats.failed_requests += 1
        if not single:  # a smaller request needs less memory: split and try again
            stats.split_requests += 1
            ids = list(batch.questions)
            for half in (ids[: len(ids) // 2], ids[len(ids) // 2:]):
                _decide(client, config, Batch(batch.prompt, batch.state, {i: batch.questions[i] for i in half}),
                        decisions, stats, log)
            return
        (item_id,) = batch.questions
        _default(batch.prompt, item_id, decisions, stats, exchange.error or "no answer")
        return
    stats.input_tokens += int((exchange.response.get("usage") or {}).get("input_tokens") or 0)
    answers = exchange.response.get("answers") or {}
    missing = []
    for item_id in batch.questions:
        answer = answers.get(item_id) or {}
        label = answer.get("choice")
        if label not in batch.prompt.labels:
            missing.append(item_id)
            continue
        confidence = float(answer.get("confidence") or 0.0)
        review = confidence < config.review_confidence
        decisions[item_id] = {
            "label": label,
            "confidence": round(confidence, 4),
            "probabilities": {name: round(float(value), 4)
                              for name, value in (answer.get("probabilities") or {}).items()},
            "review": review,
            "decided_by": "model",
        }
        stats.labels[label] += 1
        stats.low_confidence += review
    for item_id in missing:  # asked again one by one
        if single:
            _default(batch.prompt, item_id, decisions, stats, "the answer has no valid label")
        else:
            _decide(client, config, Batch(batch.prompt, batch.state, {item_id: batch.questions[item_id]}),
                    decisions, stats, log)


def _default(prompt: DecisionPrompt, item_id: str, decisions: dict[str, dict], stats: DecisionStats,
             error: str) -> None:
    decisions[item_id] = {"label": prompt.default, "confidence": None, "probabilities": {}, "review": True,
                          "decided_by": "default", "error": error}
    stats.labels[prompt.default] += 1
    stats.defaulted += 1
    stats.low_confidence += 1

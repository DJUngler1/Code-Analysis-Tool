"""Step 6: decisions of the decision model (/v1/systemone) and the Ollama client."""

import json

import pytest
from evaluation_data import DECISION_MODEL, FakeClient, FakeOllama

from scheduler.evaluation.config import DecisionConfig
from scheduler.evaluation.decide import decide_area, pack, question_for
from scheduler.evaluation.items import AreaItems
from scheduler.evaluation.ollama import OllamaClient, OllamaError, OllamaUnavailable, model_key
from scheduler.evaluation.prompts import decision_prompt


def static_items(count, *, scope="main"):
    return [{"id": f"S-{index:04d}", "kind": "static",
             "data": {"kind": "static finding", "rule": "r", "file": f"src/f{index}.py", "line": 1, "scope": scope,
                      "message": "m" * 200}}
            for index in range(1, count + 1)]


def area_with(items, duplicates=None):
    return AreaItems("security", items=items, duplicates=duplicates or {}, context={"static_tools": ["opengrep"]})


def test_pack_respects_question_and_size_limits():
    prompt = decision_prompt("security_decision")
    state = "s" * 1000
    items = static_items(30)
    config = DecisionConfig(max_questions=8, max_input_kib=6)
    batches = pack(items, prompt, state, config)
    assert [item_id for batch in batches for item_id in batch.questions] == [item["id"] for item in items]
    for batch in batches:
        size = sum(len(json.dumps(question).encode()) + len(state) for question in batch.questions.values())
        assert len(batch.questions) <= 8
        assert size <= 6 * 1024 or len(batch.questions) == 1


def test_question_carries_the_labels_as_criteria():
    prompt = decision_prompt("security_decision")
    question = question_for(prompt, static_items(1)[0])
    assert question["type"] == "choice"
    assert list(question["criteria"]) == ["REAL", "FALSE_POSITIVE", "NOISE"]
    assert '"id": "S-0001"' in question["instructions"]


def test_decisions_labels_confidence_and_review():
    fake = FakeOllama()
    items = static_items(2) + static_items(1, scope="test")
    items[2]["id"] = "S-0003"
    items[2]["data"]["file"] = "tests/test_x.py"
    decisions, stats = decide_area(FakeClient(fake), DecisionConfig(review_confidence=0.6),
                                   area_with(items, {"S-0004": "S-0001"}), out=lambda text: None)
    assert decisions["S-0001"]["label"] == "REAL" and decisions["S-0001"]["review"] is False
    assert decisions["S-0003"] == decisions["S-0003"] | {"label": "NOISE", "review": True, "confidence": 0.4}
    assert decisions["S-0004"] == {"label": "DUPLICATE", "duplicate_of": "S-0001", "decided_by": "tool"}
    assert stats.labels == {"REAL": 2, "NOISE": 1, "DUPLICATE": 1}
    assert stats.low_confidence == 1 and stats.requests == 1
    body = fake.requests[0][1]
    assert body["model"] == DECISION_MODEL and set(body["questions"]) == {"S-0001", "S-0002", "S-0003"}


def test_failed_request_is_split_and_unanswered_items_get_the_default_label():
    fake = FakeOllama()
    calls = []

    def post(endpoint, body, timeout):
        calls.append(len(body["questions"]))
        if len(body["questions"]) > 1:
            raise OllamaError("/v1/systemone: HTTP 400: input too large", 400)
        if "S-0002" in body["questions"]:
            return {"answers": {"S-0002": {"choice": "MAYBE"}}}  # not one of the labels
        return fake.post(endpoint, body, timeout)

    client = OllamaClient("http://x", post=post, pause=0.0, retries=1)
    decisions, stats = decide_area(client, DecisionConfig(max_questions=4), area_with(static_items(3)),
                                   out=lambda text: None)
    assert calls[0] == 3 and stats.split_requests >= 1
    assert decisions["S-0001"]["decided_by"] == "model"
    assert decisions["S-0002"] == decisions["S-0002"] | {"label": "REAL", "decided_by": "default", "review": True}
    assert stats.defaulted == 1


def test_ollama_stopping_raises_after_failures_in_a_row():
    fake = FakeOllama()
    fake.fail.add("/v1/systemone")
    client = FakeClient(fake, max_failures=3)
    with pytest.raises(OllamaUnavailable, match="3 requests in a row"):
        decide_area(client, DecisionConfig(max_questions=8, retries=0), area_with(static_items(8)),
                    out=lambda text: None)
    assert len(fake.requests) == 3  # a request, then a half of it, then a half of that: then it stops


def test_the_decision_models_own_time_limit_retries_and_keep_alive():
    fake = FakeOllama()
    fake.fail.add("/v1/systemone")
    timeouts = []

    def post(endpoint, body, timeout):
        timeouts.append(timeout)
        return fake.post(endpoint, body, timeout)

    client = OllamaClient("http://x", post=post, pause=0.0, retries=5, timeout=900, keep_alive="10m",
                          max_failures=10)
    config = DecisionConfig(retries=1, timeout_seconds=30, keep_alive="1m")
    decide_area(client, config, area_with(static_items(1)), out=lambda text: None)
    assert timeouts == [30, 30]  # one item: asked 1 + retries times, with the model's time limit
    assert {body["keep_alive"] for _, body in fake.requests} == {"1m"}


def test_rejected_requests_do_not_count_as_ollama_stopping():
    def post(endpoint, body, timeout):
        raise OllamaError("HTTP 400: bad request", 400)

    client = OllamaClient("http://x", post=post, pause=0.0, retries=0, max_failures=2)
    for _ in range(5):
        assert client.decide("m", "s", {}).response is None


def test_model_key():
    assert model_key("qwen3.8") == model_key("qwen3.8:latest") == "qwen3.8:latest"
    assert model_key("registry.example/team/model") == "registry.example/team/model:latest"
    assert model_key("pplx-decider-flash:4b") == "pplx-decider-flash:4b"

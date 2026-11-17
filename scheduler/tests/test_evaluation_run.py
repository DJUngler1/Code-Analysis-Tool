"""Step 6 as a whole: preparation, decisions, reports, audit, with a fake Ollama."""

import json

import pytest
from evaluation_data import DECISION_MODEL, REPORT_MODEL, FakeClient, FakeOllama, make_run

from scheduler.config import Settings
from scheduler.evaluation import run_evaluation
from scheduler.evaluation.audit import language_of, main as audit_main, write_audit
from scheduler.linting import Status

from conftest import REPOSITORY


def settings_for(config_file=REPOSITORY / "code-analysis.toml"):
    return Settings(projects_dir=None, host_home=None, ollama_base_url="http://ollama.invalid",
                    config_file=config_file)


def evaluate(run, fake=None, *, settings=None, client=None):
    fake = fake or FakeOllama()
    client = client or FakeClient(fake)
    lines = []
    result = run_evaluation(run, settings or settings_for(), client=client, out=lines.append)
    return result, fake, "\n".join(lines)


def read(path):
    return json.loads(path.read_text())


def test_full_evaluation(tmp_path):
    run = make_run(tmp_path)
    result, fake, out = evaluate(run)
    assert result.complete, result.errors
    assert [area.status for area in result.areas] == [Status.OK, Status.OK, Status.OK]
    assert fake.most_loaded == 1 and fake.loaded == []  # never both models, both unloaded at the end
    step = run / "step6-llm-evaluation"
    summary = read(step / "summary.json")
    assert summary["complete"] and summary["audit"] == ["audit.md", "audit.json"]
    assert summary["models"]["decision"]["name"] == DECISION_MODEL
    assert summary["models"]["report"]["name"] == REPORT_MODEL
    security = next(area for area in summary["areas"] if area["area"] == "security")
    assert security["items"] == 4 and security["duplicates"] == 2
    assert security["decisions"]["labels"] == {"REAL": 3, "NOISE": 1, "DUPLICATE": 2}
    decisions = {entry["id"]: entry for entry in read(step / "security" / "decisions.json")}
    assert decisions["S-0002"] == {"id": "S-0002", "label": "DUPLICATE", "duplicate_of": "S-0001",
                                   "decided_by": "tool"}
    report = read(step / "security" / "report.json")
    assert report["texts_missing"] == 0 and report["entries"][0]["title"] == "Title of E-001"
    requests = sorted(path.name for path in (step / "security" / "requests").iterdir())
    assert requests[0] == "001-decision.json" and requests[-1].endswith("-report.json")
    logged = read(step / "security" / "requests" / requests[-1])
    assert logged["response"]["message"]["thinking"] == "..."  # the model's reasoning is kept
    assert (run / "audit.md").is_file() and read(run / "audit.json")["key_figures"]["security"]["real"] == 3
    assert "Labeling with pplx-decider-flash:latest" in out


def test_order_decisions_first_then_reports(tmp_path):
    run = make_run(tmp_path)
    _, fake, _ = evaluate(run)
    endpoints = [endpoint for endpoint, _ in fake.requests if endpoint != "/api/generate"]
    last_decision = max(index for index, endpoint in enumerate(endpoints) if endpoint == "/v1/systemone")
    first_chat = endpoints.index("/api/chat")
    assert last_decision < first_chat


def test_reasoning_model_loaded_from_an_earlier_run_is_unloaded_first(tmp_path):
    run = make_run(tmp_path)
    fake = FakeOllama()
    fake.loaded = [REPORT_MODEL]
    result, fake, _ = evaluate(run, fake)
    assert result.complete and fake.most_loaded == 1


def test_areas_without_their_steps_are_skipped(tmp_path):
    run = make_run(tmp_path, steps=(2,))
    result, fake, out = evaluate(run)
    assert result.complete
    assert [(area.area, area.status) for area in result.areas] == [
        ("security", Status.SKIPPED), ("code_quality", Status.OK), ("test_quality", Status.SKIPPED)]
    assert "security       skipped: Steps 3 and 4 did not run" in out
    assert "Not evaluated." in (run / "audit.md").read_text()


def test_areas_not_configured_are_skipped(tmp_path):
    config = tmp_path / "code-analysis.toml"
    config.write_text('[llm]\nareas = ["code_quality"]\n')
    run = make_run(tmp_path)
    result, fake, _ = evaluate(run, settings=settings_for(config))
    assert [area.status for area in result.areas] == [Status.SKIPPED, Status.OK, Status.SKIPPED]
    assert not (run / "step6-llm-evaluation" / "security").exists()


def test_nothing_to_evaluate_needs_no_ollama(tmp_path):
    run = make_run(tmp_path, steps=())
    result, fake, _ = evaluate(run, client=FakeClient(FakeOllama(), reachable=False))
    assert result.complete and result.audit is None
    assert all(area.status is Status.SKIPPED for area in result.areas)


def test_missing_model_stops_with_a_hint(tmp_path):
    run = make_run(tmp_path)
    result, fake, _ = evaluate(run, FakeOllama(installed=(DECISION_MODEL,)))
    assert not result.complete
    assert result.errors == [f"the report model {REPORT_MODEL} is not installed in Ollama. Install it on the host "
                             f"with `ollama pull {REPORT_MODEL}`, or change [llm.report] model in "
                             "code-analysis.toml."]
    assert fake.requests == []
    assert all(area.status is Status.FAILED for area in result.areas)
    assert read(run / "step6-llm-evaluation" / "summary.json")["complete"] is False


def test_unreachable_ollama(tmp_path):
    run = make_run(tmp_path)
    result, _, _ = evaluate(run, client=FakeClient(FakeOllama(), reachable=False))
    assert not result.complete and "not reachable" in result.errors[0]
    assert result.audit is None


def test_ollama_stopping_during_the_reports(tmp_path):
    run = make_run(tmp_path)
    fake = FakeOllama()
    fake.fail.add("/api/chat")
    result, fake, _ = evaluate(run, fake, client=FakeClient(fake, retries=0, max_failures=3))
    assert not result.complete and "Ollama stopped answering" in result.errors[0]
    security = result.areas[0]
    assert security.status is Status.FAILED
    # The decisions and the report structure are kept; the texts are missing.
    report = read(run / "step6-llm-evaluation" / "security" / "report.json")
    assert report["texts_missing"] == 4 and report["texts"]["summary_text"] is None
    assert (run / "audit.md").is_file()
    assert sum(1 for endpoint, _ in fake.requests if endpoint == "/api/chat") == 3


def test_partial_texts_make_the_area_partial(tmp_path):
    run = make_run(tmp_path)
    fake = FakeOllama()
    fake.drop_ids = {"E-001"}
    result, _, _ = evaluate(run, fake)
    assert not result.complete
    assert result.areas[0].status is Status.PARTIAL and result.areas[0].report.texts_missing == 1


def test_invalid_llm_config(tmp_path):
    config = tmp_path / "code-analysis.toml"
    config.write_text("[llm.decision]\nmax_questions = 100\n")
    result, fake, _ = evaluate(make_run(tmp_path), settings=settings_for(config))
    assert not result.complete and "max_questions" in result.errors[0] and fake.requests == []


@pytest.mark.parametrize("language, expected", [("English", "en"), ("German", "de"), ("Deutsch", "de"),
                                                ("french", "en")])
def test_audit_language(language, expected):
    assert language_of(language) == expected


def test_audit_in_german_and_standalone(tmp_path, capsys):
    config = tmp_path / "code-analysis.toml"
    config.write_text('[llm]\nreport_language = "German"\n')
    run = make_run(tmp_path)
    evaluate(run, settings=settings_for(config))
    text = (run / "audit.md").read_text()
    assert "## 3. Sicherheit" in text and "### REAL-Befunde (3)" in text
    (run / "audit.md").unlink()
    assert audit_main([str(run)]) == 0
    assert (run / "audit.md").read_text() == text
    assert audit_main([str(tmp_path)]) == 1 and "step 6 did not run" in capsys.readouterr().out


def test_audit_content(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    write_audit(run)
    text = (run / "audit.md").read_text()
    for expected in ("# Code analysis audit: project", "| 6 – LLM evaluation | complete |",
                     "- Security: 3 REAL findings (1 error, 1 high, 1 medium) of 6 findings; 1 filtered, "
                     "2 duplicates merged.",
                     "| E-001 | high | Title of E-001 | lodash@4.17.20 | Fix E-001. |",
                     "<summary>Filtered (1)</summary>", "```diff\n@@ -1,3 +1,3 @@",
                     "| Python (requirements.txt) | coverage.py | ok | 60.0% | – |",
                     "| Security | E-004 | NOISE | 0.40 (low confidence) | Title of E-004 | – |",
                     "pplx-decider-flash:latest (000000000000)"):
        assert expected in text, expected
    audit = read(run / "audit.json")
    assert audit["areas"]["security"]["report"]["statistics"]["input_findings"] == 6
    assert [entry["id"] for entry in audit["review"]] == ["E-004", "G-003", "E-001"]


def test_a_second_evaluation_replaces_the_first(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    config = tmp_path / "code-analysis.toml"
    config.write_text('[llm]\nareas = ["code_quality"]\n')
    result, _, _ = evaluate(run, settings=settings_for(config))
    step = run / "step6-llm-evaluation"
    assert sorted(path.name for path in step.iterdir()) == ["code_quality", "summary.json"]
    assert sorted(path.name for path in (step / "code_quality" / "requests").iterdir()) == [
        "001-decision.json", "002-report.json", "003-report.json"]
    assert "Not evaluated." in (run / "audit.md").read_text()

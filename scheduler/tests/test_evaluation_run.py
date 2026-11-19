"""Step 6 as a whole: preparation, decisions, reports, audit, with a fake Ollama."""

import json

import pytest
from evaluation_data import (DECISION_MODEL, INSTALLED, REPORT_MODEL, FakeClient, FakeOllama, add_quality_findings,
                             add_security_groups, make_run)

from scheduler.config import Settings
from scheduler.evaluation import run_evaluation
from scheduler.evaluation.audit import language_of, main as audit_main, write_audit
from scheduler.evaluation.config import AREAS
from scheduler.linting import Status

from conftest import REPOSITORY


def settings_for(config_file=REPOSITORY / "code-analysis.toml"):
    return Settings(projects_dir=None, host_home=None, ollama_base_url="http://ollama.invalid",
                    config_file=config_file)


def evaluate(run, fake=None, *, settings=None, client=None, resume=False):
    fake = fake or FakeOllama()
    client = client or FakeClient(fake)
    lines = []
    result = run_evaluation(run, settings or settings_for(), resume=resume, client=client, out=lines.append)
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
    assert f"Labeling with {DECISION_MODEL}" in out


def test_static_findings_without_security_category_go_to_code_quality(tmp_path):
    run = make_run(tmp_path)
    add_quality_findings(run)
    result, fake, out = evaluate(run)
    assert result.complete, result.errors
    step = run / "step6-llm-evaluation"
    security = [item["id"] for item in read(step / "security" / "items.json")["items"]]
    assert security == ["S-0001", "S-0003", "S-0007", "S-0008", "S-0009", "D-0001", "D-0003"]
    quality = read(step / "code_quality" / "items.json")["items"]
    assert sorted(item["kind"] for item in quality).count("static_group") == 2
    # Both kinds of group are asked with the code quality prompt.
    questions = [question["instructions"] for endpoint, body in fake.requests if endpoint == "/v1/systemone"
                 for question in body["questions"].values()]
    assert any("python:S1192" in text and "Which label fits this group of findings?" in text for text in questions)
    assert "2 groups of static analysis findings" in out
    text = (run / "audit.md").read_text()
    assert ("3 findings of the static code analysis are in code quality (category neither security, correctness "
            "nor reliability).") in text
    assert "; 3 findings from the static code analysis);" in text
    assert "| sonarqube / python:S1192 (maintainability) | main | 2 |" in text


def test_settings_per_area_and_input_limits(tmp_path):
    config = tmp_path / "code-analysis.toml"
    config.write_text('[llm.input]\nmessage_chars = 10\n[llm.report]\nthink = "low"\nkeep_alive = "2m"\n'
                      '[llm.report.security]\nthink = "high"\n')
    run = make_run(tmp_path)
    result, fake, _ = evaluate(run, settings=settings_for(config))
    assert result.complete, result.errors
    chats = [body for endpoint, body in fake.requests if endpoint == "/api/chat"]
    security = [body for body in chats if "security analyst" in body["messages"][0]["content"]]
    others = [body for body in chats if body not in security]
    assert security and {body["think"] for body in security} == {"high"}
    assert others and {body["think"] for body in others} == {"low"}
    assert {body["keep_alive"] for body in chats} == {"2m"}
    items = read(run / "step6-llm-evaluation" / "security" / "items.json")["items"]
    assert items[0]["data"]["message"] == "SQL built…"  # message_chars = 10
    assert "- Models (reports, Security): think high" in (run / "audit.md").read_text()


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
    config = tmp_path / "code-analysis.toml"
    config.write_text("[llm.report]\nretries = 0\n")
    run = make_run(tmp_path)
    fake = FakeOllama()
    fake.fail.add("/api/chat")
    result, fake, _ = evaluate(run, fake, client=FakeClient(fake, max_failures=3), settings=settings_for(config))
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
    assert "#### Fundstellen der REAL-Gruppen" in text and "src/app.py:1 (+1 weitere)" in text
    assert "<summary>G-001 – ruff check / F401: 2 Befunde in 2 Dateien</summary>" in text
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
                     "| E-001 | high | 1 | Title of E-001 | lodash@4.17.20 | Fix E-001. |",
                     "<summary>Filtered (1)</summary>", "```diff\n@@ -1,3 +1,3 @@",
                     "| Python (requirements.txt) | coverage.py | ok | 60.0% | – |",
                     "| Security | E-004 | NOISE | 0.40 (low confidence) | Title of E-004 | – |",
                     # Code quality: where the findings are, in the table and as a list per REAL group.
                     "| G-001 | error | ruff check / F401 | main | 2 | src/app.py:1 (+1 more) | Title of G-001 |",
                     "#### Locations of the REAL groups",
                     "<summary>G-001 – ruff check / F401: 2 findings in 2 files</summary>",
                     "- `src/app.py`: 1\n- `src/other.py`: 1",
                     "<summary>G-002 – biome / lint/style/useTemplate: 1 finding in 1 file</summary>",
                     "| G-003 | NOISE | ruff check / E501 | test | 1 | tests/test_app.py:1 | Reason for G-003. |",
                     f"{DECISION_MODEL} (000000000000)"):
        assert expected in text, expected
    audit = read(run / "audit.json")
    assert audit["areas"]["security"]["report"]["statistics"]["input_findings"] == 6
    assert [entry["id"] for entry in audit["review"]] == ["E-004", "G-003", "E-001"]


def test_security_groups_in_the_audit(tmp_path):
    run = make_run(tmp_path)
    add_security_groups(run)
    result, fake, _ = evaluate(run)
    assert result.complete, result.errors
    text = (run / "audit.md").read_text()
    for expected in ("### REAL findings (9)",
                     "| E-004 | medium | 3 | Title of E-004 | tomcat-embed-core@9.0.1 | Fix E-004. |",
                     "| E-006 | note | 2 | Title of E-006 | src/app.py:1 (+1 more) | Fix E-006. |",
                     # Every finding of a group stays in the audit, as a collapsible list.
                     "<summary>E-004 – tomcat-embed-core: 3 findings</summary>",
                     "- GHSA-aaaa-0000-0001 (medium): 9.0.1 → 9.0.40",
                     "<summary>E-006 – opengrep / c.raptor-typos: 2 findings in 1 file</summary>",
                     "- `src/app.py`: 1, 3",
                     "<summary>Filtered (3)</summary>",
                     "#### Locations of the filtered groups",
                     "<summary>E-008 – opengrep / c.raptor-typos: 2 findings in 1 file</summary>",
                     "- `tests/test_app.py`: 1, 3",
                     # The fake decides tests/ with low confidence: both findings of the group.
                     "| Security | E-008 | NOISE | 2 / 2 low confidence | Title of E-008 | – |"):
        assert expected in text, expected
    sent = [body["messages"][1]["content"] for endpoint, body in fake.requests
            if endpoint == "/api/chat" and "security analyst" in body["messages"][0]["content"]]
    assert any('"id": "E-008",\n  "label": "NOISE",\n  "count": 2,\n  "low_confidence": true,\n'
               '  "low_confidence_findings": 2' in content for content in sent)


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


# --- --resume: the decisions of the earlier evaluation are reused, the reports written again ---


def decision_requests(fake):
    return [body for endpoint, body in fake.requests if endpoint == "/v1/systemone"]


def logs(step, area, kind):
    return sorted(path.name for path in (step / area / "requests").glob(f"*-{kind}.json"))


def test_resume_reuses_the_decisions_after_the_reports_failed(tmp_path):
    config = tmp_path / "code-analysis.toml"
    config.write_text("[llm.report]\nretries = 0\n")
    run = make_run(tmp_path)
    failing = FakeOllama()
    failing.fail.add("/api/chat")
    first, _, _ = evaluate(run, failing, client=FakeClient(failing, max_failures=3), settings=settings_for(config))
    assert not first.complete
    step = run / "step6-llm-evaluation"
    started = read(step / "summary.json")["started"]
    decisions = {area: (step / area / "decisions.json").read_text() for area in AREAS}
    kept = {area: logs(step, area, "decision") for area in AREAS}
    assert logs(step, "security", "report")  # the failed report requests of the first evaluation

    result, fake, out = evaluate(run, settings=settings_for(config), resume=True)
    assert result.complete, result.errors
    assert decision_requests(fake) == []  # nothing labeled again
    assert "Labeling with" not in out and f"security: reused (evaluation started {started})" in out
    for area in AREAS:
        assert (step / area / "decisions.json").read_text() == decisions[area]
        assert logs(step, area, "decision") == kept[area]
        reports = logs(step, area, "report")  # only the new ones, numbered after the kept requests
        assert reports and (not kept[area] or reports[0] > kept[area][-1])
    summary = read(step / "summary.json")
    security = next(area for area in summary["areas"] if area["area"] == "security")
    assert security["status"] == "ok" and security["decisions_reused_from"] == started
    assert security["decisions"]["labels"] == {"REAL": 3, "NOISE": 1, "DUPLICATE": 2}
    assert read(step / "security" / "report.json")["texts_missing"] == 0
    assert (f"- Decisions (Security, Code quality, Test quality) taken over from the evaluation started {started}"
            in (run / "audit.md").read_text())


def test_resume_of_an_evaluation_without_decisions_info(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    step = run / "step6-llm-evaluation"
    started = read(step / "summary.json")["started"]
    for area in AREAS:  # as written before decisions_info.json existed: the facts are only in summary.json
        (step / area / "decisions_info.json").unlink()
    result, fake, _ = evaluate(run, resume=True)
    assert result.complete and decision_requests(fake) == []
    assert read(step / "security" / "decisions_info.json")["started"] == started


def changed_config(tmp_path, old, new):
    """The repository's code-analysis.toml with one value changed."""
    text = (REPOSITORY / "code-analysis.toml").read_text()
    assert old in text
    config = tmp_path / "code-analysis.toml"
    config.write_text(text.replace(old, new, 1))
    return settings_for(config)


def test_resume_labels_again_when_the_decision_settings_changed(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    settings = changed_config(tmp_path, "review_confidence = 0.6", "review_confidence = 0.5")
    result, fake, out = evaluate(run, settings=settings, resume=True)
    assert result.complete and decision_requests(fake)
    assert "security: labeled again: [llm.decision] review_confidence changed since" in out
    step = run / "step6-llm-evaluation"
    assert logs(step, "security", "decision")[0] == "001-decision.json"  # the earlier requests are removed
    summary = read(step / "summary.json")
    assert read(step / "security" / "decisions_info.json")["started"] == summary["started"]
    assert all(area["decisions_reused_from"] is None for area in summary["areas"])


def test_resume_labels_again_when_the_questions_changed(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    settings = changed_config(tmp_path, "message_chars = 300", "message_chars = 10")
    result, fake, out = evaluate(run, settings=settings, resume=True)
    assert result.complete and decision_requests(fake)
    assert "security: labeled again: the questions changed since" in out


def test_resume_labels_again_with_another_decision_model(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    other = FakeOllama(installed=tuple(reversed(INSTALLED)))  # same name, another digest
    result, fake, out = evaluate(run, other, resume=True)
    assert result.complete and decision_requests(fake)
    assert "security: labeled again: they were made by another decision model" in out


def test_resume_without_an_earlier_evaluation_labels_everything(tmp_path):
    run = make_run(tmp_path)
    result, fake, out = evaluate(run, resume=True)
    assert result.complete and decision_requests(fake)
    assert "security: labeled again: there are no complete decisions of an earlier evaluation" in out


def test_a_failed_resume_keeps_the_decisions(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    failed, _, _ = evaluate(run, client=FakeClient(FakeOllama(), reachable=False), resume=True)
    assert not failed.complete
    result, fake, _ = evaluate(run, resume=True)
    assert result.complete and decision_requests(fake) == []


def test_resume_removes_areas_that_are_skipped_now(tmp_path):
    run = make_run(tmp_path)
    evaluate(run)
    settings = changed_config(tmp_path, 'areas = ["security", "code_quality", "test_quality"]',
                              'areas = ["code_quality"]')
    result, fake, _ = evaluate(run, settings=settings, resume=True)
    assert result.complete and decision_requests(fake) == []
    step = run / "step6-llm-evaluation"
    assert sorted(path.name for path in step.iterdir()) == ["code_quality", "summary.json"]


# --- threat model (written into the run folder before step 2): security only ---


THREAT_MODEL = "# Threat model\n\n## Untrusted inputs (trust boundaries)\n\n- Players send image files."


def with_threat_model(run, text=THREAT_MODEL):
    from scheduler.threat_model import ThreatModel, copy_to_run

    copy_to_run(run, ThreatModel(text, "results", run / "threat-model.md", "2026-11-18T08:00:00", "abc123def4567"))


def test_the_threat_model_reaches_only_the_security_prompts(tmp_path):
    run = make_run(tmp_path)
    with_threat_model(run)
    result, fake, _ = evaluate(run)
    assert result.complete, result.errors
    states = [body["state"] for endpoint, body in fake.requests if endpoint == "/v1/systemone"]
    security = [state for state in states if "security analysis" in state]
    assert security and all("Players send image files." in state for state in security)
    assert all("Players send image files." not in state for state in states if state not in security)
    systems = [body["messages"][0]["content"] for endpoint, body in fake.requests if endpoint == "/api/chat"]
    assert any("security analyst" in system and "Players send image files." in system for system in systems)
    assert all("Players send image files." not in system for system in systems if "security analyst" not in system)
    step = run / "step6-llm-evaluation"
    info = read(step / "summary.json")["threat_model"]
    assert info["source"] == "results" and info["sha256"] == "abc123def4567" and not info["shortened"]
    assert read(step / "security" / "items.json")["threat_model"].startswith("# Threat model")
    text = (run / "audit.md").read_text()
    assert ("- Threat model (confirmed in the tool on 2026-11-18T08:00:00, SHA-256 abc123def456): used for the "
            "security decisions and texts.") in text
    assert "<summary>Threat model</summary>" in text and "**Untrusted inputs (trust boundaries)**" in text


def test_without_threat_model_the_audit_says_so(tmp_path):
    run = make_run(tmp_path)
    result, _, _ = evaluate(run)
    assert result.complete and result.areas[0].notes[-1].startswith("No threat model in the run folder")
    assert read(run / "step6-llm-evaluation" / "summary.json")["threat_model"] is None
    assert "- No threat model: the security decisions and texts were made without one." in (
        run / "audit.md").read_text()


def test_resume_labels_security_again_when_the_threat_model_changed(tmp_path):
    run = make_run(tmp_path)
    with_threat_model(run)
    evaluate(run)
    with_threat_model(run, "# Threat model\n\nNo untrusted inputs: a command line tool for its own developers.")
    result, fake, out = evaluate(run, resume=True)
    assert result.complete, result.errors
    assert "security: labeled again: the questions changed since" in out
    assert "code quality: reused" in out and "test quality: reused" in out
    assert any("command line tool for its own developers" in body["state"]
               for endpoint, body in fake.requests if endpoint == "/v1/systemone")


def test_decisions_info_names_what_the_model_saw(tmp_path):
    run = make_run(tmp_path)
    with_threat_model(run)
    evaluate(run)
    info = read(run / "step6-llm-evaluation" / "security" / "decisions_info.json")
    from dataclasses import asdict

    from scheduler.evaluation.config import load_config

    assert info["input"] == asdict(load_config(REPOSITORY / "code-analysis.toml").input)  # the settings of the run
    assert info["threat_model"]["sha256"] == "abc123def4567"
    assert read(run / "step6-llm-evaluation" / "code_quality" / "decisions_info.json")["threat_model"] is None

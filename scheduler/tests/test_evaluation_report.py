"""Step 6: reports – the structure Python builds and the texts of the reasoning model."""

import json

import pytest
from evaluation_data import PATCH, FakeClient, FakeOllama, add_quality_findings, add_security_groups, make_run

from scheduler.evaluation.config import ConfigError, InputConfig, ReportConfig, load_config
from scheduler.evaluation.items import SourceReader, code_quality_items, project_of, security_items, test_items
from scheduler.evaluation.report import (TextWriter, build_report, locations_by_file, patch_example, patch_sample,
                                         patch_stats, validate_summary, validate_texts, write_texts)


def decisions_for(area, labels=None):
    labels = labels or {}
    decisions = {item["id"]: {"label": labels.get(item["id"], "REAL"), "confidence": 0.9, "review": False,
                              "decided_by": "model"} for item in area.items}
    decisions |= {duplicate: {"label": "DUPLICATE", "duplicate_of": canonical, "decided_by": "tool"}
                  for duplicate, canonical in area.duplicates.items()}
    return decisions


def test_security_report_keeps_every_finding_exactly_once(tmp_path):
    run = make_run(tmp_path)
    area = security_items(run, SourceReader(project_of(run)))
    draft = build_report(area, decisions_for(area, {"S-0003": "NOISE"}), run)
    report = draft.report
    source_ids = [source for entry in report["entries"] for source in entry["source_ids"]]
    assert sorted(source_ids) == ["D-0001", "D-0002", "D-0003", "S-0001", "S-0002", "S-0003"]
    assert report["statistics"]["input_findings"] == 6
    assert report["statistics"]["by_label"] == {"REAL": 3, "NOISE": 1, "DUPLICATE": 2}
    assert report["statistics"]["real_by_severity"] == {"error": 1, "high": 1, "medium": 1}
    # REAL first (by severity), then the filtered ones; numbered in that order.
    assert [(entry["entry_id"], entry["label"]) for entry in report["entries"]] == [
        ("E-001", "REAL"), ("E-002", "REAL"), ("E-003", "REAL"), ("E-004", "NOISE")]
    sql = next(entry for entry in report["entries"] if entry["source_ids"][0] == "S-0001")
    assert sql["tools"] == ["opengrep", "sonarqube"]
    assert sql["identifiers"]["rule_ids"] == ["python.sql-injection", "pythonsecurity:S3649"]
    assert sql["locations"] == [{"file": "src/app.py", "line": 4}] and sql["package"] is None
    lodash = next(entry for entry in report["entries"] if entry["source_ids"][0] == "D-0001")
    assert lodash["identifiers"]["cve"] == ["CVE-2021-23337"] and lodash["package"]["fixed_versions"] == ["4.17.21"]
    assert all(entry["title"] is None for entry in report["entries"])  # texts come from the model
    assert set(draft.inputs) == {entry["entry_id"] for entry in report["entries"]}


NOISE_IN_TESTS = {"S-0003": "NOISE", "S-0006": "NOISE", "S-0007": "NOISE"}


def test_security_report_groups_by_rule_and_label(tmp_path):
    run = make_run(tmp_path)
    add_security_groups(run)
    area = security_items(run, SourceReader(project_of(run)))
    limits = InputConfig(group_samples=2, group_samples_with_code=1)
    draft = build_report(area, decisions_for(area, NOISE_IN_TESTS), run, limits)
    report = draft.report
    # REAL of high, error or critical: one entry each; the others by rule (or package) and label; a group of one
    # is a single entry. Sorted by label, severity, size.
    assert [(entry["entry_id"], entry["label"], entry["count"], entry["source_ids"])
            for entry in report["entries"]] == [
        ("E-001", "REAL", 1, ["D-0001", "D-0002"]),
        ("E-002", "REAL", 1, ["S-0001", "S-0002"]),
        ("E-003", "REAL", 1, ["S-0008"]),
        ("E-004", "REAL", 3, ["D-0004", "D-0005", "D-0006"]),
        ("E-005", "REAL", 1, ["D-0003"]),
        ("E-006", "REAL", 2, ["S-0004", "S-0005"]),
        ("E-007", "NOISE", 1, ["S-0003"]),
        ("E-008", "NOISE", 2, ["S-0006", "S-0007"]),
    ]
    stats = report["statistics"]
    assert (stats["input_findings"], stats["report_entries"], stats["groups"]) == (14, 8, 3)
    assert stats["by_label"] == {"REAL": 9, "NOISE": 3, "DUPLICATE": 2}
    assert stats["real_by_severity"] == {"error": 2, "high": 1, "medium": 4, "note": 2}  # per finding
    typos = report["entries"][5]
    assert typos["locations"] == [{"file": "src/app.py", "line": 1}, {"file": "src/app.py", "line": 3}]
    assert (typos["files"], typos["severities"], typos["identifiers"]["rule_ids"]) == (1, {"note": 2},
                                                                                       ["c.raptor-typos"])
    # Every finding stays in the report, with its own decision.
    assert [(finding["source_ids"], finding["confidence"]) for finding in typos["findings"]] == [
        (["S-0004"], 0.9), (["S-0005"], 0.9)]
    tomcat = report["entries"][3]
    assert tomcat["package"]["installed_versions"] == ["9.0.1"]
    assert tomcat["package"]["fixed_versions"] == ["9.0.40", "9.0.43"]
    assert [finding["identifiers"]["other"] for finding in tomcat["findings"]] == [
        ["GHSA-aaaa-0000-0001"], ["GHSA-aaaa-0000-0002"], ["GHSA-aaaa-0000-0003"]]
    # The model sees a group with its size and a few samples, the first ones with code.
    shown = draft.inputs["E-006"]
    assert (shown["id"], shown["label"], shown["count"], shown["rule"]) == ("E-006", "REAL", 2, "c.raptor-typos")
    assert [sample["line"] for sample in shown["samples"]] == [1, 3]
    assert "code" in shown["samples"][0] and "code" not in shown["samples"][1]
    assert draft.inputs["E-004"]["package"] == "tomcat-embed-core" and len(draft.inputs["E-004"]["samples"]) == 2
    assert draft.inputs["E-003"]["file"] == "src/other.py"  # a single finding as before
    assert [entry["count"] for entry in draft.facts["real_entries"]] == [1, 1, 1, 3, 1, 2]


def test_security_single_severity(tmp_path):
    run = make_run(tmp_path)
    add_security_groups(run)
    area = security_items(run, SourceReader(project_of(run)))
    report = build_report(area, decisions_for(area, NOISE_IN_TESTS), run,
                          InputConfig(security_single_severity="note")).report
    # Every REAL finding is an entry of its own; NOISE is still grouped.
    assert [entry["count"] for entry in report["entries"]] == [1] * 9 + [1, 2]
    assert report["statistics"]["groups"] == 1


@pytest.mark.parametrize("value, error", [('"Critical"', None), ('"urgent"', "must be one of")])
def test_security_single_severity_setting(tmp_path, value, error):
    config = tmp_path / "code-analysis.toml"
    config.write_text(f"[llm.input]\nsecurity_single_severity = {value}\n")
    if error is None:
        assert load_config(config).input.security_single_severity == "critical"
    else:
        with pytest.raises(ConfigError, match=error):
            load_config(config)


def test_lint_report_statistics_and_patch(tmp_path):
    run = make_run(tmp_path)
    area = code_quality_items(run, SourceReader(project_of(run)))
    noise = next(item["id"] for item in area.items if item["data"]["rule"] == "E501")
    report = build_report(area, decisions_for(area, {noise: "NOISE"}), run).report
    stats = report["statistics"]
    assert stats["input_findings"] == 5 and stats["exact_duplicates"] == 1
    assert stats["groups_by_label"] == {"REAL": 2, "NOISE": 1}
    assert stats["findings_by_label"] == {"REAL": 3, "NOISE": 1}
    assert stats["real_findings_by_severity"] == {"error": 2, "note": 1}
    assert [group["group_id"] for group in report["groups"]] == ["G-001", "G-002", "G-003"]
    assert report["groups"][0]["rule_id"] == "F401"  # REAL, highest severity first
    assert report["groups"][0]["locations"] == [{"file": "src/app.py", "lines": [1]},
                                                {"file": "src/other.py", "lines": [1]}]
    changes = report["formatter_changes"]
    assert changes["tools"] == ["biome"]
    assert (changes["files_changed"], changes["lines_added"], changes["lines_removed"]) == (1, 2, 2)
    assert changes["example"]["file"] == "src/util.js"
    assert changes["example"]["diff"].splitlines()[0] == "@@ -1,3 +1,3 @@"
    assert report["excluded_files"] == {"total": 1, "groups": [
        {"reason_or_pattern": "generated: header comment", "count": 1, "examples": ["src/gen.py"]}]}


def test_static_findings_in_the_reports(tmp_path):
    run = make_run(tmp_path)
    add_quality_findings(run)
    reader = SourceReader(project_of(run))
    security_area = security_items(run, reader)
    security = build_report(security_area, decisions_for(security_area), run).report
    assert security["statistics"]["input_findings"] == 9  # 6 of step 3, 3 of step 4
    assert security["statistics"]["static_findings_in_code_quality"] == 3
    quality_area = code_quality_items(run, reader)
    draft = build_report(quality_area, decisions_for(quality_area), run)
    stats = draft.report["statistics"]
    assert (stats["input_findings"], stats["lint_findings"], stats["static_findings"]) == (8, 5, 3)
    smell = next(group for group in draft.report["groups"] if group["rule_id"] == "python:S1192")
    assert smell["category"] == "maintainability" and smell["count"] == 2
    assert {"id": smell["group_id"], "tool": "sonarqube", "rule": "python:S1192", "category": "maintainability",
            "scope": "main", "count": 2} in draft.facts["real_groups"]


def test_locations_by_file():
    assert locations_by_file([{"file": "a.py", "line": 3}, {"file": "a.py", "line": 9}, {"file": "b.py", "line": None},
                              {"file": "c.py", "line": 1}]) == [{"file": "a.py", "lines": [3, 9]},
                                                                {"file": "b.py", "lines": []},
                                                                {"file": "c.py", "lines": [1]}]


def test_patch_helpers():
    assert patch_stats(PATCH) == {"files_changed": 1, "lines_added": 2, "lines_removed": 2}
    example = patch_example(PATCH, limit=3)
    assert example == {"file": "src/util.js", "diff": "@@ -1,3 +1,3 @@\n-const a = 'x' + b\n+const a = `x${b}`;"}
    assert patch_example("") is None
    assert patch_sample(PATCH, 10_000).startswith("--- src/util.js\n@@")
    assert patch_sample(PATCH, 10) == ""


def test_test_report(tmp_path):
    run = make_run(tmp_path)
    area = test_items(run, SourceReader(project_of(run)))
    labels = {"T-001": "PROJECT", "F-0001": "NON_CRITICAL", "F-0002": "CRITICAL", "M-0001": "TEST_GAP"}
    report = build_report(area, decisions_for(area, labels), run).report
    kinds = [(entry["entry_id"], entry["kind"], entry["label"], entry["source_ids"]) for entry in report["entries"]]
    assert kinds == [("E-001", "failed_tests", "PROJECT", ["T-001"]),
                     ("E-002", "weakly_tested_file", "CRITICAL", ["F-0002"]),  # CRITICAL before NON_CRITICAL
                     ("E-003", "weakly_tested_file", "NON_CRITICAL", ["F-0001"]),
                     ("E-004", "surviving_mutant", "TEST_GAP", ["M-0001"])]
    stats = report["statistics"]
    assert stats["tests"] == {"passed": 1, "failed": 2, "error": 0, "skipped": 0}
    assert stats["mutation"][0]["score"] == 0.5 and stats["coverage"][0]["line_rate"] == 0.6


def test_validate_texts_by_label():
    texts = {"id": "E-001", "title": " T ", "description": "D", "recommendation": "R", "reason": "x",
             "review_hint": "null"}
    assert validate_texts(texts, "REAL") == {"title": "T", "description": "D", "recommendation": "R",
                                             "reason": None, "review_hint": None}
    assert validate_texts(texts, "NOISE") == {"title": "T", "description": None, "recommendation": None,
                                              "reason": "x", "review_hint": None}
    assert validate_texts(texts | {"recommendation": ""}, "TEST_GAP") is None
    assert validate_texts(texts | {"reason": ""}, "LOW_VALUE") is None
    assert validate_texts(texts | {"title": ""}, "REAL") is None
    assert validate_summary({"summary_text": "S", "change_types": ["a", ""]},
                            {"summary_text": "text", "change_types": "list"}) == {"summary_text": "S",
                                                                                  "change_types": ["a"]}
    assert validate_summary({"summary_text": ""}, {"summary_text": "text"}) is None


def writer_for(fake, area, **config):
    return TextWriter(FakeClient(fake), ReportConfig(**config), area=area, language="English", retries=1,
                      out=lambda text: None)


def test_texts_are_written_in_batches(tmp_path):
    run = make_run(tmp_path)
    area = security_items(run, SourceReader(project_of(run)))
    draft = build_report(area, decisions_for(area, {"S-0003": "NOISE"}), run)
    fake = FakeOllama()
    stats = write_texts(draft, writer_for(fake, "security", entries_per_request=2))
    assert stats.requests == 3  # 2 batches of entries, 1 summary
    assert stats.complete and draft.report["texts_missing"] == 0
    entries = draft.report["entries"]
    assert entries[0]["title"] == "Title of E-001" and entries[0]["recommendation"] == "Fix E-001."
    assert entries[3]["reason"] == "Reason for E-004." and entries[3]["description"] is None
    assert draft.report["texts"] == {"summary_text": "summary_text text.", "run_notes": "run_notes text."}
    body = fake.requests[0][1]
    assert body["format"]["required"] == ["entries"] and body["think"] is True
    assert "English" in body["messages"][0]["content"]
    assert body["options"]["seed"] == 42 and body["options"]["num_ctx"] == 16384


def test_missing_entries_are_asked_again_then_left_empty(tmp_path):
    run = make_run(tmp_path)
    area = security_items(run, SourceReader(project_of(run)))
    draft = build_report(area, decisions_for(area), run)
    fake = FakeOllama()
    fake.drop_ids = {"E-002"}  # the model never writes this entry
    stats = write_texts(draft, writer_for(fake, "security", entries_per_request=10))
    chats = [body for endpoint, body in fake.requests
             if endpoint == "/api/chat" and "entries" in body["format"]["properties"]]
    seeds = [body["options"]["seed"] for body in chats if '"E-002"' in body["messages"][1]["content"]]
    assert seeds == [42, 43]  # asked alone with another seed, `retries` times
    assert stats.invalid_answers == 2 and stats.texts_missing == 1 and not stats.complete
    assert draft.report["entries"][1]["title"] is None and draft.report["entries"][0]["title"]
    assert draft.report["texts_missing"] == 1


def test_invalid_answer_of_a_batch_is_split(tmp_path):
    run = make_run(tmp_path)
    area = security_items(run, SourceReader(project_of(run)))
    draft = build_report(area, decisions_for(area), run)
    fake = FakeOllama()
    original = fake._chat
    sizes = []

    def chat(body):
        user = body["messages"][1]["content"]
        count = user.count('"label":')
        sizes.append(count)
        if count > 2:  # e.g. the answer was cut off at num_predict
            return {"message": {"content": '{"entries": [{"id": "E-0'}, "done_reason": "length"}
        return original(body)

    fake._chat = chat
    stats = write_texts(draft, writer_for(fake, "security", entries_per_request=10))
    assert sizes[:3] == [4, 2, 2] and stats.split_requests == 1 and stats.complete
    assert stats.invalid_answers == 1


def test_summary_facts_are_shortened_to_fit_the_context(tmp_path):
    run = make_run(tmp_path)
    area = code_quality_items(run, SourceReader(project_of(run)))
    draft = build_report(area, decisions_for(area), run)
    draft.facts["real_groups"] = [{"id": f"G-{index:03d}", "rule": "x" * 50} for index in range(2000)]
    fake = FakeOllama()
    writer = writer_for(fake, "code_quality", num_ctx=4096, num_predict=1024)
    writer.write_summary(draft)
    user = fake.requests[-1][1]["messages"][1]["content"]
    facts = json.loads(user[user.index("{"):])
    assert len(user) + len(writer.system) <= (4096 - 1024) * 2.5
    assert facts["real_groups_left_out"] > 0
    assert draft.report["texts"]["change_types"] == ["indentation"]

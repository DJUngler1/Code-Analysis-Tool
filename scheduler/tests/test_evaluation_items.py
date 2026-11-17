"""Step 6: the configuration, the prompts and the items Python prepares for the models."""

import pytest
from evaluation_data import add_quality_findings, make_run

from scheduler.evaluation.config import ConfigError, LlmConfig, load_config
from scheduler.evaluation.items import (SourceReader, code_quality_items, failure_cause, is_security, path_scope,
                                        project_of, security_items, test_items)
from scheduler.evaluation.prompts import decision_prompt, render, report_prompt

from conftest import REPOSITORY


def write_config(tmp_path, text):
    path = tmp_path / "code-analysis.toml"
    path.write_text(text)
    return path


def test_repository_config_is_valid():
    config = load_config(REPOSITORY / "code-analysis.toml")
    assert config.decision.max_questions <= 64
    assert set(config.areas) <= {"security", "code_quality", "test_quality"}


def test_defaults_without_a_file():
    assert load_config(None) == LlmConfig()


def test_fine_grained_settings(tmp_path):
    config = load_config(write_config(tmp_path, """
[llm.input]
code_chars = 200
weak_line_rate = 0.8
[llm.decision]
timeout_seconds = 60
keep_alive = 120
[llm.report]
think = "medium"
min_p = 0.05
retries = 1
[llm.report.security]
think = "high"
num_predict = 8192
[llm.report.code_quality]
"""))
    assert config.input.code_chars == 200 and config.input.weak_line_rate == 0.8
    assert config.input.message_chars == 300  # not set: the default
    assert (config.decision.timeout_seconds, config.decision.keep_alive) == (60, "120")
    assert config.report.think == "medium" and config.report.options()["min_p"] == 0.05
    assert "min_p" not in LlmConfig().report.options()  # not set: not sent, the model's default applies
    assert config.report_overrides == {"security": {"think": "high", "num_predict": 8192}}  # empty tables: nothing
    security = config.report_for("security")
    assert (security.think, security.num_predict, security.min_p, security.retries) == ("high", 8192, 0.05, 1)
    assert config.report_for("code_quality") == config.report
    assert load_config(write_config(tmp_path, "[llm.report]\nthink = false\n")).report.think is False


@pytest.mark.parametrize("text, message", [
    ('[llm]\nareas = ["security", "style"]\n', "llm.areas"),
    ("[llm.decision]\nmax_questions = 65\n", "at most 64"),
    ("[llm.decision]\nreview_confidence = 1.5\n", "review_confidence"),
    ('[llm.report]\nthink = 2\n', 'true, false or a level'),
    ('[llm.report]\nthink = " "\n', 'true, false or a level'),
    ("[llm.report]\nnum_ctx = 512\n", "num_ctx"),
    ("[llm.report]\nnum_ctx = 4096\nnum_predict = 4096\n", "smaller than num_ctx"),
    ("[llm.report]\nmin_p = 1.5\n", "min_p"),
    ('[llm.decision]\nkeep_alive = true\n', "keep_alive"),
    ("[llm]\ntimeout_seconds = 60\n", "set per model now"),
    ("[llm.decision]\nmax_question = 4\n", "llm.decision.max_question is not a setting"),
    ("[llm.input]\ncode_chars = 0\n", "llm.input.code_chars"),
    ("[llm.input]\ngroup_samples = 2\ngroup_samples_with_code = 3\n", "at most group_samples"),
    ("[llm.input]\nweak_line_rate = 2\n", "weak_line_rate"),
    ("[llm.report.securty]\nthink = true\n", "not an area"),
    ('[llm.report.security]\nmodel = "x"\n', "all areas use the reasoning model"),
    ("[llm.report.security]\ntemprature = 0.2\n", "llm.report.security.temprature is not a setting"),
])
def test_invalid_values_name_the_key(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write_config(tmp_path, text))


@pytest.mark.parametrize("name", ["security_decision", "lint_decision", "test_failure_decision",
                                  "test_file_decision", "test_mutant_decision"])
def test_decision_prompts_parse(name):
    prompt = decision_prompt(name)
    assert "{{RUN_CONTEXT}}" in prompt.context and "{{ITEM}}" in prompt.question
    assert prompt.default in ("REAL", "PROJECT", "CRITICAL", "TEST_GAP")
    assert "DUPLICATE" not in prompt.labels


@pytest.mark.parametrize("name", ["security_report", "lint_report", "test_report"])
def test_report_prompts_parse_and_render(name):
    parts = report_prompt(name)
    assert "{{REPORT_LANGUAGE}}" in parts["System"]
    assert render(parts["System"], REPORT_LANGUAGE="German").count("German") >= 1
    assert render(parts["Entries"], ENTRIES="[]")
    assert render(parts["Summary"], FACTS="{}")


def test_project_and_scopes(tmp_path):
    run = make_run(tmp_path)
    assert project_of(run) == tmp_path / "project"
    generated = {"src/gen.py"}
    assert path_scope("src/gen.py", generated) == "generated"
    assert path_scope("tests/test_app.py", generated) == "test"
    assert path_scope("src/app.test.ts", generated) == "test"
    assert path_scope("node_modules/x/index.js", generated) == "vendored"
    assert path_scope(".github/workflows/ci.yml", generated) == "ci"
    assert path_scope("src/app.py", generated) == "main"


def test_security_ids_and_duplicates_across_tools(tmp_path):
    run = make_run(tmp_path)
    area = security_items(run, SourceReader(project_of(run)))
    assert [item["id"] for item in area.items] == ["S-0001", "S-0003", "D-0001", "D-0003"]
    # Same file and line with a shared CWE (OpenGrep and SonarQube); same package, version and CVE (OSV, npm audit).
    assert area.duplicates == {"S-0002": "S-0001", "D-0002": "D-0001"}
    sql = area.items[0]["data"]
    assert sql["also_reported_as"] == ["sonarqube: pythonsecurity:S3649"]
    assert sql["cwe"] == ["CWE-89"]
    assert "SELECT * FROM t" in sql["code"]  # the code around the line, from the project
    assert area.items[1]["data"]["scope"] == "test"
    lodash = area.items[2]["data"]
    assert lodash["advisories"] == ["CVE-2021-23337", "GHSA-35jh-r3h4-6jhm"]
    assert lodash["tools"] == ["npm audit", "osv-scanner"]


def test_security_without_steps_3_and_4(tmp_path):
    run = make_run(tmp_path, steps=(2,))
    area = security_items(run, SourceReader(None))
    assert not area.available and area.items == []


def test_lint_groups_by_tool_rule_and_scope(tmp_path):
    run = make_run(tmp_path)
    area = code_quality_items(run, SourceReader(project_of(run)))
    groups = {(item["data"]["tool"], item["data"]["rule"], item["data"]["scope"]): item for item in area.items}
    assert set(groups) == {("ruff check", "F401", "main"), ("ruff check", "E501", "test"),
                           ("biome", "lint/style/useTemplate", "main")}
    unused = groups[("ruff check", "F401", "main")]["data"]
    assert unused["count"] == 2 and unused["files"] == 2  # the exact duplicate is removed
    # Every location, sorted; not part of what the model sees (data).
    assert groups[("ruff check", "F401", "main")]["locations"] == [{"file": "src/app.py", "line": 1},
                                                                    {"file": "src/other.py", "line": 1}]
    assert "locations" not in unused
    assert area.facts["exact_duplicates"] == 1
    assert groups[("biome", "lint/style/useTemplate", "main")]["data"]["fixed_by_the_tool"] == 1
    assert [item["id"] for item in area.items] == ["L-001", "L-002", "L-003"]


@pytest.mark.parametrize("category, security", [("security", True), ("Security", True), (None, True), ("", True),
                                                ("correctness", True), ("reliability", True),
                                                ("maintainability", False), ("best-practice", False),
                                                ("portability", False), ("performance", False)])
def test_security_by_the_tools_category(category, security):
    assert is_security({"category": category}) is security


def test_static_findings_split_by_category(tmp_path):
    run = make_run(tmp_path)
    add_quality_findings(run)
    reader = SourceReader(project_of(run))
    security = security_items(run, reader)
    # The ids still count all findings of step 3; S-0004 to S-0006 are in code quality.
    assert [item["id"] for item in security.items] == ["S-0001", "S-0003", "S-0007", "S-0008", "S-0009", "D-0001",
                                                       "D-0003"]
    assert security.facts["static_findings"] == 9 and security.facts["static_in_code_quality"] == 3
    quality = code_quality_items(run, reader)
    groups = {(item["data"]["tool"], item["data"]["rule"], item["data"]["scope"]): item for item in quality.items}
    smell = groups[("sonarqube", "python:S1192", "main")]
    assert smell["kind"] == "static_group"
    assert smell["data"]["kind"] == "group of static analysis findings"
    assert smell["data"]["category"] == "maintainability" and smell["data"]["subcategory"] == ["code_smell"]
    assert smell["data"]["count"] == 2 and "fixed_by_the_tool" not in smell["data"]
    assert groups[("opengrep", "python.pass-body-fn", "test")]["data"]["category"] == "Best-Practice"
    assert "category" not in groups[("ruff check", "F401", "main")]["data"]
    assert (quality.facts["lint_findings"], quality.facts["static_findings"], quality.facts["input_findings"]) == (
        5, 3, 8)
    assert [tool["name"] for tool in quality.facts["tools"]] == ["ruff check", "biome", "opengrep", "sonarqube"]
    assert quality.context["static_analysis"] == ["opengrep 1.33.1 (ok)", "sonarqube 26.9 (ok)"]


def test_code_quality_with_step_3_only(tmp_path):
    run = make_run(tmp_path, steps=(3,))
    add_quality_findings(run)
    area = code_quality_items(run, SourceReader(project_of(run)))
    assert area.available and area.notes == ["Step 2 (linters and formatters) did not run."]
    assert {item["kind"] for item in area.items} == {"static_group"} and len(area.items) == 2


def test_code_quality_without_steps_2_and_3(tmp_path):
    area = code_quality_items(make_run(tmp_path, steps=(4, 5)), SourceReader(None))
    assert not area.available and area.items == []


def test_test_items(tmp_path):
    run = make_run(tmp_path)
    area = test_items(run, SourceReader(project_of(run)))
    kinds = [(item["id"], item["kind"]) for item in area.items]
    # Both failures have the same message apart from the quoted argument: one group.
    assert kinds == [("T-001", "test_failure"), ("F-0001", "weak_file"), ("F-0002", "weak_file"),
                     ("M-0001", "mutant")]
    assert area.items[0]["data"]["count"] == 2
    weak = {item["data"]["file"]: item["data"] for item in area.items if item["kind"] == "weak_file"}
    assert weak["src/app.py"]["line_rate"] == 0.2 and weak["src/app.py"]["mutants_not_detected"] == {"Survived": 1}
    assert weak["src/other.py"]["loaded_by_tests"] is False
    assert "def find" in weak["src/app.py"]["declarations"]


def test_failure_cause_ignores_numbers_paths_and_quotes():
    assert failure_cause("AssertionError: 'a' != 'b' at /x/y.py:12") == failure_cause("AssertionError: 'c' != 'd' "
                                                                                       "at /z.py:3")

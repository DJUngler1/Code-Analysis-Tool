"""The project's threat model: lookup, questions, draft by the reasoning model, confirmation, copy into a run."""

import json

import pytest
from evaluation_data import REPORT_MODEL, FakeClient, FakeOllama

from scheduler.config import Settings
from scheduler.evaluation.threat import run_threat_model
from scheduler.threat_model import (PROJECT_FILE, ThreatModel, ThreatModelError, collect_docs, copy_to_run, digest,
                                    ensure_threat_model, find, project_facts)

from conftest import REPOSITORY


def settings_for(tmp_path, config_file=REPOSITORY / "code-analysis.toml"):
    return Settings(projects_dir=None, host_home=None, ollama_base_url="http://ollama.invalid",
                    output_dir=tmp_path / "results", config_file=config_file)


def make_project(tmp_path):
    project = tmp_path / "game"
    (project / "docs").mkdir(parents=True)
    (project / "src").mkdir()
    (project / "tests").mkdir()
    (project / "README.md").write_text("# Game\nA game library that loads images.\n")
    (project / "SECURITY.md").write_text("Report issues to security@example.org.\n")
    (project / "docs" / "guide.md").write_text("Players can load their own maps.\n")
    return project


def confirmed(folder, text="# Threat model: game\n\n## Software and use\n\nA library.\n"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "threat-model.md").write_text(text)
    (folder / "threat-model.json").write_text(json.dumps({"status": "confirmed", "confirmed_at": "2026-11-18T08:00:00",
                                                          "sha256": digest(text)}))
    return text


class Script:
    """Answers the questions in order; records what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        answer = self.answers.pop(0)
        return answer() if callable(answer) else answer  # a callable acts first, e.g. edits the file


def ensure(tmp_path, project, *, interactive=True, ask=None, fake=None, settings=None, out=None):
    fake = fake or FakeOllama()
    lines = out if out is not None else []
    model = ensure_threat_model(project, settings or settings_for(tmp_path), folder=tmp_path / "results" / "game",
                                facts={"languages": {"primary": "Java"}}, interactive=interactive,
                                client=FakeClient(fake), ask=ask or Script(), out=lines.append)
    return model, fake, "\n".join(lines)


def test_the_project_file_comes_first(tmp_path):
    project = make_project(tmp_path)
    (project / PROJECT_FILE).parent.mkdir()
    (project / PROJECT_FILE).write_text("# Committed threat model\n\nA game library; players load images.\n")
    confirmed(tmp_path / "results" / "game")
    lookup = find(project, tmp_path / "results" / "game")
    assert lookup.state == "confirmed" and lookup.model.source == "project"


def test_a_confirmed_threat_model_is_used_without_questions(tmp_path):
    text = confirmed(tmp_path / "results" / "game")
    model, fake, out = ensure(tmp_path, make_project(tmp_path), interactive=False)
    assert model.text == text and model.source == "results" and model.confirmed_at == "2026-11-18T08:00:00"
    assert fake.requests == [] and "(confirmed 2026-11-18T08:00:00)" in out


def test_without_terminal_and_without_threat_model_the_tool_stops(tmp_path):
    with pytest.raises(ThreatModelError, match="without a terminal nobody can answer the questions"):
        ensure(tmp_path, make_project(tmp_path), interactive=False)


def test_a_threat_model_changed_after_its_confirmation_needs_a_new_confirmation(tmp_path):
    folder = tmp_path / "results" / "game"
    project = make_project(tmp_path)
    confirmed(folder)
    (folder / "threat-model.md").write_text("# Threat model: game\n\nEdited later.\n")
    with pytest.raises(ThreatModelError, match="was changed after it was confirmed"):
        ensure(tmp_path, project, interactive=False)
    model, fake, out = ensure(tmp_path, project, ask=Script("c"))
    assert "Edited later." in model.text and fake.requests == []
    assert json.loads((folder / "threat-model.json").read_text())["sha256"] == digest(model.text)


def test_questions_draft_and_confirmation(tmp_path):
    project = make_project(tmp_path)
    ask = Script("A game library", "for desktop and Android", "", "Image files of players", "", "", "c")
    model, fake, out = ensure(tmp_path, project, ask=ask)
    folder = tmp_path / "results" / "game"
    text = (folder / "threat-model.md").read_text()
    assert model.text == text and model.source == "results"
    assert "# Threat model: game" in text and "## Untrusted inputs (trust boundaries)" in text
    assert "software text." in text and "- indentation" in text  # the fake's answer, rendered
    info = json.loads((folder / "threat-model.json").read_text())
    assert info["status"] == "confirmed" and info["sha256"] == digest(text)
    assert info["answers"] == {"software": "A game library\nfor desktop and Android",
                               "untrusted_inputs": "Image files of players", "shipping": "(no answer)"}
    assert [entry["file"] for entry in info["documentation"]] == ["README.md", "SECURITY.md", "docs/guide.md"]
    (endpoint, body), = [request for request in fake.requests if request[0] == "/api/chat"]
    assert body["model"] == REPORT_MODEL and body["format"]["required"][0] == "software"
    user = body["messages"][1]["content"]
    assert "A game library\nfor desktop and Android" in user and "===== README.md =====" in user
    assert '"primary": "Java"' in user
    assert fake.loaded == []  # unloaded for the tools of steps 2 to 5
    assert (folder / "threat-model-request.json").is_file()
    assert "Threat model confirmed." in out


def test_regenerate_keeps_the_previous_draft_and_abort_leaves_it_unconfirmed(tmp_path):
    ask = Script("Library", "", "", "", "r", "Server", "", "", "", "a")
    with pytest.raises(ThreatModelError, match="aborted"):
        ensure(tmp_path, make_project(tmp_path), ask=ask)
    folder = tmp_path / "results" / "game"
    assert (folder / "threat-model.md.previous").is_file()
    info = json.loads((folder / "threat-model.json").read_text())
    assert info["status"] == "draft" and info["answers"]["software"] == "Server"


def test_an_unconfirmed_draft_is_offered_for_review(tmp_path):
    folder = tmp_path / "results" / "game"
    folder.mkdir(parents=True)
    (folder / "threat-model.md").write_text("# Threat model: game\n\nDraft.\n")
    (folder / "threat-model.json").write_text(json.dumps({"status": "draft", "answers": {}}))
    model, fake, out = ensure(tmp_path, make_project(tmp_path), ask=Script("x", "c"))
    assert "Draft." in model.text and fake.requests == []
    assert "not confirmed yet" in out and "Please answer c, r or a." in out


def test_end_of_input_aborts(tmp_path):
    with pytest.raises(ThreatModelError, match="no more input"):
        ensure(tmp_path, make_project(tmp_path), ask=Script())


def test_no_draft_from_the_model(tmp_path):
    fake = FakeOllama()
    fake.fail.add("/api/chat")
    with pytest.raises(ThreatModelError, match="wrote no valid draft"):
        ensure(tmp_path, make_project(tmp_path), ask=Script("", "", ""), fake=fake)
    assert not (tmp_path / "results" / "game" / "threat-model.md").exists()


def test_a_long_threat_model_is_reported(tmp_path):
    config = tmp_path / "code-analysis.toml"
    config.write_text("[llm.threat_model]\ncontext_chars = 200\n")
    confirmed(tmp_path / "results" / "game", "# Threat model\n\n" + "x" * 300)
    _, _, out = ensure(tmp_path, make_project(tmp_path), interactive=False,
                       settings=settings_for(tmp_path, config))
    assert "the security decisions see only the first 200" in out


def test_documentation_within_the_budget(tmp_path):
    text, read, left_out = collect_docs(make_project(tmp_path), 20)
    assert [entry["file"] for entry in read] == ["README.md"] and read[0]["used"] == 20
    assert "[... shortened]" in text and left_out == 2


def test_project_facts_with_the_scope_of_the_top_level_folders(tmp_path):
    project = make_project(tmp_path)
    (project / ".git").mkdir()
    facts = project_facts({"languages": {"primary": "Java"}, "package_managers": {}, "project": "x"}, project)
    assert facts == {"languages": {"primary": "Java"},
                     "top_level_folders": {"docs": "docs/examples", "src": "main", "tests": "test"}}


def test_the_copy_in_the_run_folder_is_what_step_6_sees(tmp_path):
    model = ThreatModel("<!-- hint for the user -->\n# Threat model\n\n" + "y" * 50, "results",
                        tmp_path / "threat-model.md", "2026-11-18", "abc")
    run_dir = tmp_path / "run"
    copy_to_run(run_dir, model)
    text, info = run_threat_model(run_dir, 30)
    assert text.startswith("# Threat model") and text.endswith(" …") and "hint" not in text
    assert info["source"] == "results" and info["shortened"] and info["sha256"] == "abc"
    assert run_threat_model(tmp_path / "other", 30) == (None, None)


# --- [llm.threat_model] create = "manual": the user writes the threat model from a template ---


def manual_settings(tmp_path):
    config = tmp_path / "manual.toml"
    config.write_text('[llm.threat_model]\ncreate = "manual"\n')
    return settings_for(tmp_path, config)


def test_manual_threat_model_from_the_template(tmp_path):
    path = tmp_path / "results" / "game" / "threat-model.md"

    def fill():  # the user writes into the template, then confirms
        path.write_text(path.read_text().replace("## Software and use\n", "## Software and use\n\nA game library.\n"))
        return "c"

    ask = Script("c", "r", fill)
    model, fake, out = ensure(tmp_path, make_project(tmp_path), ask=ask, settings=manual_settings(tmp_path))
    assert "A game library." in model.text and fake.requests == []  # no model involved
    assert "<!-- 1. What kind of software is this" in model.text  # the hints stay, the models do not see them
    assert ask.prompts[0] == "[c]onfirm, [a]bort: "
    assert "has no content yet (only headings)" in out and "Please answer c or a." in out
    info = json.loads((path.parent / "threat-model.json").read_text())
    assert info["status"] == "confirmed" and info["written_by"] == "user"


def test_an_empty_template_in_the_project_does_not_count(tmp_path):
    project = make_project(tmp_path)
    (project / PROJECT_FILE).parent.mkdir()
    (project / PROJECT_FILE).write_text("# Threat model: game\n\n## Software and use\n\n<!-- hint -->\n- \n")
    assert find(project, tmp_path / "results" / "game").state == "missing"


def test_the_create_setting_is_checked(tmp_path):
    from scheduler.evaluation.config import ConfigError, load_config

    config = tmp_path / "code-analysis.toml"
    config.write_text('[llm.threat_model]\ncreate = "auto"\n')
    with pytest.raises(ConfigError, match='create must be "generate"'):
        load_config(config)
    config.write_text('[llm.threat_model]\ncreate = "Manual"\n')
    assert load_config(config).threat_model.create == "manual"

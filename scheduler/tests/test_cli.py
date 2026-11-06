import pytest
from conftest import commit_all, requires_git
from test_ollama import unused_port

from scheduler.cli import main


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECTS_DIR", str(tmp_path))
    monkeypatch.setenv("OLLAMA_BASE_URL", f"http://127.0.0.1:{unused_port()}")
    monkeypatch.delenv("HOST_HOME", raising=False)


def feed_input(monkeypatch, *answers):
    remaining = list(answers)

    def fake_input(prompt=""):
        if not remaining:
            raise EOFError
        return remaining.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


@requires_git
def test_accepted_project_via_prompt(monkeypatch, make_project, capsys):
    root = make_project({"main.py": "print(1)\n", "requirements.txt": "requests\n"})
    commit_all(root)
    feed_input(monkeypatch, "/definitely/outside", str(root))

    assert main([]) == 0
    out = capsys.readouterr().out
    assert "Warning: Ollama is not reachable" in out
    assert "outside the projects folder" in out  # first answer was rejected, then asked again
    assert "Languages:        Python 100% (primary: Python)" in out
    assert "Package managers: Python: pip (requirements.txt)" in out
    assert "Project accepted." in out


def test_rejected_project_via_path_argument(make_project, capsys):
    root = make_project({"main.go": "package main\n"})
    assert main(["--path", str(root)]) == 2
    assert "Abort – language not supported: Go" in capsys.readouterr().out


def test_invalid_path_argument(capsys):
    assert main(["--path", "relative"]) == 1
    assert "not an absolute path" in capsys.readouterr().out


def test_empty_input_exits(monkeypatch, capsys):
    feed_input(monkeypatch, "")
    assert main([]) == 1
    assert "No project selected" in capsys.readouterr().out

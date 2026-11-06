from pathlib import Path

import pytest

from scheduler.config import Settings
from scheduler.paths import PathError, normalize, resolve_project_path


def settings(projects_dir: Path | None = None, host_home: Path | None = None) -> Settings:
    return Settings(projects_dir=projects_dir, host_home=host_home, ollama_base_url="http://localhost:11434")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  /Users/me/my app  ", "/Users/me/my app"),
        ("'/Users/me/my app'", "/Users/me/my app"),
        ('"/Users/me/my app"', "/Users/me/my app"),
        ("/Users/me/my\\ app\\ \\(v2\\) ", "/Users/me/my app (v2)"),  # dragged into macOS Terminal
        ("~/code/app", "/Users/me/code/app"),
        ("~", "/Users/me"),
    ],
)
def test_normalize(raw, expected):
    assert normalize(raw, host_home=Path("/Users/me")) == Path(expected)


def test_valid_project_inside_projects_dir(tmp_path):
    project = tmp_path / "projects" / "app"
    project.mkdir(parents=True)
    assert resolve_project_path(f"{project} ", settings(tmp_path / "projects")) == project.resolve()


def test_tilde_uses_host_home(tmp_path):
    (tmp_path / "app").mkdir()
    assert resolve_project_path("~/app", settings(host_home=tmp_path)) == (tmp_path / "app").resolve()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "No path entered"),
        ("relative/path", "not an absolute path"),
        ("{tmp}/missing", "does not exist"),
        ("{tmp}/file.txt", "is not a directory"),
    ],
)
def test_invalid_paths(tmp_path, raw, message):
    (tmp_path / "file.txt").write_text("")
    with pytest.raises(PathError, match=message):
        resolve_project_path(raw.format(tmp=tmp_path), settings())


def test_path_outside_projects_dir_is_rejected(tmp_path):
    (tmp_path / "projects").mkdir()
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(PathError, match="outside the projects folder"):
        resolve_project_path(str(tmp_path / "elsewhere"), settings(tmp_path / "projects"))


def test_settings_from_env(tmp_path):
    loaded = Settings.from_env(
        {"PROJECTS_DIR": str(tmp_path), "HOST_HOME": "/Users/me", "OLLAMA_BASE_URL": "http://ollama:11434/"}
    )
    assert loaded == Settings(tmp_path.resolve(), Path("/Users/me"), "http://ollama:11434")
    assert Settings.from_env({}).projects_dir is None

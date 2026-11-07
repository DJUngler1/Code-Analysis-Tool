"""Hiding of project configs, change snapshots, file selection and docker arguments."""

import subprocess
from pathlib import Path

import pytest
from conftest import commit_all, requires_git

from scheduler.linting import docker as docker_module
from scheduler.linting.docker import DockerError, DockerRunner, build_run_args
from scheduler.linting.files import source_files, write_file_list
from scheduler.linting.hiding import MANIFEST, find_files, hidden_files
from scheduler.linting.snapshots import Snapshotter

# --- hiding ------------------------------------------------------------------


def test_find_files_skips_git_and_node_modules(make_project):
    root = make_project({
        "biome.json": "{}",
        "pkg/biome.json": "{}",
        "node_modules/dep/biome.json": "{}",
        ".git/biome.json": "{}",
    })
    assert find_files(root, ["biome.json"]) == [Path("biome.json"), Path("pkg/biome.json")]


def test_hidden_files_are_moved_back(make_project, tmp_path):
    root = make_project({".editorconfig": "root = true\n", "sub/.editorconfig": "x\n", "a.cs": "class A {}"})
    store = tmp_path / "store"
    errors: list[str] = []
    configs = find_files(root, [".editorconfig"])
    with hidden_files(root, configs, store, errors) as moved:
        assert moved == configs
        assert not (root / ".editorconfig").exists()
        assert (store / "sub" / ".editorconfig").read_text() == "x\n"
        assert (store / MANIFEST).is_file()
    assert (root / ".editorconfig").read_text() == "root = true\n"
    assert (root / "sub" / ".editorconfig").read_text() == "x\n"
    assert not (store / MANIFEST).exists()
    assert errors == []


def test_hidden_files_are_moved_back_after_an_error(make_project, tmp_path):
    root = make_project({"biome.json": "{}"})
    with pytest.raises(RuntimeError), hidden_files(root, [Path("biome.json")], tmp_path / "store", []):
        raise RuntimeError("tool crashed")
    assert (root / "biome.json").read_text() == "{}"


def test_failed_restore_is_reported(make_project, tmp_path):
    root = make_project({"biome.json": "{}"})
    store = tmp_path / "store"
    errors: list[str] = []
    with hidden_files(root, [Path("biome.json")], store, errors):
        (store / "biome.json").unlink()  # e.g. deleted by someone meanwhile
    assert len(errors) == 1 and "could not move" in errors[0]
    assert (store / MANIFEST).is_file()  # kept, so the user can see what was moved


# --- snapshots ---------------------------------------------------------------


def objects_in(root: Path) -> int:
    return sum(1 for path in (root / ".git" / "objects").rglob("*") if path.is_file())


@requires_git
def test_snapshots_record_changes_per_tool_without_writing_to_git(make_project):
    root = make_project({"a.py": "a = 1\n", "b.py": "b = 2\n", "biome.json": "{}\n"})
    commit_all(root)
    objects_before = objects_in(root)
    snapshots = Snapshotter(root)
    try:
        base = snapshots.base()
        before = snapshots.snapshot()
        assert before == base

        (root / "a.py").write_text("a = 10\n")  # tool 1
        after_first = snapshots.snapshot()
        assert snapshots.changed_files(before, after_first) == ["a.py"]
        assert "+a = 10" in snapshots.patch(before, after_first)

        (root / "biome.json").unlink()  # hidden while a tool runs ...
        (root / "b.py").write_text("b = 20\n")  # ... tool 2
        hidden = snapshots.snapshot()
        (root / "biome.json").write_text("{}\n")  # ... and moved back
        final = snapshots.snapshot()
        assert snapshots.changed_files(base, final) == ["a.py", "b.py"]
        assert snapshots.changed_files(after_first, hidden) == ["b.py", "biome.json"]

        (root / "new.py").write_text("untracked\n")
        assert snapshots.snapshot() == final  # untracked files are not part of snapshots
    finally:
        snapshots.close()
    assert objects_in(root) == objects_before


@requires_git
def test_snapshots_of_a_subfolder_project(make_project):
    repo = make_project({"service/app.py": "x = 1\n", "other/keep.py": "y = 1\n"})
    commit_all(repo)
    project = repo / "service"
    snapshots = Snapshotter(project)
    try:
        base = snapshots.base()
        (project / "app.py").write_text("x = 2\n")
        (repo / "other" / "keep.py").write_text("y = 2\n")  # outside the project
        assert snapshots.changed_files(base, snapshots.snapshot()) == ["app.py"]
    finally:
        snapshots.close()


# --- file selection ------------------------------------------------------------


@requires_git
def test_source_files_respect_gitignore_and_skipped_folders(make_project):
    root = make_project({
        "src/A.java": "class A {}",
        "vendor/lib/V.java": "class V {}",
        "target/generated/G.java": "class G {}",
        "ignored/I.java": "class I {}",
        ".gitignore": "ignored/\n",
        "README.md": "x",
    })
    commit_all(root)
    (root / "src" / "New.java").write_text("class New {}")  # untracked, not ignored
    assert source_files(root, [".java"]) == [Path("src/A.java"), Path("src/New.java")]


def test_write_file_list(tmp_path):
    target = write_file_list([Path("/p/a b.c"), Path("/p/x.c")], tmp_path / "list" / "files.txt")
    assert target.read_bytes() == b"/p/a b.c\0/p/x.c\0"


# --- docker --------------------------------------------------------------------


def test_build_run_args():
    args = build_run_args(
        "code-analysis/lint-python:latest",
        ["ruff", "check", "."],
        name="code-analysis-test",
        workdir=Path("/projects/app"),
        mounts=[Path("/projects/app"), Path("/projects/app/sub"), Path("/results/run")],
        env={"A": "1"},
        user="1000:1000",
        network="code-analysis-net",
    )
    assert args[:3] == ["docker", "run", "--rm"]
    assert ["--user", "1000:1000"] == args[args.index("--user"):args.index("--user") + 2]
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    # Same path inside and outside; a mount inside another one is dropped.
    assert mounts == [
        "type=bind,source=/projects/app,target=/projects/app",
        "type=bind,source=/results/run,target=/results/run",
    ]
    assert "A=1" in args and "HOME=/tmp/home" in args
    assert args[-4:] == ["code-analysis/lint-python:latest", "ruff", "check", "."]


def test_permission_error_names_the_socket_group(monkeypatch, tmp_path):
    socket = tmp_path / "docker.sock"
    socket.write_text("")
    monkeypatch.setattr(docker_module, "DOCKER_SOCKET", socket)
    denied = subprocess.CompletedProcess([], 1, "", "permission denied while trying to connect to the Docker daemon")
    monkeypatch.setattr(docker_module, "_docker", lambda *args: denied)
    with pytest.raises(DockerError, match=f"DOCKER_SOCKET_GID={socket.stat().st_gid}"):
        DockerRunner("net").check_available()


def test_missing_image_explains_how_to_build(monkeypatch):
    missing = subprocess.CompletedProcess([], 1, "", "No such image")
    monkeypatch.setattr(docker_module, "_docker", lambda *args: missing)
    with pytest.raises(DockerError, match="--profile tools build"):
        DockerRunner("net").ensure_image("code-analysis/lint-rust:latest")

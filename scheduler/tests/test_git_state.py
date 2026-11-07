from conftest import commit_all, requires_git

from scheduler.acceptance.git_state import check_git_state

pytestmark = requires_git


def test_not_a_repository(make_project):
    state = check_git_state(make_project({"app.py": ""}))
    assert not state.is_repo
    assert not state.clean


def test_clean_repository(make_project):
    root = make_project({"app.py": "print(1)\n"})
    commit_all(root)
    state = check_git_state(root)
    assert state.is_repo
    assert state.clean


def test_modified_file_makes_it_dirty(make_project):
    root = make_project({"app.py": "print(1)\n"})
    commit_all(root)
    (root / "app.py").write_text("print(2)\n")
    state = check_git_state(root)
    assert state.changes == (" M app.py",)
    assert not state.clean


def test_untracked_file_makes_it_dirty(make_project):
    root = make_project({"app.py": "print(1)\n"})
    commit_all(root)
    (root / "notes.txt").write_text("todo\n")
    assert check_git_state(root).changes == ("?? notes.txt",)


def test_only_the_project_subfolder_is_checked(make_project):
    root = make_project({"service/app.py": "print(1)\n", "other/lib.py": "print(1)\n"})
    commit_all(root)
    (root / "other" / "lib.py").write_text("print(2)\n")
    assert check_git_state(root / "service").clean
    assert not check_git_state(root).clean


def test_stale_index_pattern():
    from scheduler.acceptance.git_state import looks_like_stale_index

    assert looks_like_stale_index(["D  a.py", "D  b.py", "?? a.py", "?? b.py"])
    assert not looks_like_stale_index(["D  a.py", "?? a.py", " M b.py"])
    assert not looks_like_stale_index(["?? a.py"])
    assert not looks_like_stale_index([])


@requires_git
def test_stale_index_is_retried(make_project, monkeypatch):
    import subprocess as sp

    from scheduler.acceptance import git_state

    root = make_project({"a.py": "x = 1\n"})
    commit_all(root)
    stale = sp.CompletedProcess([], 0, "D  a.py\n?? a.py\n", "")
    real = git_state._git
    answers = [stale]

    def fake_git(project, *args):
        if args[0] == "status" and answers:
            return answers.pop()
        return real(project, *args)

    monkeypatch.setattr(git_state, "_git", fake_git)
    monkeypatch.setattr(git_state, "STALE_INDEX_WAIT_SECONDS", 0)
    assert check_git_state(root).clean

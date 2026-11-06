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

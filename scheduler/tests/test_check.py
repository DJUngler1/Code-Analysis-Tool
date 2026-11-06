from conftest import commit_all, requires_git

from scheduler.acceptance import Outcome, run_acceptance


@requires_git
def test_accepted_project(make_project):
    root = make_project({"app/main.py": "x" * 80, "pyproject.toml": "[project]\n", "web/app.ts": "x" * 20})
    commit_all(root)
    result = run_acceptance(root)
    assert result.outcome is Outcome.ACCEPTED
    assert result.accepted
    assert result.languages.primary == "Python"
    assert [hit.manager for hit in result.package_managers["Python"]] == ["pip"]
    assert result.git is not None and result.git.clean


def test_unsupported_language(make_project):
    result = run_acceptance(make_project({"main.go": "package main\n", "go.mod": "module x\n"}))
    assert result.outcome is Outcome.LANGUAGE_NOT_SUPPORTED
    assert result.outcome == 2
    assert "language not supported: Go" in result.message


def test_no_source_code(make_project):
    result = run_acceptance(make_project({"README.md": "# docs\n"}))
    assert result.outcome is Outcome.LANGUAGE_NOT_SUPPORTED
    assert "no source code found" in result.message


def test_cpp_without_conan_or_vcpkg_is_rejected(make_project):
    result = run_acceptance(make_project({"src/main.cpp": "int main() {}\n", "CMakeLists.txt": ""}))
    assert result.outcome is Outcome.NO_PACKAGE_MANAGER
    assert result.outcome == 3
    assert "no package manager found for C++" in result.message
    assert "vcpkg.json" in result.message


@requires_git
def test_cpp_with_vcpkg_is_accepted(make_project):
    root = make_project({"src/main.cpp": "int main() {}\n", "CMakeLists.txt": "", "vcpkg.json": "{}"})
    commit_all(root)
    assert run_acceptance(root).outcome is Outcome.ACCEPTED


@requires_git
def test_not_a_git_repository(make_project):
    result = run_acceptance(make_project({"main.rs": "fn main() {}\n", "Cargo.toml": ""}))
    assert result.outcome is Outcome.GIT_NOT_CLEAN
    assert result.outcome == 4
    assert "not a git repository" in result.message


@requires_git
def test_uncommitted_changes_are_listed(make_project):
    root = make_project({"main.rs": "fn main() {}\n", "Cargo.toml": ""})
    commit_all(root)
    for i in range(12):
        (root / f"new{i:02}.rs").write_text("")
    result = run_acceptance(root)
    assert result.outcome is Outcome.GIT_NOT_CLEAN
    assert "?? new00.rs" in result.message
    assert "... and 2 more" in result.message

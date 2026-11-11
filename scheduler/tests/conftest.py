from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

requires_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

REPOSITORY = Path(__file__).resolve().parents[2]
DEFAULT_RULESETS = ("opengrep-rules", "0xdea-semgrep-rules")


def opengrep_config(folder: Path, rulesets: tuple[str, ...] = DEFAULT_RULESETS) -> Path:
    """A copy of config/opengrep.toml with the given active rule sets.

    Keeps the tests independent of the rule sets the user has activated.
    """
    text = (REPOSITORY / "config" / "opengrep.toml").read_text()
    active = ", ".join(f'"{name}"' for name in rulesets)
    text, count = re.subn(r"(?m)^rulesets = \[.*\]$", f"rulesets = [{active}]", text)
    assert count == 1, "config/opengrep.toml: no `rulesets = [...]` line"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "opengrep.toml").write_text(text)
    return folder

MakeProject = Callable[[Mapping[str, str]], Path]


@pytest.fixture
def make_project(tmp_path: Path) -> MakeProject:
    """Create a project folder from {relative path: content}."""
    counter = 0

    def _make(files: Mapping[str, str]) -> Path:
        nonlocal counter
        counter += 1
        root = tmp_path / f"project{counter}"
        root.mkdir()
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return root.resolve()

    return _make


def git(root: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            # Isolate from the developer's git config (signing, hooks, identity).
            "-c", "user.name=Test",
            "-c", "user.email=test@example.com",
            "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null",
            # No background gc/maintenance: it would repack objects mid-test.
            "-c", "gc.auto=0",
            "-c", "maintenance.auto=false",
            "-C", str(root),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def commit_all(root: Path) -> None:
    """Turn a folder into a git repo with everything committed."""
    git(root, "init", "--quiet")
    git(root, "add", "--all")
    git(root, "commit", "--quiet", "--message", "initial")

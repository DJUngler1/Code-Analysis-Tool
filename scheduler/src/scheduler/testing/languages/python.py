"""Python: install the project into .venv with its package manager, then run pytest.

pytest also runs unittest tests. If the project does not install pytest
itself, the pinned version below is added to its environment.
"""

from __future__ import annotations

import configparser
import re
import tomllib
from pathlib import Path

from scheduler.linting.model import Status
from scheduler.testing.context import Job, TestContext, judge, new_run, note_prepare
from scheduler.testing.model import SuiteRun
from scheduler.testing.reports import ReportError, parse_junit

IMAGE = "code-analysis/test-python:latest"
PYTEST_VERSION = "9.2.0"
VENV_PYTHON = ".venv/bin/python"
# Extras and dependency groups that usually hold the test dependencies (pip projects).
TEST_GROUPS = re.compile(r"^(test|tests|testing|dev|develop|development)$", re.IGNORECASE)
# pytest's exit codes: 0 all passed, 1 tests failed, 5 no tests collected.
COMPLETED = (0, 1, 5)
_PYTEST_HEADER = re.compile(r"platform \S+ -- Python ([^\s,]+), pytest-([^\s,]+)")


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "pytest")
    if job.suite.manager == "Conda":
        run.status = Status.SKIPPED
        run.notes.append("Conda environments are not supported")
        return run
    cache = ctx.cache_dir / "python"
    env = {
        "UV_CACHE_DIR": str(cache / "uv"),
        "UV_PYTHON_INSTALL_DIR": str(cache / "uv-python"),  # Python versions uv downloads for the project
        "UV_LINK_MODE": "copy",
        "POETRY_CACHE_DIR": str(cache / "poetry"),
        "PDM_CACHE_DIR": str(cache / "pdm"),
        "PIP_CACHE_DIR": str(cache / "pip"),
        "PIPENV_CACHE_DIR": str(cache / "pipenv"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "POETRY_NO_INTERACTION": "1",
        "PIP_NO_INPUT": "1",
        # The copy is a git repository without commits: setuptools-scm cannot derive a version from it.
        "SETUPTOOLS_SCM_PRETEND_VERSION": "0.0.0",
    }
    prepared = ctx.run(run, IMAGE, ["sh", "-c", install_script(job)], workdir=job.folder, log_dir=job.out_dir,
                       label="install", online=True, env=env)
    note_prepare(run, prepared, f"installing the project with {job.suite.manager}")

    report = job.out_dir / "junit.xml"
    result = ctx.run(run, IMAGE, [VENV_PYTHON, "-m", "pytest", f"--junitxml={report}",
                                  "--continue-on-collection-errors", "-p", "no:cacheprovider"],
                     workdir=job.folder, log_dir=job.out_dir, label="pytest", online=False,
                     env={**env, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"})
    if header := _PYTEST_HEADER.search(result.stdout):
        run.version = f"pytest {header.group(2)}, Python {header.group(1)}"
    tests = []
    if report.is_file():
        try:
            tests = parse_junit(report)
        except ReportError as exc:
            run.notes.append(f"the JUnit report could not be read: {exc}")
    if result.exit_code == 5:
        run.notes.append("pytest collected no tests")
    return judge(ctx, run, result, tests, completed=result.exit_code in COMPLETED and report.is_file(),
                 what="pytest")


def install_script(job: Job) -> str:
    """Shell script (online) that creates .venv with the project and its test dependencies, plus pytest."""
    folder, manager = job.folder, job.suite.manager
    if manager == "uv":
        install = "uv sync --frozen --all-extras --all-groups"
    elif manager == "Poetry":
        install = "poetry install --all-extras --all-groups"
    elif manager == "PDM":
        install = "pdm install -G :all"
    elif manager == "Pipenv":
        install = "pipenv sync --dev" if (folder / "Pipfile.lock").is_file() else "pipenv install --dev"
    else:
        install = "uv venv --quiet .venv && uv pip install --python .venv/bin/python " + " ".join(pip_arguments(folder))
    ensure_pytest = (f'{VENV_PYTHON} -c "import pytest" 2>/dev/null '
                     f"|| uv pip install --python {VENV_PYTHON} pytest=={PYTEST_VERSION}")
    return f"set -e\n{install}\n{ensure_pytest}\n"


def pip_arguments(folder: Path) -> list[str]:
    """`uv pip install` arguments for a pip project: the project (editable) with its test extras, the
    test dependency groups and the requirements files."""
    arguments: list[str] = []
    extras: list[str] = []
    pyproject = folder / "pyproject.toml"
    data: dict = {}
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            data = {}
    project = data.get("project") if isinstance(data.get("project"), dict) else None
    optional = project.get("optional-dependencies") if project else None
    if isinstance(optional, dict):
        extras += [name for name in optional if TEST_GROUPS.match(name)]
    extras += _setup_cfg_extras(folder / "setup.cfg")
    if project is not None or (folder / "setup.py").is_file() or (folder / "setup.cfg").is_file():
        arguments += ["-e", f"'.[{','.join(extras)}]'" if extras else "."]
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for name in groups:
            if TEST_GROUPS.match(name):
                arguments += ["--group", name]
    for requirements in requirement_files(folder):
        arguments += ["-r", f"'{requirements.relative_to(folder).as_posix()}'"]
    return arguments or ["pytest"]


def requirement_files(folder: Path) -> list[Path]:
    """requirements.txt, and the requirements files for tests or development (e.g. requirements-dev.txt,
    requirements/test.txt); others such as requirements-docs.txt are left out."""
    candidates = sorted(folder.glob("requirements*.txt")) + sorted((folder / "requirements").glob("*.txt"))
    return [path for path in candidates if path.name == "requirements.txt"
            or path.stem in ("base", "common") and path.parent.name == "requirements"
            or re.search(r"test|dev", path.stem, re.IGNORECASE)]


def _setup_cfg_extras(path: Path) -> list[str]:
    if not path.is_file():
        return []
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(path.read_text(encoding="utf-8", errors="replace"))
    except configparser.Error:
        return []
    if not parser.has_section("options.extras_require"):
        return []
    return [name for name in parser["options.extras_require"] if TEST_GROUPS.match(name)]

"""Python: install the project into .venv with its package manager, then run pytest.

pytest also runs unittest tests. If the project does not install pytest
itself, the pinned version below is added to its environment.

Mutation testing: mutmut 3, added to the environment (online). Its settings
([tool.mutmut] in pyproject.toml of the copy, which mutmut reads before
setup.cfg) name the selected files and deselect the tests that failed in 5.2.
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
from pathlib import Path

from scheduler.linting.model import Status
from scheduler.testing.context import (
    Job,
    TestContext,
    finish_coverage,
    judge,
    new_coverage,
    new_run,
    note_prepare,
    read_report,
)
from scheduler.testing.coverage_reports import parse_cobertura
from scheduler.testing.model import CoverageRun, Mutant, MutationRun, SuiteRun
from scheduler.testing.mutation import finish_mutation, new_mutation
from scheduler.testing.reports import ReportError, parse_junit

IMAGE = "code-analysis/test-python:latest"
PYTEST_VERSION = "9.2.0"
COVERAGE_VERSION = "7.17.3"
MUTMUT_VERSION = "3.8.1"
# mutmut's states (its status_by_exit_code) -> schema states.
MUTMUT_STATES = {"killed": "Killed", "survived": "Survived", "no tests": "NoCoverage", "timeout": "Timeout",
                 "caught by type check": "CompileError", "suspicious": "RuntimeError", "segfault": "RuntimeError",
                 "skipped": "Ignored", "not checked": "Pending", "check was interrupted by user": "Pending"}
_MUTMUT_RESULT = re.compile(r"^\s*(\S+): (.+?)\s*$")
_MUTMUT_TABLE = re.compile(r"^\[tool\.mutmut\]\s*$.*?(?=^\[|\Z)", re.MULTILINE | re.DOTALL)
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
    env = _env(ctx)
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


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    """coverage.py around pytest, with branch coverage and without the project's coverage settings.

    The project's own coverage.py version is used if its environment has one
    (from 5.2); otherwise the pinned version is added (online).
    """
    coverage = new_coverage(ctx, job, "coverage.py")
    env = _env(ctx)
    install = (f'{VENV_PYTHON} -c "import coverage" 2>/dev/null '
               f"|| uv pip install --python {VENV_PYTHON} coverage=={COVERAGE_VERSION}")
    prepared = ctx.run(coverage, IMAGE, ["sh", "-c", install],
                       workdir=job.folder, log_dir=job.out_dir, label="install", online=True, env=env)
    note_prepare(coverage, prepared, "installing coverage.py")
    # Own settings instead of the project's (.coveragerc, [tool.coverage]): the same measurement for every project.
    settings = job.out_dir / "coveragerc"
    settings.write_text(f"[run]\nbranch = True\ndata_file = {job.out_dir / '.coverage'}\n", encoding="utf-8")
    offline = {**env, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    result = ctx.run(coverage, IMAGE, [VENV_PYTHON, "-m", "coverage", "run", f"--rcfile={settings}", "-m", "pytest",
                                       "-p", "no:cacheprovider", "-p", "no:cov", "--continue-on-collection-errors"],
                     workdir=job.folder, log_dir=job.out_dir, label="coverage-run", online=False, env=offline)
    report = job.out_dir / "coverage.xml"
    ctx.run(coverage, IMAGE, [VENV_PYTHON, "-m", "coverage", "xml", f"--rcfile={settings}", "-i", "-o", str(report)],
            workdir=job.folder, log_dir=job.out_dir, label="coverage-xml", online=False, env=offline)
    files = read_report(coverage, report, lambda path: parse_cobertura(path, job.workspace))
    if report.is_file() and (match := re.search(r'<coverage version="([^"]+)"', report.read_text(errors="replace"))):
        coverage.version = f"coverage.py {match.group(1)}"
    return finish_coverage(ctx, coverage, job, files, result, what="coverage.py")


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    mutation = new_mutation(ctx, job, "mutmut")
    mutation.version = f"mutmut {MUTMUT_VERSION}"
    env = _env(ctx)
    mutmut = ".venv/bin/mutmut"
    install = f"test -x {mutmut} || uv pip install --python {VENV_PYTHON} mutmut=={MUTMUT_VERSION}"
    prepared = ctx.run(mutation, IMAGE, ["sh", "-c", install], workdir=job.folder, log_dir=job.out_dir,
                       label="install", online=True, env=env)
    note_prepare(mutation, prepared, f"installing mutmut {MUTMUT_VERSION}")
    root = job.suite.root
    sources = [file if root == "." else file.removeprefix(f"{root}/") for file in files]
    deselect = [node for test in failing if (node := pytest_node(job.folder, test))]
    mutation.excluded_tests = deselect
    write_mutmut_settings(job.folder / "pyproject.toml", sources, deselect)
    offline = {**env, "UV_OFFLINE": "1", "PIP_NO_INDEX": "1"}
    result = ctx.run(mutation, IMAGE, [mutmut, "run"], workdir=job.folder, log_dir=job.out_dir, label="mutmut-run",
                     online=False, env=offline, timeout="mutation")
    # The results of every mutant, and the diffs of those no test detected (they name the changed line).
    script = (f"{mutmut} results --all true; echo '=== code-analysis diffs'; "
              f"for name in $({mutmut} results | sed -n 's/^ *\\([^:]*\\): .*/\\1/p'); "
              f'do echo "=== $name"; {mutmut} show "$name"; done')
    listed = ctx.run(mutation, IMAGE, ["sh", "-c", script], workdir=job.folder, log_dir=job.out_dir,
                     label="mutmut-results", online=False, env=offline)
    mutants = parse_mutmut(listed.stdout, job.folder, job.workspace) if listed.exit_code == 0 else None
    if mutants == [] and result.exit_code not in (0, None):
        mutants = None
    return finish_mutation(ctx, mutation, job, mutants, result, what="mutmut")


def write_mutmut_settings(pyproject: Path, sources: list[str], deselect: list[str]) -> None:
    """[tool.mutmut] in the copy's pyproject.toml, replacing the project's own."""
    text = pyproject.read_text(encoding="utf-8", errors="replace") if pyproject.is_file() else ""
    text = _MUTMUT_TABLE.sub("", text).rstrip()
    selection = [argument for node in deselect for argument in ("--deselect", node)]
    table = ("\n\n[tool.mutmut]\n"
             f"source_paths = {json.dumps(sources)}\n"
             f"pytest_add_cli_args_test_selection = {json.dumps(selection)}\n")
    pyproject.write_text(text + table, encoding="utf-8")


def pytest_node(folder: Path, test_id: str) -> str | None:
    """JUnit id ("tests.test_calc.TestX.test_y[1.5]") -> pytest node id ("tests/test_calc.py::TestX::test_y[1.5]")."""
    head, bracket, tail = test_id.partition("[")
    parts = head.split(".")
    for end in range(len(parts) - 1, 0, -1):
        module = "/".join(parts[:end]) + ".py"
        if (folder / module).is_file():
            return "::".join([module, *parts[end:]]) + (bracket + tail if bracket else "")
    return None


def parse_mutmut(output: str, folder: Path, workspace: Path) -> list[Mutant]:
    """`mutmut results --all true` (state per mutant) and `mutmut show` (diff) of the undetected ones."""
    listing, _, diffs = output.partition("=== code-analysis diffs")
    lines_by_name: dict[str, tuple[str, int | None, str, str]] = {}
    for block in diffs.split("\n=== ")[1:]:
        name, _, diff = block.partition("\n")
        lines_by_name[name.strip()] = _diff_location(diff, folder, workspace)
    mutants = []
    for line in listing.splitlines():
        match = _MUTMUT_RESULT.match(line)
        if not match or "__mutmut_" not in match.group(1):
            continue
        name, state = match.groups()
        file, number, original, replacement = lines_by_name.get(name) or (_module_file(name, folder, workspace),
                                                                           None, "", "")
        if file is None:
            continue
        mutants.append(Mutant(file, number, "mutmut", replacement or name, MUTMUT_STATES.get(state, "RuntimeError"),
                              original))
    return mutants


def _diff_location(diff: str, folder: Path, workspace: Path) -> tuple[str | None, int | None, str, str]:
    """File, line in the original file, original and new code of a `mutmut show` diff.

    The hunk covers only the mutated function: its first line is looked up in
    the original file to get the absolute line number.
    """
    file = None
    body: list[str] = []
    for line in diff.splitlines():
        if line.startswith("--- "):
            file = line[4:].strip()
        elif line.startswith(("+++ ", "@@", "# ")):
            continue
        elif file is not None and line[:1] in (" ", "-", "+"):
            body.append(line)
    if file is None:
        return None, None, "", ""
    relative = (folder / file).resolve().relative_to(workspace.resolve()).as_posix() \
        if (folder / file).resolve().is_relative_to(workspace.resolve()) else None
    original = [line[1:] for line in body if line.startswith("-")]
    replacement = [line[1:] for line in body if line.startswith("+")]
    number = None
    source = (folder / file).read_text(encoding="utf-8", errors="replace").splitlines() \
        if (folder / file).is_file() else []
    first = next((line[1:] for line in body if line[:1] in (" ", "-")), None)
    offset = next((index for index, line in enumerate(line for line in body if line[:1] in (" ", "-"))
                   if line.startswith("-")), None)
    starts = [index for index, text in enumerate(source) if first is not None and text == first]
    if len(starts) == 1 and offset is not None:
        number = starts[0] + offset + 1
    return relative, number, " | ".join(text.strip() for text in original), " | ".join(text.strip()
                                                                                       for text in replacement)


def _module_file(name: str, folder: Path, workspace: Path) -> str | None:
    """"calc.sub.x_f__mutmut_1" -> the module's file (calc/sub.py or calc/sub/__init__.py)."""
    parts = name.split(".")[:-1]
    while parts:
        for candidate in ("/".join(parts) + ".py", "/".join(parts) + "/__init__.py"):
            if (folder / candidate).is_file():
                path = (folder / candidate).resolve()
                return path.relative_to(workspace.resolve()).as_posix()
        parts = parts[:-1]
    return None


def _env(ctx: TestContext) -> dict[str, str]:
    cache = ctx.cache_dir / "python"
    return {
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

"""JavaScript/TypeScript: install with the project's package manager, then run its test framework.

Online phase: install without lifecycle scripts (`--ignore-scripts`), so no
code of the project or its packages runs with network. Offline: the
packages' install scripts (rebuild), then the tests.

The framework runs with the arguments of the project's test script when the
script is a single call of it (e.g. "vitest run --dir src"), otherwise with
its own configuration; a reporter for a machine-readable report is added.
Frameworks that need a browser (Playwright, Cypress, Karma) are not run.

Coverage: the frameworks' own where they have one (Jest's built-in coverage;
Vitest with @vitest/coverage-v8 in the version of the project's Vitest, added
to the copy online), otherwise c8 (V8 coverage) around the test command
(Mocha, node:test, a test script without a known framework). Bun: not measured.

Mutation testing: StrykerJS, added to the copy (online) with the runner of the
framework (Jest, Vitest, Mocha; otherwise Stryker's command runner with the
test command). Stryker needs all tests to pass without mutants and cannot
leave single tests out: a suite with failing tests is skipped.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

from scheduler.linting.model import CommandResult, Status
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
from scheduler.testing.detect import BROWSER_FRAMEWORKS
from scheduler.testing.model import CoverageRun, MutationRun, SuiteRun, TestCase
from scheduler.testing.mutation import finish_mutation, new_mutation, parse_stryker, skip
from scheduler.testing.reports import ReportError, parse_jest, parse_junit

IMAGE = "code-analysis/test-node:latest"
LOCKFILES = {"npm": ("package-lock.json", "npm-shrinkwrap.json"), "Yarn": ("yarn.lock",),
             "pnpm": ("pnpm-lock.yaml",), "Bun": ("bun.lock", "bun.lockb")}
STRYKER_VERSION = "10.1.1"
STRYKER_RUNNERS = {"Jest": "jest", "Vitest": "vitest", "Mocha": "mocha"}  # others: the command runner
# Framework -> its command; in this order when a suite has several.
RUNNABLE = {"Vitest": "vitest", "Jest": "jest", "Mocha": "mocha", "node:test": "node", "Bun test": "bun"}
_SHELL_OPERATORS = ("&&", "||", ";", "|", ">", "<", "$(", "`")
# Runs "$@" with the package binaries of the suite and of the install root ($1) on the PATH, as npm does.
WITH_BINARIES = 'PATH="$1/node_modules/.bin:$PWD/node_modules/.bin:$PATH"; shift; exec "$@"'


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    suite = job.suite
    framework = next((name for name in RUNNABLE if name in suite.frameworks), None)
    run = new_run(ctx, job, framework or f"{suite.manager} test")
    versions = ctx.runner.image_versions(IMAGE)
    run.version = versions.get("node")
    package = _package(job.folder / "package.json")
    script = _test_script(package)
    if framework is None and (script is None or set(suite.frameworks) & set(BROWSER_FRAMEWORKS)):
        run.status = Status.SKIPPED
        browsers = [name for name in suite.frameworks if name in BROWSER_FRAMEWORKS]
        run.notes.append(f"{', '.join(browsers)} need(s) a browser, which the test image does not contain"
                         if browsers else "no test framework the tool can run")
        return run

    root = install_root(job.folder, job.workspace, suite.manager)
    env = _env(ctx)
    installed = ctx.run(run, IMAGE, ["sh", "-c", install_command(suite.manager, root)], workdir=root,
                        log_dir=job.out_dir, label="install", online=True, env=env)
    note_prepare(run, installed, f"installing the packages with {suite.manager}")
    rebuilt = ctx.run(run, IMAGE, ["sh", "-c", rebuild_command(suite.manager)], workdir=root,
                      log_dir=job.out_dir, label="rebuild", online=False, env=env)
    note_prepare(run, rebuilt, "running the install scripts of the packages")

    if framework is None:  # a test script without a known framework: only its exit code
        result = ctx.run(run, IMAGE, ["sh", "-c", f"{_runner(suite.manager)} test"], workdir=job.folder,
                         log_dir=job.out_dir, label="test", online=False, env=env)
        run.notes.append("the test script uses no known test framework: only its exit code is recorded")
        judge(ctx, run, result, [], completed=False, what="the test script")
        if result.exit_code is not None and not result.timed_out:
            run.status = Status.PARTIAL
        return run

    report = job.out_dir / ("jest.json" if framework == "Jest" else "junit.xml")
    command = test_command(framework, script, report)
    result = ctx.run(run, IMAGE, _with_binaries(root, command), workdir=job.folder, log_dir=job.out_dir,
                     label="test", online=False, env=env)
    tests: list[TestCase] = []
    if report.is_file():
        try:
            tests = parse_jest(report, job.folder) if framework == "Jest" else parse_junit(report)
        except ReportError as exc:
            run.notes.append(f"the test report could not be read: {exc}")
    others = [name for name in suite.frameworks if name != framework and name in RUNNABLE]
    if others:
        run.notes.append(f"only {framework} was run, not {', '.join(others)}")
    return judge(ctx, run, result, tests, completed=report.is_file() and result.exit_code is not None,
                 what=framework)


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    """Runs after run_suite in the same copy: the packages are installed."""
    suite = job.suite
    framework = next((name for name in RUNNABLE if name in suite.frameworks), None)
    script = _test_script(_package(job.folder / "package.json"))
    root = install_root(job.folder, job.workspace, suite.manager)
    env = _env(ctx)
    report = job.out_dir / "cobertura-coverage.xml"
    if framework == "Bun test":
        coverage = new_coverage(ctx, job, "Bun")
        coverage.status = Status.SKIPPED
        coverage.notes.append("Bun's coverage is not measured")
        return coverage
    if framework == "Jest":
        coverage = new_coverage(ctx, job, "Jest (built-in coverage)")
        command = [*test_command("Jest", script, job.out_dir / "jest.json"), "--coverage",
                   "--coverageReporters=cobertura", f"--coverageDirectory={job.out_dir}"]
    elif framework == "Vitest":
        coverage = new_coverage(ctx, job, "Vitest (@vitest/coverage-v8)")
        version = _installed_version(root, job.folder, "vitest")
        if version is None:
            coverage.notes.append("the installed Vitest version is unknown")
            return finish_coverage(ctx, coverage, job, None, _no_command(), what="Vitest")
        package = f"@vitest/coverage-v8@{version}"
        coverage.version = package
        added = ctx.run(coverage, IMAGE, ["sh", "-c", add_command(suite.manager, package)], workdir=job.folder,
                        log_dir=job.out_dir, label="install", online=True, env=env)
        note_prepare(coverage, added, f"installing {package}")
        command = [*test_command("Vitest", script, job.out_dir / "junit.xml"), "--coverage.enabled=true",
                   "--coverage.provider=v8", "--coverage.reporter=cobertura", "--coverage.reportOnFailure=true",
                   f"--coverage.reportsDirectory={job.out_dir}"]
    else:
        coverage = new_coverage(ctx, job, "c8")
        coverage.version = ctx.runner.image_versions(IMAGE).get("c8")
        test = test_command(framework, script, job.out_dir / "junit.xml") if framework else \
            ["sh", "-c", f"{_runner(suite.manager)} test"]
        command = ["c8", "--reporter=cobertura", f"--reports-dir={job.out_dir}",
                   f"--temp-directory={job.out_dir / 'c8-tmp'}", *test]
    result = ctx.run(coverage, IMAGE, _with_binaries(root, command), workdir=job.folder, log_dir=job.out_dir,
                     label="coverage", online=False, env=env)
    files = read_report(coverage, report, lambda path: parse_cobertura(path, job.workspace))
    return finish_coverage(ctx, coverage, job, files, result, what=coverage.tool)


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    mutation = new_mutation(ctx, job, "StrykerJS")
    mutation.version = f"StrykerJS {STRYKER_VERSION}"
    if failing:
        mutation.excluded_tests = failing
        return skip(mutation, f"StrykerJS needs all tests to pass and cannot leave single tests out "
                              f"({len(failing)} failed in the test run)")
    suite = job.suite
    framework = next((name for name in RUNNABLE if name in suite.frameworks), None)
    script = _test_script(_package(job.folder / "package.json"))
    root = install_root(job.folder, job.workspace, suite.manager)
    env = _env(ctx)
    runner = STRYKER_RUNNERS.get(framework or "")
    packages = [f"@stryker-mutator/core@{STRYKER_VERSION}"]
    if runner:
        packages.append(f"@stryker-mutator/{runner}-runner@{STRYKER_VERSION}")
    added = ctx.run(mutation, IMAGE, ["sh", "-c", add_command(suite.manager, " ".join(packages))], workdir=job.folder,
                    log_dir=job.out_dir, label="install", online=True, env=env)
    note_prepare(mutation, added, "installing StrykerJS")
    folder_prefix = "" if suite.root == "." else f"{suite.root}/"
    settings: dict[str, object] = {
        "mutate": [file.removeprefix(folder_prefix) for file in files],
        "testRunner": runner or "command",
        "reporters": ["json", "clear-text"],
        "jsonReporter": {"fileName": str(job.out_dir / "mutation.json")},
        "tempDirName": ".stryker-tmp",
    }
    if runner:
        settings["plugins"] = [f"@stryker-mutator/{runner}-runner"]
    else:
        command = " ".join(test_command(framework, script, job.out_dir / "junit.xml")) if framework else \
            f"{_runner(suite.manager)} test"
        settings["commandRunner"] = {"command": command}
    config = job.folder / "code-analysis-stryker.json"  # the project's own Stryker settings are not used
    config.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    result = ctx.run(mutation, IMAGE, _with_binaries(root, ["stryker", "run", config.name]), workdir=job.folder,
                     log_dir=job.out_dir, label="stryker", online=False, env=env, timeout="mutation")
    report = job.out_dir / "mutation.json"
    mutants = None
    if report.is_file():
        try:
            mutants = parse_stryker(report, job.workspace)
        except ReportError as exc:
            mutation.notes.append(f"the Stryker report could not be read: {exc}")
    return finish_mutation(ctx, mutation, job, mutants, result, what="StrykerJS")


def add_command(manager: str, package: str) -> str:
    """Add a development package to the copy, without lifecycle scripts."""
    if manager == "Yarn":
        return (f'case "$(yarn --version)" in 1.*) yarn add --dev --ignore-scripts {package} ;; '
                f"*) yarn add --dev {package} ;; esac")
    if manager == "pnpm":
        return f"pnpm add --save-dev --ignore-scripts {package}"
    if manager == "Bun":
        return f"bun add --dev --ignore-scripts {package}"
    return f"npm install --no-save --ignore-scripts --no-audit --no-fund {package}"


def _installed_version(root: Path, folder: Path, name: str) -> str | None:
    for base in (folder, root):
        version = _package(base / "node_modules" / name / "package.json").get("version")
        if isinstance(version, str):
            return version
    return None


def _no_command() -> CommandResult:
    return CommandResult((), None, "", "", 0.0)


def _with_binaries(root: Path, command: list[str]) -> list[str]:
    """Run the command with the package binaries on the PATH (Yarn Plug'n'Play: through yarn)."""
    if (root / ".pnp.cjs").is_file():  # no node_modules: Yarn runs the package binaries
        command = ["yarn", *command]
    return ["sh", "-c", WITH_BINARIES, "sh", str(root), *command]


def _env(ctx: TestContext) -> dict[str, str]:
    cache = ctx.cache_dir / "node"
    return {"CI": "true", "COREPACK_HOME": str(cache / "corepack"), "npm_config_cache": str(cache / "npm"),
            "YARN_CACHE_FOLDER": str(cache / "yarn"), "YARN_GLOBAL_FOLDER": str(cache / "yarn-berry"),
            "npm_config_store_dir": str(cache / "pnpm-store"), "BUN_INSTALL_CACHE_DIR": str(cache / "bun")}


def install_root(folder: Path, workspace: Path, manager: str) -> Path:
    """The folder with the package manager's lock file (a workspace root), else the suite's folder."""
    current = folder
    while True:
        if any((current / name).is_file() for name in LOCKFILES.get(manager, ())):
            return current
        if current == workspace or current == current.parent:
            return folder
        current = current.parent


def install_command(manager: str, root: Path) -> str:
    """Install without lifecycle scripts; with the lock file as it is, where there is one."""
    locked = any((root / name).is_file() for name in LOCKFILES.get(manager, ()))
    if manager == "Yarn":
        return ('case "$(yarn --version)" in 1.*) yarn install --frozen-lockfile --ignore-scripts --non-interactive ;; '
                "*) yarn install --immutable --mode=skip-build ;; esac")
    if manager == "pnpm":
        return f"pnpm install {'--frozen-lockfile ' if locked else ''}--ignore-scripts"
    if manager == "Bun":
        return f"bun install {'--frozen-lockfile ' if locked else ''}--ignore-scripts"
    return f"npm {'ci' if locked else 'install'} --ignore-scripts --no-audit --no-fund"


def rebuild_command(manager: str) -> str:
    """Run the install scripts of the packages, offline."""
    if manager == "Yarn":
        return 'case "$(yarn --version)" in 1.*) yarn install --frozen-lockfile --offline --non-interactive ;; ' \
               "*) yarn rebuild ;; esac"
    if manager == "pnpm":
        return "pnpm rebuild"
    if manager == "Bun":
        return "true"  # Bun runs install scripts only for trusted packages; they were skipped
    return "npm rebuild"


def test_command(framework: str, script: str | None, report: Path) -> list[str]:
    """The framework's command (with the test script's arguments if it is a single call of it) and a reporter."""
    binary = RUNNABLE[framework]
    words = _single_call(script, binary)
    if framework == "Jest":
        return [*(words or ["jest"]), "--ci", "--json", f"--outputFile={report}"]
    if framework == "Vitest":
        words = words or ["vitest", "run"]
        if len(words) < 2 or words[1] != "run":
            words = [words[0], "run", *words[1:]]
        return [*words, "--reporter=default", "--reporter=junit", f"--outputFile.junit={report}"]
    if framework == "Mocha":
        return [*(words or ["mocha"]), "--reporter", "xunit", "--reporter-option", f"output={report}"]
    if framework == "node:test":
        words = words if words and "--test" in words else ["node", "--test"]
        return [words[0], "--test-reporter=spec", "--test-reporter-destination=stdout", "--test-reporter=junit",
                f"--test-reporter-destination={report}", *words[1:]]
    words = words if words and words[1:2] == ["test"] else ["bun", "test"]
    return [*words, "--reporter=junit", f"--reporter-outfile={report}"]


def _single_call(script: str | None, binary: str) -> list[str] | None:
    if not script or any(operator in script for operator in _SHELL_OPERATORS):
        return None
    try:
        words = shlex.split(script)
    except ValueError:
        return None
    return words if words and words[0] == binary else None


def _runner(manager: str) -> str:
    return {"Yarn": "yarn", "pnpm": "pnpm", "Bun": "bun run"}.get(manager, "npm")


def _package(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _test_script(package: dict) -> str | None:
    scripts = package.get("scripts")
    script = scripts.get("test") if isinstance(scripts, dict) else None
    return script if isinstance(script, str) and "no test specified" not in script else None

"""5.4: what the mutation tools of all languages share.

- Which files are mutated: in mode "sample" a random sample (fixed seed) of
  the source files the tests cover (5.3), in mode "full" every source file of
  the suite.
- Which tests are left out: those that failed in 5.2. The tools need a passing
  test run without mutants.
- The reports, mapped to the mutant states of the mutation-testing-elements
  schema: Stryker's JSON (StrykerJS, Stryker.NET), PIT's XML, cargo-mutants'
  outcomes.json; mutmut is read in languages/python.py.
"""

from __future__ import annotations

import json
import random
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from scheduler.linting.model import CommandResult, Status
from scheduler.testing.context import Job, TestContext, reason
from scheduler.testing.model import CoverageRun, Mutant, MutationRun, SuiteRun
from scheduler.testing.reports import ReportError
from scheduler.testing.sources import suite_sources

MAX_ORIGINAL = 300
UNDETECTED = ("Survived", "NoCoverage")
# Tool states -> schema states.
PIT_STATES = {"KILLED": "Killed", "SURVIVED": "Survived", "NO_COVERAGE": "NoCoverage", "TIMED_OUT": "Timeout",
              "NON_VIABLE": "CompileError", "MEMORY_ERROR": "RuntimeError", "RUN_ERROR": "RuntimeError",
              "NOT_STARTED": "Pending", "STARTED": "Pending", "EQUIVALENT": "Ignored"}
CARGO_MUTANTS_STATES = {"CaughtMutant": "Killed", "MissedMutant": "Survived", "Timeout": "Timeout",
                        "Unviable": "CompileError"}


def new_mutation(ctx: TestContext, job: Job, tool: str) -> MutationRun:
    job.out_dir.mkdir(parents=True, exist_ok=True)
    return MutationRun(suite=job.suite.label, language=job.suite.language, tool=tool, status=Status.FAILED,
                       mode=ctx.config.mutation.mode, output_dir=job.out_dir.relative_to(ctx.step_dir).as_posix())


def select_files(ctx: TestContext, job: Job, coverage: CoverageRun | None) -> tuple[list[str], bool, int, list[str]]:
    """The project-relative files to mutate, whether they are a sample, the number of candidates, notes."""
    config = ctx.config.mutation
    notes = []
    if config.mode == "full":
        candidates = suite_sources(ctx.acceptance, ctx.suites, job.suite)
    elif coverage is not None and coverage.status in (Status.OK, Status.PARTIAL):
        candidates = [file.file for file in coverage.files if file.lines_covered > 0]
    else:
        candidates = suite_sources(ctx.acceptance, ctx.suites, job.suite)
        notes.append("no coverage data: the sample is drawn from all source files")
    candidates = sorted(candidates)
    if config.mode == "sample" and len(candidates) > config.sample_files:
        return sorted(random.Random(config.seed).sample(candidates, config.sample_files)), True, len(candidates), notes
    return candidates, False, len(candidates), notes


def failing_tests(run: SuiteRun) -> list[str]:
    return [test.id for test in run.tests if test.outcome in ("failed", "error")]


def finish_mutation(ctx: TestContext, mutation: MutationRun, job: Job, mutants: list[Mutant] | None,
                    result: CommandResult, *, what: str) -> MutationRun:
    """Status from the command and the mutants read; the source line for undetected mutants.

    Reaching the configured time limit gives `partial` (with the mutants
    written so far), not `failed`: the limit is a setting, not an error.
    """
    if result.timed_out:
        minutes = ctx.config.mutation.time_limit_minutes
        mutation.status = Status.PARTIAL
        mutation.notes.append(f"time limit of {minutes} minutes reached (config/tests.toml): "
                              + (f"{len(mutants)} mutants were evaluated before" if mutants
                                 else f"{what} wrote no results before"))
    elif mutants is None:
        mutation.status = Status.FAILED
        mutation.notes.append(f"{what} wrote no report ({reason(result)})")
        return mutation
    else:
        mutation.status = Status.OK
    lines: dict[str, list[str]] = {}
    with_source = []
    for mutant in mutants or []:
        if mutant.status in UNDETECTED and mutant.line and not mutant.original:
            if mutant.file not in lines:
                path = job.workspace / mutant.file
                lines[mutant.file] = path.read_text(encoding="utf-8", errors="replace").splitlines() \
                    if path.is_file() else []
            source = lines[mutant.file]
            if 0 < mutant.line <= len(source):
                mutant = Mutant(mutant.file, mutant.line, mutant.mutator, mutant.replacement, mutant.status,
                                source[mutant.line - 1].strip()[:MAX_ORIGINAL])
        with_source.append(mutant)
    mutation.mutants = with_source
    return mutation


def skip(mutation: MutationRun, note: str) -> MutationRun:
    mutation.status = Status.SKIPPED
    mutation.notes.append(note)
    return mutation


def relative_to_workspace(path: Path, workspace: Path) -> str | None:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except ValueError:
        return None


# --- reports -----------------------------------------------------------------------


def parse_stryker(path: Path, workspace: Path) -> list[Mutant]:
    """mutation-testing-elements JSON (StrykerJS, Stryker.NET): file keys absolute or relative to projectRoot."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc
    root = Path(data.get("projectRoot") or path.parent)
    mutants = []
    for name, entry in (data.get("files") or {}).items():
        file = relative_to_workspace(Path(name) if Path(name).is_absolute() else root / name, workspace)
        if file is None:
            continue
        for mutant in entry.get("mutants") or []:
            start = (mutant.get("location") or {}).get("start") or {}
            status = mutant.get("status") or "Pending"
            mutants.append(Mutant(file, start.get("line"), mutant.get("mutatorName") or "",
                                  str(mutant.get("replacement") or ""), status if status in _STRYKER else "Pending"))
    return mutants


_STRYKER = {"Killed", "Survived", "NoCoverage", "Timeout", "CompileError", "RuntimeError", "Ignored", "Pending"}


def parse_pit(path: Path, java_files: Iterable[str]) -> list[Mutant]:
    """PIT's mutations.xml; files are found by package path and file name, like JaCoCo's."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc
    by_suffix: dict[str, list[str]] = {}
    for file in java_files:
        parts = file.split("/")
        for start in range(len(parts)):
            by_suffix.setdefault("/".join(parts[start:]), []).append(file)
    mutants = []
    for element in root.iter("mutation"):
        mutated_class = element.findtext("mutatedClass") or ""  # e.g. "calc.Calc" or "calc.Calc$Inner"
        package = mutated_class.rsplit(".", 1)[0].replace(".", "/") if "." in mutated_class else ""
        name = "/".join(filter(None, [package, element.findtext("sourceFile") or ""]))
        matches = by_suffix.get(name, [])
        if len(matches) != 1:
            continue
        line = element.findtext("lineNumber")
        mutator = (element.findtext("mutator") or "").rsplit(".", 1)[-1]
        mutants.append(Mutant(matches[0], int(line) if line and line.isdigit() else None, mutator,
                              element.findtext("description") or "",
                              PIT_STATES.get(element.get("status") or "", "Pending")))
    return mutants


def parse_cargo_mutants(path: Path, folder: Path, workspace: Path) -> list[Mutant]:
    """cargo-mutants' mutants.out/outcomes.json; file paths are relative to the tree (the suite folder)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc
    mutants = []
    for outcome in data.get("outcomes") or []:
        scenario = outcome.get("scenario")
        if not isinstance(scenario, dict) or "Mutant" not in scenario:
            continue  # the baseline
        mutant = scenario["Mutant"]
        file = relative_to_workspace(folder / (mutant.get("file") or ""), workspace)
        if file is None:
            continue
        start = ((mutant.get("span") or {}).get("start") or {})
        mutants.append(Mutant(file, start.get("line"), mutant.get("genre") or "",
                              mutant.get("name") or mutant.get("replacement") or "",
                              CARGO_MUTANTS_STATES.get(outcome.get("summary") or "", "RuntimeError")))
    return mutants

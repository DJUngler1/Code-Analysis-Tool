"""Read the test reports of the frameworks into TestCase lists (5.2).

- JUnit XML: pytest, Maven Surefire, Gradle, CTest, Vitest, Mocha (xunit), node:test, Bun.
- TRX: dotnet test (VSTest).
- Jest's JSON report.
- libtest's text output: cargo test (its JSON output needs a nightly toolchain).

Test ids must not depend on where the copy lies: the runs after step 2 and
at HEAD are compared by id.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from scheduler.testing.model import TestCase

MAX_MESSAGE = 1000


class ReportError(Exception):
    """A report exists but cannot be read."""


def shorten(text: str | None, limit: int = MAX_MESSAGE) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- JUnit XML ---------------------------------------------------------------------


def parse_junit(path: Path) -> list[TestCase]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc
    found: list[tuple[tuple[str, ...], TestCase]] = []
    _walk_junit(root, (), found)
    return _unique([(path_, case) for path_, case in found])


def _walk_junit(element: ET.Element, suites: tuple[str, ...], found: list[tuple[tuple[str, ...], TestCase]]) -> None:
    for child in element:
        if child.tag == "testsuite":
            _walk_junit(child, (*suites, child.get("name") or ""), found)
        elif child.tag == "testcase":
            found.append((suites, _junit_case(child, suites)))
        elif child.tag == "testsuites":
            _walk_junit(child, suites, found)
    if element.tag == "testcase":  # a report that is a single test case
        found.append((suites, _junit_case(element, suites)))


_SCRIPT_FILE = re.compile(r"\.[cm]?[jt]sx?$")
# Class names that say nothing about the test: Node.js's test runner writes "test" for every test.
PLACEHOLDER_CLASSNAMES = ("", "test")


def _junit_case(case: ET.Element, suites: tuple[str, ...]) -> TestCase:
    name = case.get("name") or ""
    classname = case.get("classname") or ""
    outcome, message = "passed", ""
    for child in case:
        if child.tag in ("failure", "error"):
            outcome = "failed" if child.tag == "failure" else "error"
            # The message attribute is a summary; the text (or CTest's output) has the details.
            details = (child.text or "").strip() or (case.findtext("system-out") or "").strip()
            message = "\n".join(part for part in (child.get("message"), details) if part)
            break
        if child.tag == "skipped":
            outcome, message = "skipped", child.get("message") or child.text or ""
    if case.get("status") in ("notrun", "disabled") and outcome == "passed":  # CTest
        outcome = "skipped"
    if classname in PLACEHOLDER_CLASSNAMES:
        test_id = " > ".join([*filter(None, suites), name])
    elif classname == name:
        test_id = name
    else:  # a file name as class name (Vitest, Bun): "test/calc.test.js > calc > adds"
        separator = " > " if "/" in classname or _SCRIPT_FILE.search(classname) else "."
        test_id = f"{classname}{separator}{name}"
    return TestCase(test_id, outcome, _seconds(case.get("time")), shorten(message))


def _seconds(value: str | None) -> float | None:
    try:
        return round(float(value), 3) if value else None
    except ValueError:
        return None


def _unique(found: list[tuple[tuple[str, ...], TestCase]]) -> list[TestCase]:
    """Ids that occur twice get the names of their test suites (e.g. describe blocks), then a number."""
    counts = Counter(case.id for _, case in found)
    cases = []
    for suites, case in found:
        if counts[case.id] > 1:
            case = TestCase(" > ".join([*filter(None, suites), case.id]), case.outcome, case.duration_seconds,
                            case.message)
        cases.append(case)
    return _numbered(cases)


def _numbered(cases: Iterable[TestCase]) -> list[TestCase]:
    seen: Counter[str] = Counter()
    result = []
    for case in cases:
        seen[case.id] += 1
        if seen[case.id] > 1:
            case = TestCase(f"{case.id} #{seen[case.id]}", case.outcome, case.duration_seconds, case.message)
        result.append(case)
    return result


# --- TRX (VSTest) ------------------------------------------------------------------

_TRX = "{http://microsoft.com/schemas/VisualStudio/TeamTest/2010}"
TRX_OUTCOMES = {"passed": "passed", "failed": "failed", "notexecuted": "skipped", "inconclusive": "skipped",
                "pending": "skipped", "disconnected": "error", "aborted": "error", "error": "error",
                "timeout": "failed", "warning": "passed", "completed": "passed"}
_TRX_TFM = re.compile(r"_(net[\w.\-]+?)_\d")


def parse_trx(paths: list[Path]) -> list[TestCase]:
    """All TRX files of a run. With several (one per target framework), the framework goes into the id."""
    cases: list[TestCase] = []
    for path in sorted(paths):
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as exc:
            raise ReportError(f"{path.name}: {exc}") from exc
        classes = {test.get("id"): (method.get("className") or "")
                   for test in root.iter(f"{_TRX}UnitTest")
                   for method in test.iter(f"{_TRX}TestMethod")}
        tfm = _TRX_TFM.search(path.name)
        suffix = f" [{tfm.group(1)}]" if len(paths) > 1 and tfm else ""
        for result in root.iter(f"{_TRX}UnitTestResult"):
            name = result.get("testName") or ""
            classname = classes.get(result.get("testId"), "")
            test_id = name if not classname or name.startswith(classname) else f"{classname}.{name}"
            message = "".join(result.find(f"{_TRX}Output/{_TRX}ErrorInfo/{_TRX}Message").itertext()) \
                if result.find(f"{_TRX}Output/{_TRX}ErrorInfo/{_TRX}Message") is not None else ""
            outcome = TRX_OUTCOMES.get((result.get("outcome") or "").lower(), "error")
            cases.append(TestCase(test_id + suffix, outcome, _trx_duration(result.get("duration")), shorten(message)))
    return _numbered(cases)


def _trx_duration(value: str | None) -> float | None:
    """"00:00:01.2340000" -> 1.234."""
    match = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", value or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return round(int(hours) * 3600 + int(minutes) * 60 + float(seconds), 3)


# --- Jest JSON ---------------------------------------------------------------------

JEST_OUTCOMES = {"passed": "passed", "failed": "failed", "pending": "skipped", "skipped": "skipped",
                 "todo": "skipped", "disabled": "skipped", "focused": "passed"}


def parse_jest(path: Path, root: Path) -> list[TestCase]:
    """Jest's --json report; `root` is the folder the file paths are made relative to."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"{path.name}: {exc}") from exc
    cases: list[TestCase] = []
    for result in data.get("testResults") or []:
        file = _relative(result.get("name") or result.get("testFilePath") or "", root)
        assertions = result.get("assertionResults") or []
        for assertion in assertions:
            title = assertion.get("fullName") or " ".join([*assertion.get("ancestorTitles", []),
                                                           assertion.get("title", "")])
            duration = assertion.get("duration")
            cases.append(TestCase(f"{file} > {title}", JEST_OUTCOMES.get(assertion.get("status"), "error"),
                                  round(duration / 1000, 3) if isinstance(duration, (int, float)) else None,
                                  shorten("\n".join(assertion.get("failureMessages") or []))))
        if not assertions and result.get("status") == "failed":  # the test file itself failed (e.g. syntax)
            cases.append(TestCase(file, "error", None, shorten(result.get("message"))))
    return _numbered(cases)


def _relative(file: str, root: Path) -> str:
    try:
        return Path(file).relative_to(root).as_posix()
    except ValueError:
        return file


# --- libtest (cargo test) ----------------------------------------------------------

_LIBTEST_RUNNING = re.compile(r"^\s*Running (?:unittests |tests? )?(\S+) \((?:.*/)?([^/()]+?)(?:-[0-9a-f]{8,})?\)")
_LIBTEST_DOC = re.compile(r"^\s*Doc-tests (\S+)")
_LIBTEST_TEST = re.compile(r"^test (.+?) \.\.\. (ok|FAILED|ignored(?:, .*)?)$")
_LIBTEST_FAILURE = re.compile(r"^---- (.+?) stdout ----$")
_DOCTEST_LINE = re.compile(r" \(line \d+\)$")
LIBTEST_OUTCOMES = {"ok": "passed", "FAILED": "failed"}


def parse_libtest(output: str) -> list[TestCase]:
    """The text output of `cargo test` (stdout and stderr in one stream, in order).

    Ids are "<crate> <target>::<test>"; for doctests the line number is left
    out, because formatting (step 2) moves it.
    """
    cases: list[TestCase] = []
    messages: dict[tuple[str, str], list[str]] = {}
    target, current_failure = "", None
    for line in output.splitlines():
        if match := _LIBTEST_RUNNING.match(line):
            target, current_failure = f"{match.group(2)} {match.group(1)}", None
            continue
        if match := _LIBTEST_DOC.match(line):
            target, current_failure = f"{match.group(1)} doctests", None
            continue
        if match := _LIBTEST_TEST.match(line):
            name, result = match.groups()
            cases.append(TestCase(f"{target}::{name}", LIBTEST_OUTCOMES.get(result, "skipped")))
            continue
        if match := _LIBTEST_FAILURE.match(line):
            current_failure = (target, match.group(1))
            messages[current_failure] = []
            continue
        if current_failure is not None:
            if line.startswith(("failures:", "test result:")):
                current_failure = None
            else:
                messages[current_failure].append(line)
    result = []
    for case in cases:
        target_name, _, name = case.id.partition("::")
        message = "\n".join(messages.get((target_name, name), []))
        test_id = _DOCTEST_LINE.sub("", case.id) if target_name.endswith(" doctests") else case.id
        result.append(TestCase(test_id, case.outcome, None, shorten(message)))
    return _numbered(result)


def libtest_ran(output: str) -> bool:
    """True if at least one test binary reported a result."""
    return any(line.startswith("test result:") for line in output.splitlines())

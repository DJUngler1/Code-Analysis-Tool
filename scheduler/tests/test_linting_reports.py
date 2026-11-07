"""Parsers, tested with reports recorded from real runs of the pinned tools
(paths replaced by /work/...), see tests/fixtures/linting/."""

from pathlib import Path

from scheduler.linting.reports import (
    files_with_errors,
    parse_clang_tidy,
    parse_clippy,
    parse_dotnet_report,
    parse_sarif,
    relative_file,
)

FIXTURES = Path(__file__).parent / "fixtures" / "linting"


def test_relative_file():
    project = Path("/work/py")
    assert relative_file("file:///work/py/src/a%20b.py", project) == "src/a b.py"
    assert relative_file("/work/py/m.py", project) == "m.py"
    assert relative_file("m.py", project) == "m.py"
    assert relative_file("../include/x.h", project, Path("/work/py/build")) == "include/x.h"
    assert relative_file("/usr/include/stdio.h", project) is None
    assert relative_file(None, project) is None


def test_ruff_sarif():
    report = parse_sarif(FIXTURES / "ruff.sarif", language="Python", tool="ruff check", project=Path("/work/py"))
    rules = [(finding.rule, finding.file, finding.line) for finding in report.findings]
    assert ("F841", "m.py", 5) in rules
    assert ("invalid-syntax", "bad.py", 1) in rules
    assert {finding.severity for finding in report.findings} == {"error"}
    assert report.problems == []


def test_biome_sarif():
    report = parse_sarif(FIXTURES / "biome.sarif", language="JavaScript/TypeScript", tool="biome", project=Path("/work/js"))
    found = {(finding.rule, finding.severity, finding.file) for finding in report.findings}
    assert found == {
        ("lint/correctness/noUnusedVariables", "warning", "src/main.ts"),
        ("lint/suspicious/noDoubleEquals", "error", "src/main.ts"),
    }


def test_pmd_sarif_with_unparsed_file():
    report = parse_sarif(FIXTURES / "pmd.sarif", language="Java", tool="pmd", project=Path("/work/java"))
    assert {finding.rule for finding in report.findings} >= {"CompareObjectsWithEquals", "NoPackage"}
    assert all(finding.file == "Ok.java" for finding in report.findings)
    assert len(report.problems) == 1
    assert report.problems[0].startswith("Bad.java: ParseException")


def test_dotnet_report_keeps_only_analyzer_fixes():
    findings = parse_dotnet_report(FIXTURES / "format-report.json", project=Path("/work/cs"))
    assert [(finding.rule, finding.file, finding.fixed) for finding in findings] == [("CA2200", "Program.cs", True)]


def test_clippy_messages():
    lines = (FIXTURES / "clippy.json").read_text().splitlines()
    findings = parse_clippy(lines, project=Path("/work/rs"), workspace=Path("/work/rs"))
    assert [(finding.rule, finding.file, finding.line) for finding in findings] == [
        ("unused_variables", "src/main.rs", 4),
        ("clippy::len_zero", "src/main.rs", 3),
    ]


def test_run_clang_tidy_output():
    output = (FIXTURES / "run-clang-tidy.out").read_text()
    directories = {"/work/cpp/a.cpp": Path("/work/cpp"), "/work/cpp/b.c": Path("/work/cpp")}
    report = parse_clang_tidy(output, project=Path("/work/cpp"), directories=directories)
    assert [(finding.rule, finding.file, finding.line) for finding in report.findings] == [
        ("modernize-use-nullptr", "a.cpp", 4)
    ]
    assert (report.suppressed, report.nolint) == (4, 1)


def test_clang_tidy_deduplicates_header_findings_and_drops_system_headers():
    output = "\n".join([
        "/work/cpp/include/x.h:2:5: warning: use nullptr [modernize-use-nullptr]",
        "/work/cpp/include/x.h:2:5: warning: use nullptr [modernize-use-nullptr]",
        "/usr/include/stdio.h:1:1: warning: something [bugprone-foo]",
        "/work/cpp/src/a.cpp:1:10: error: 'fmt/core.h' file not found [clang-diagnostic-error]",
        "Suppressed 7 warnings (5 in non-user code, 2 NOLINT).",
    ])
    report = parse_clang_tidy(output, project=Path("/work/cpp"))
    assert [(finding.file, finding.severity) for finding in report.findings] == [
        ("include/x.h", "warning"),
        ("src/a.cpp", "error"),
    ]
    assert (report.suppressed, report.nolint) == (7, 2)


def test_files_with_errors():
    java = (
        "/work/java/Bad.java:1:28: error: illegal start of type\n"
        "public class Bad { void f( { }\n"
        "/work/java/Bad.java:1:31: error: reached end of file while parsing\n"
    )
    assert files_with_errors(java, project=Path("/work/java"), kind="java") == ["Bad.java"]
    ruff = "error: Failed to parse bad.py:1:12: Expected a parameter\n1 file left unchanged\n"
    assert files_with_errors(ruff, project=Path("/work/py"), kind="ruff") == ["bad.py"]

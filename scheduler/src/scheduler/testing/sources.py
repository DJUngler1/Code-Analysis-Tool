"""Which files the coverage of a suite counts (5.3).

The source files of the suite's language below its folder, without:
- test code: the test files of 5.1, files in test folders (test/, tests/,
  __tests__/, spec/, Maven's and Gradle's src/test/ etc.) and test support
  files (conftest.py);
- generated files (step 1) and files of nested suites of the same language;
- files that are not part of the program: build scripts and tool
  configuration (setup.py, noxfile.py, build.rs, *.config.js, ...),
  documentation and examples.
Rust's source files with #[cfg(test)] modules stay: they are program code.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from scheduler.acceptance import AcceptanceResult
from scheduler.linting.files import source_files
from scheduler.testing.detect import C_SUFFIXES, JS_SUFFIXES
from scheduler.testing.model import Suite

SUFFIXES = {
    "Python": (".py",),
    "JavaScript/TypeScript": JS_SUFFIXES,
    "Java": (".java",),
    "C#": (".cs",),
    "Rust": (".rs",),
    "C/C++": C_SUFFIXES,
}
# Only these count as "not loaded" for C/C++: headers often have no code of their own.
C_IMPLEMENTATION = (".c", ".cpp", ".cc", ".cxx", ".c++")
TEST_FOLDERS = {"test", "tests", "__tests__", "spec", "specs", "testing", "unittest", "unittests",
                "androidTest", "testFixtures", "integrationTest", "benches"}
OTHER_FOLDERS = {"docs", "doc", "examples", "example", "samples", "sample", "benchmarks"}
NOT_PROGRAM = {"conftest.py", "setup.py", "noxfile.py", "fabfile.py", "build.rs", "AssemblyInfo.cs"}


def suite_sources(acceptance: AcceptanceResult, suites: list[Suite], suite: Suite) -> list[str]:
    """Project-relative source files whose coverage the suite's run should report."""
    project = acceptance.project
    root = PurePosixPath(suite.root)
    test_files = {file for other in suites if other.language == suite.language for file in other.test_files}
    # Files of nested suites of the same language belong to them (e.g. a package.json in a sub-folder).
    nested = [PurePosixPath(other.root) for other in suites
              if other.language == suite.language and other is not suite and other.root != suite.root
              and _within(PurePosixPath(other.root), root)]
    if suite.language == "C#":  # a suite is a test project: its sources are the rest of the C# code
        root = PurePosixPath(".")
        nested = [PurePosixPath(other.root) for other in suites if other.language == "C#"]
    generated = set(acceptance.languages.excluded)
    result = []
    for path in source_files(project, SUFFIXES[suite.language]):
        file = PurePosixPath(path.as_posix())
        if not _within(file, root) or any(_within(file, folder) for folder in nested):
            continue
        relative = file.relative_to(root) if str(root) != "." else file
        if file.as_posix() in test_files or file.as_posix() in generated or _excluded(relative, suite.language):
            continue
        result.append(file.as_posix())
    return result


def counts_as_not_loaded(file: str, language: str) -> bool:
    return language != "C/C++" or Path(file).suffix.lower() in C_IMPLEMENTATION


def _excluded(relative: PurePosixPath, language: str) -> bool:
    folders = set(relative.parts[:-1])
    if folders & TEST_FOLDERS or folders & OTHER_FOLDERS:
        return True
    if "src/test" in relative.as_posix() or relative.name in NOT_PROGRAM:
        return True
    if language == "JavaScript/TypeScript":
        return ".config." in relative.name or relative.name.endswith((".d.ts", ".d.mts", ".d.cts")) \
            or relative.name.startswith(".")
    if language == "Rust":
        return "examples" in folders
    return False


def _within(path: PurePosixPath, folder: PurePosixPath) -> bool:
    return str(folder) == "." or path == folder or folder in path.parents

"""5.1: find the test suites of a project ("Tests present?").

Static: only reads the files git knows (without vendored and build folders) and
runs nothing. Each language uses the default conventions of its test frameworks
(docs/testAnalysis/README.md). How many tests there are, and whether they pass,
comes from running them (5.2).
"""

from __future__ import annotations

import configparser
import json
import re
import tomllib
from collections.abc import Callable, Iterable, Sequence
from fnmatch import fnmatchcase
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.acceptance.package_managers import PackageManagerHit
from scheduler.linting.files import source_files
from scheduler.static_analysis.sonarqube import java_build_roots
from scheduler.testing.model import Suite

MAX_READ_BYTES = 2_000_000  # longer files are only read up to here

# Languages of step 1 grouped the way their tests run: JavaScript and TypeScript
# share package.json, C and C++ share the CMake build.
LANGUAGE_GROUPS = {
    "Python": ("Python",),
    "JavaScript/TypeScript": ("JavaScript", "TypeScript"),
    "Java": ("Java",),
    "C#": ("C#",),
    "Rust": ("Rust",),
    "C/C++": ("C", "C++"),
}
NO_PACKAGE_MANAGER = "no package manager found in step 1"


def detect_suites(acceptance: AcceptanceResult) -> tuple[list[Suite], dict[str, str]]:
    """Test suites per language (primary language first), and the languages that were not checked."""
    project = acceptance.project
    files = source_files(project, None)
    suites: list[Suite] = []
    not_checked: dict[str, str] = {}
    for group in _groups(acceptance):
        hits = [hit for language in LANGUAGE_GROUPS[group] for hit in acceptance.package_managers.get(language, ())]
        hits = list(dict.fromkeys(hits))  # JavaScript and TypeScript share their package.json
        if not hits:
            not_checked[group] = NO_PACKAGE_MANAGER
            continue
        found, reason = DETECTORS[group](project, files, hits)
        if reason:
            not_checked[group] = reason
        suites += found
    return suites, not_checked


def _groups(acceptance: AcceptanceResult) -> list[str]:
    """Language groups in the order of step 1 (primary first)."""
    groups: list[str] = []
    for language in acceptance.package_managers:
        group = next((name for name, members in LANGUAGE_GROUPS.items() if language in members), None)
        if group and group not in groups:
            groups.append(group)
    return groups


# --- Python ------------------------------------------------------------------------

# pytest's defaults for `python_files` and unittest's default discovery pattern.
PYTEST_FILES = ("test_*.py", "*_test.py")
UNITTEST_FILES = ("test*.py",)
# Configuration files in pytest's order: (file, format, table or section).
PYTEST_CONFIGS = (
    ("pytest.toml", "toml", "pytest"), (".pytest.toml", "toml", "pytest"),
    ("pytest.ini", "ini", "pytest"), (".pytest.ini", "ini", "pytest"),
    ("pyproject.toml", "toml", "tool.pytest"), ("tox.ini", "ini", "pytest"), ("setup.cfg", "ini", "tool:pytest"),
)
# Module-level test functions and plain test classes: only pytest runs them.
_PY_PYTEST_TEST = re.compile(
    r"^(?:async\s+)?def\s+test\w*\s*\(|^class\s+Test\w*\s*(?::|\(\s*(?:object)?\s*\)\s*:)", re.MULTILINE)
_PY_TESTCASE = re.compile(r"^\s*class\s+\w+\s*\([^)]*\bTestCase\b", re.MULTILINE)
_PY_TEST_METHOD = re.compile(r"^\s+(?:async\s+)?def\s+test\w*\s*\(", re.MULTILINE)
_PY_PYTEST_IMPORT = re.compile(r"^\s*(?:import\s+pytest\b|from\s+pytest\b)", re.MULTILINE)
_PYTEST_NAME = re.compile(r"(?<![\w-])pytest(?![\w-])", re.IGNORECASE)
_PY_DEPENDENCY_FILES = ("pyproject.toml", "setup.cfg", "setup.py", "Pipfile", "tox.ini", "noxfile.py")


def _python(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    folders: dict[Path, PackageManagerHit] = {}
    for hit in hits:
        folder = hit.manifest.parent
        if folder.name == "requirements":  # requirements/dev.txt belongs to the project above
            folder = folder.parent
        folders.setdefault(folder, hit)
    suites = []
    python_files = [path for path in files if path.suffix == ".py"]
    for root in _top_most(folders):
        hit = folders[root]
        suite = _suite("Python", root, hit.manager, hit.manifest)
        patterns, config = _pytest_patterns(project, root)
        if config:
            suite.evidence.append(f"pytest configuration in {config}")
            suite.frameworks.append("pytest")
        dependency = _pytest_dependency(project, root, files)
        if dependency:
            suite.evidence.append(f"pytest is a dependency ({dependency})")
            _add(suite.frameworks, "pytest")

        pytest_style, unittest_style = [], []
        for path in _inside(python_files, root):
            relative = path.relative_to(root)
            pytest_name = _matches(relative, patterns)
            if not (pytest_name or _matches(relative, UNITTEST_FILES)):
                continue
            text = _read(project / path)
            if pytest_name and (_PY_PYTEST_TEST.search(text) or _PY_PYTEST_IMPORT.search(text)):
                pytest_style.append(path)
            elif _PY_TESTCASE.search(text) and _PY_TEST_METHOD.search(text):
                unittest_style.append(path)
        if pytest_style:
            _add(suite.frameworks, "pytest")
            suite.evidence.append(f"{len(pytest_style)} test files with pytest tests ({' or '.join(patterns)})")
        if unittest_style:
            _add(suite.frameworks, "unittest")
            suite.evidence.append(f"{len(unittest_style)} test files with unittest.TestCase classes only")
        tests = sorted(pytest_style + unittest_style)
        suite.test_files = _posix(tests)
        suite.tests_found = bool(tests)
        suites.append(suite)
    return suites, None


def _pytest_patterns(project: Path, root: Path) -> tuple[tuple[str, ...], str | None]:
    """pytest's `python_files` and the file that configures pytest, if any.

    pytest uses the first match of PYTEST_CONFIGS; pytest.toml and pytest.ini
    match even when empty, the others only with their pytest table or section.
    """
    for name, kind, section in PYTEST_CONFIGS:
        path = project / root / name
        if not path.is_file():
            continue
        if kind == "toml":
            try:
                table: object = tomllib.loads(_read(path))
            except tomllib.TOMLDecodeError:
                table = {}
            for key in section.split("."):
                table = table.get(key) if isinstance(table, dict) else None
            if table is None and section != "pytest":  # pyproject.toml without a pytest table
                continue
            options = table if isinstance(table, dict) else {}
            if name == "pyproject.toml" and isinstance(options.get("ini_options"), dict):
                options = options["ini_options"]
        else:
            options = _ini_section(path, section)
            if options is None and name not in ("pytest.ini", ".pytest.ini"):
                continue
        return _split((options or {}).get("python_files")) or PYTEST_FILES, (root / name).as_posix()
    return PYTEST_FILES, None


def _ini_section(path: Path, section: str) -> dict[str, str] | None:
    if not path.is_file():
        return None
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(_read(path))
    except configparser.Error:
        return None
    return dict(parser[section]) if parser.has_section(section) else None


def _split(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(value.split())
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _pytest_dependency(project: Path, root: Path, files: list[Path]) -> str | None:
    candidates = [root / name for name in _PY_DEPENDENCY_FILES]
    candidates += sorted(path for path in files if path.suffix == ".txt" and (
        (path.parent == root and path.name.startswith("requirements")) or path.parent == root / "requirements"))
    for path in candidates:
        if (project / path).is_file() and _PYTEST_NAME.search(_read(project / path)):
            return path.as_posix()
    return None


# --- JavaScript and TypeScript -----------------------------------------------------

JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
# Default test file patterns of Jest, Vitest, the Node.js test runner, Mocha and Cypress.
JS_TEST_NAMES = ("*.test.*", "*.spec.*", "*-test.*", "*_test.*", "test-*.*", "test.*", "spec.*", "*.cy.*")
JS_TEST_FOLDERS = ("__tests__", "test")
# npm init's placeholder test script.
_NPM_PLACEHOLDER = re.compile(r"no test specified")
_JS_TEST_CALL = re.compile(r"\b(?:describe|it|test|suite)(?:\.\w+)*\s*\(")
_JS_NODE_TEST = re.compile(r"""from\s+['"]node:test['"]|require\(\s*['"]node:test['"]\s*\)""")
_JS_BUN_TEST = re.compile(r"""from\s+['"]bun:test['"]""")
# Package name -> framework.
JS_FRAMEWORK_PACKAGES = {
    "jest": "Jest", "vitest": "Vitest", "mocha": "Mocha", "ava": "AVA", "jasmine": "Jasmine",
    "@playwright/test": "Playwright", "cypress": "Cypress", "karma": "Karma", "tap": "tap", "uvu": "uvu",
}
# Command in the test script -> framework.
JS_FRAMEWORK_COMMANDS = (
    (re.compile(r"\bjest\b"), "Jest"), (re.compile(r"\breact-scripts\s+test\b"), "Jest"),
    (re.compile(r"\bvitest\b"), "Vitest"), (re.compile(r"\bmocha\b"), "Mocha"), (re.compile(r"\bava\b"), "AVA"),
    (re.compile(r"\bjasmine\b"), "Jasmine"), (re.compile(r"\bplaywright\s+test\b"), "Playwright"),
    (re.compile(r"\bcypress\s+run\b"), "Cypress"), (re.compile(r"\bkarma\b"), "Karma"),
    (re.compile(r"\bng\s+test\b"), "Angular CLI (ng test)"), (re.compile(r"\bnode\b[^&|;]*\s--test\b"), "node:test"),
    (re.compile(r"\bbun\s+test\b"), "Bun test"), (re.compile(r"\btap\b"), "tap"), (re.compile(r"\buvu\b"), "uvu"),
)
BROWSER_FRAMEWORKS = ("Playwright", "Cypress", "Karma")


def _javascript(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    roots = {hit.manifest.parent: hit for hit in hits}
    by_root: dict[Path, list[Path]] = {root: [] for root in roots}
    for path in files:
        if path.suffix in JS_SUFFIXES and not path.name.endswith((".d.ts", ".d.mts", ".d.cts")):
            owner = _owner(path, roots)
            if owner is not None:
                by_root[owner].append(path)
    suites = []
    for root in sorted(roots, key=_depth_first):
        hit = roots[root]
        suite = _suite("JavaScript/TypeScript", root, hit.manager, hit.manifest)
        try:
            package = json.loads(_read(project / hit.manifest))
        except json.JSONDecodeError as exc:
            package = {}
            suite.notes.append(f"package.json could not be read: {exc}")
        package = package if isinstance(package, dict) else {}
        scripts = package.get("scripts")
        script = scripts.get("test") if isinstance(scripts, dict) else None
        script = script if isinstance(script, str) and not _NPM_PLACEHOLDER.search(script) else None

        script_frameworks = [name for pattern, name in JS_FRAMEWORK_COMMANDS if script and pattern.search(script)]
        for name in script_frameworks:
            _add(suite.frameworks, name)
        if script:
            suite.evidence.append(f"test script: {script}")
        for section in ("devDependencies", "dependencies"):
            packages = package.get(section)
            for name in sorted(packages) if isinstance(packages, dict) else ():
                if name in JS_FRAMEWORK_PACKAGES:
                    _add(suite.frameworks, JS_FRAMEWORK_PACKAGES[name])
                    suite.evidence.append(f"{name} in {section}")

        tests = []
        for path in by_root[root]:
            relative = path.relative_to(root)
            by_name = _matches(Path(relative.name), JS_TEST_NAMES)
            by_folder = any(part in JS_TEST_FOLDERS for part in relative.parts[:-1])
            if not (by_name or by_folder):
                continue
            text = _read(project / path)
            if _JS_NODE_TEST.search(text):
                _add(suite.frameworks, "node:test")
            if _JS_BUN_TEST.search(text):
                _add(suite.frameworks, "Bun test")
            if _JS_TEST_CALL.search(text):
                tests.append(path)
        suite.test_files = _posix(tests)
        if tests:
            suite.evidence.append(f"{len(tests)} test files (*.test.*, *.spec.*, __tests__/, test/, ...)")
        if script and not script_frameworks and not tests:
            suite.notes.append("the test script uses no known test framework, and no test files were found")
        browsers = [name for name in suite.frameworks if name in BROWSER_FRAMEWORKS]
        if browsers:
            suite.notes.append(f"{', '.join(browsers)} run(s) the tests in a browser")
        suite.tests_found = bool(tests) or bool(script_frameworks)
        suites.append(suite)
    return suites, None


# --- Java --------------------------------------------------------------------------

_JAVA_TEST_ANNOTATION = re.compile(
    r"@(?:org\.junit\.(?:jupiter\.api\.)?|org\.testng\.annotations\.)?"
    r"(?:Test|ParameterizedTest|RepeatedTest|TestFactory|TestTemplate)\b|\bextends\s+TestCase\b")
# Import prefix -> framework, most specific first.
JAVA_FRAMEWORK_IMPORTS = (
    ("org.junit.jupiter", "JUnit Jupiter"), ("org.junit.platform", "JUnit Jupiter"),
    ("org.junit.", "JUnit 4"), ("junit.framework", "JUnit 3"), ("org.testng", "TestNG"),
)
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)", re.MULTILINE)
ANDROID_INSTRUMENTED = "src/androidTest/"


def _java(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    manifests = {hit.manifest.parent: hit for hit in hits}
    java_files = [path for path in files if path.suffix == ".java"]
    suites = []
    for root, tool in java_build_roots(hit.manifest for hit in hits):
        hit = manifests[root]
        suite = _suite("Java", root, "Maven" if tool == "maven" else "Gradle", hit.manifest)
        tests, instrumented = [], []
        for path in _inside(java_files, root):
            text = _read(project / path)
            frameworks = _java_frameworks(text)
            if not frameworks or not _JAVA_TEST_ANNOTATION.search(text):
                continue
            for name in frameworks:
                _add(suite.frameworks, name)
            tests.append(path)
            if ANDROID_INSTRUMENTED in f"/{path.as_posix()}":
                instrumented.append(path)
        suite.test_files = _posix(tests)
        if tests:
            suite.evidence.append(f"{len(tests)} test classes with JUnit or TestNG tests")
        if instrumented:
            suite.notes.append(f"{len(instrumented)} of them are Android instrumented tests (src/androidTest), "
                               "which need a device or an emulator")
        suite.tests_found = bool(tests)
        suites.append(suite)
    return suites, None


def _java_frameworks(text: str) -> list[str]:
    found: list[str] = []
    for imported in _JAVA_IMPORT.findall(text):
        for prefix, name in JAVA_FRAMEWORK_IMPORTS:
            if imported.startswith(prefix):
                _add(found, name)
                break
    return found


# --- C# ----------------------------------------------------------------------------

# NuGet package -> framework. Microsoft.NET.Test.Sdk makes a project a test project.
DOTNET_FRAMEWORK_PACKAGES = {
    "xunit": "xUnit", "xunit.v3": "xUnit", "xunit.core": "xUnit", "nunit": "NUnit",
    "mstest": "MSTest", "mstest.testframework": "MSTest", "tunit": "TUnit",
}
_PACKAGE_REFERENCE = re.compile(r"<PackageReference\b[^>]*\bInclude\s*=\s*\"([^\"]+)\"", re.IGNORECASE)
_IS_TEST_PROJECT = re.compile(r"<IsTestProject>\s*(true|false)\s*</IsTestProject>", re.IGNORECASE)
_MSTEST_SDK = re.compile(r"\bSdk\s*=\s*\"MSTest\.Sdk\b|<Sdk\b[^>]*\bName\s*=\s*\"MSTest\.Sdk\b", re.IGNORECASE)
_CS_TEST_ATTRIBUTE = re.compile(
    r"\[\s*(?:Fact|Theory|Test|TestCase|TestCaseSource|TestMethod|DataTestMethod|TestClass|TestFixture)\s*[\](,]")


def _csharp(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    projects = sorted((path for path in files if path.suffix == ".csproj"), key=_depth_first)
    cs_files = [path for path in files if path.suffix == ".cs"]
    folders = {path.parent: path for path in projects}
    suites = []
    for csproj in projects:
        text = _read(project / csproj)
        flag = _IS_TEST_PROJECT.search(text)
        if flag and flag.group(1).lower() == "false":
            continue
        frameworks: list[str] = []
        evidence = [f"<IsTestProject>true in {csproj.as_posix()}"] if flag else []
        for name in _PACKAGE_REFERENCE.findall(text):
            if name.lower() == "microsoft.net.test.sdk" or name.lower() in DOTNET_FRAMEWORK_PACKAGES:
                evidence.append(f"{name} in {csproj.as_posix()}")
            if name.lower() in DOTNET_FRAMEWORK_PACKAGES:
                _add(frameworks, DOTNET_FRAMEWORK_PACKAGES[name.lower()])
        if _MSTEST_SDK.search(text):
            evidence.append(f"MSTest.Sdk in {csproj.as_posix()}")
            _add(frameworks, "MSTest")
        if not evidence:
            continue
        suite = _suite("C#", csproj.parent, "NuGet", csproj)
        suite.frameworks = frameworks
        suite.evidence = evidence
        own = [path for path in _inside(cs_files, csproj.parent) if _owner(path, folders) == csproj.parent]
        tests = [path for path in own if _CS_TEST_ATTRIBUTE.search(_read(project / path))]
        suite.test_files = _posix(tests)
        if tests:
            suite.evidence.append(f"{len(tests)} files with test methods ([Fact], [Test], [TestMethod], ...)")
        else:
            suite.notes.append("test project without test methods")
        suite.tests_found = bool(tests)
        suites.append(suite)
    if not suites:  # no test project: one entry for the report
        first = hits[0]
        suites.append(_suite("C#", first.manifest.parent, first.manager, first.manifest,
                             notes=["no test project (Microsoft.NET.Test.Sdk, xUnit, NUnit, MSTest, TUnit)"]))
    return suites, None


# --- Rust --------------------------------------------------------------------------

# #[test], #[tokio::test], #[rstest], #[test_case(...)], ... but not #[cfg(test)].
_RUST_TEST_ATTRIBUTE = re.compile(r"#\[\s*(?:[A-Za-z_]\w*::)*(?:test|rstest|test_case|quickcheck)\b")
_RUST_DOC_LINE = re.compile(r"^\s*(?:///|//!)\s?(.*)$")
# Attributes rustdoc accepts on a Rust code block; any other word marks another language.
RUSTDOC_ATTRIBUTES = ("rust", "ignore", "should_panic", "no_run", "compile_fail", "test_harness",
                      "standalone_crate", "edition")
_RUST_LIB_SECTION = re.compile(r"^\s*\[lib\]", re.MULTILINE)


def _rust(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    crates = {hit.manifest.parent: hit for hit in hits}
    rust_files = [path for path in files if path.suffix == ".rs"]
    suites = []
    for root in _top_most(crates):
        hit = crates[root]
        suite = _suite("Rust", root, hit.manager, hit.manifest)
        integration, inline, doctests = [], [], []
        for path in _inside(rust_files, root):
            text = _read(project / path)
            crate = _owner(path, crates)
            in_tests = crate is not None and path.relative_to(crate).parts[0] == "tests"
            if _RUST_TEST_ATTRIBUTE.search(text):
                (integration if in_tests else inline).append(path)
            elif crate is not None and not in_tests and _has_doc_example(text) and _is_library(project, crate):
                doctests.append(path)
        suite.test_files = _posix(integration)
        suite.inline_test_files = _posix(inline)
        if inline or integration:
            suite.frameworks.append("libtest")
        if doctests:
            suite.frameworks.append("doctests")
        if inline:
            suite.evidence.append(f"{len(inline)} source files with test functions (#[test])")
        if integration:
            suite.evidence.append(f"{len(integration)} integration test files (tests/)")
        if doctests:
            suite.evidence.append(f"{len(doctests)} more source files with code examples in doc comments (doctests)")
        suite.tests_found = bool(integration or inline or doctests)
        suites.append(suite)
    return suites, None


def _has_doc_example(text: str) -> bool:
    """A Rust code block in a doc comment: cargo test runs it as a doctest (library crates only)."""
    fence: str | None = None
    for line in text.splitlines():
        match = _RUST_DOC_LINE.match(line)
        if match is None:
            fence = None
            continue
        content = match.group(1).strip()
        if not content.startswith(("```", "~~~")):
            continue
        if fence is not None:
            if content.startswith(fence):
                fence = None
            continue
        fence = content[:3]
        words = [word for word in re.split(r"[\s,]+", content.lstrip("`~")) if word]
        if all(word.startswith(RUSTDOC_ATTRIBUTES) for word in words):
            return True
    return False


def _is_library(project: Path, crate: Path) -> bool:
    if (project / crate / "src" / "lib.rs").is_file():
        return True
    return bool(_RUST_LIB_SECTION.search(_read(project / crate / "Cargo.toml")))


# --- C and C++ ---------------------------------------------------------------------

C_SUFFIXES = (".c", ".h", ".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".ipp")
_CMAKE_TEST = re.compile(
    r"^\s*(add_test|gtest_discover_tests|gtest_add_tests|catch_discover_tests|doctest_discover_tests)\s*\(",
    re.MULTILINE | re.IGNORECASE)
_CMAKE_ENABLE = re.compile(r"^\s*(?:enable_testing\s*\(|include\s*\(\s*CTest\s*\))", re.MULTILINE | re.IGNORECASE)
# Header -> framework.
C_FRAMEWORK_HEADERS = (
    ("gtest/", "GoogleTest"), ("gmock/", "GoogleTest"), ("catch2/", "Catch2"), ("catch.hpp", "Catch2"),
    ("doctest/", "doctest"), ("doctest.h", "doctest"), ("boost/test/", "Boost.Test"), ("CppUTest/", "CppUTest"),
    ("cmocka.h", "cmocka"), ("unity.h", "Unity"),
)
_C_INCLUDE = re.compile(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", re.MULTILINE)
# Package names in conanfile.txt/.py and vcpkg.json -> framework.
C_FRAMEWORK_PACKAGES = (
    (re.compile(r"\bgtest\b|\bgoogletest\b", re.IGNORECASE), "GoogleTest"),
    (re.compile(r"\bcatch2\b", re.IGNORECASE), "Catch2"), (re.compile(r"\bdoctest\b", re.IGNORECASE), "doctest"),
    (re.compile(r"\bboost-test\b", re.IGNORECASE), "Boost.Test"), (re.compile(r"\bcpputest\b", re.IGNORECASE), "CppUTest"),
    (re.compile(r"\bcmocka\b", re.IGNORECASE), "cmocka"),
)
C_TEST_FOLDERS = ("test", "tests", "unittest", "unittests")
NO_CMAKE = "no CMakeLists.txt in the project folder: only CMake projects (CTest) are supported"


def _cpp(project: Path, files: list[Path], hits: list[PackageManagerHit]) -> tuple[list[Suite], str | None]:
    if not (project / "CMakeLists.txt").is_file():
        return [], NO_CMAKE
    suite = _suite("C/C++", Path("."), "CMake", Path("CMakeLists.txt"))
    for hit in hits:
        text = _read(project / hit.manifest)
        for pattern, name in C_FRAMEWORK_PACKAGES:
            if pattern.search(text):
                _add(suite.frameworks, name)
                suite.evidence.append(f"{name} in {hit.manifest.as_posix()}")
    registered, enabled = [], False
    for path in files:
        if path.name == "CMakeLists.txt" or path.suffix == ".cmake":
            text = _read(project / path)
            if _CMAKE_TEST.search(text):
                registered.append(path)
            enabled = enabled or bool(_CMAKE_ENABLE.search(text))
    tests = []
    for path in files:
        if path.suffix.lower() not in C_SUFFIXES:
            continue
        frameworks = [name for include in _C_INCLUDE.findall(_read(project / path))
                      for prefix, name in C_FRAMEWORK_HEADERS if include.startswith(prefix)]
        for name in frameworks:
            _add(suite.frameworks, name)
        if frameworks or any(part.lower() in C_TEST_FOLDERS for part in path.parts[:-1]):
            tests.append(path)
    suite.test_files = _posix(tests)
    if registered:
        suite.evidence.append(f"tests registered with CTest in {', '.join(_posix(registered[:5]))}"
                              + (f" and {len(registered) - 5} more" if len(registered) > 5 else ""))
        if not suite.frameworks:
            suite.frameworks.append("CTest")
        if not enabled:
            suite.notes.append("add_test() without enable_testing() or include(CTest): CTest finds no tests")
    elif tests:
        suite.notes.append("test files, but no tests registered with CTest (add_test, gtest_discover_tests, ...)")
    if tests:
        suite.evidence.append(f"{len(tests)} test files (test framework headers or test folders)")
    suite.tests_found = bool(registered) and enabled
    return [suite], None


DETECTORS: dict[str, Callable[[Path, list[Path], list[PackageManagerHit]], tuple[list[Suite], str | None]]] = {
    "Python": _python,
    "JavaScript/TypeScript": _javascript,
    "Java": _java,
    "C#": _csharp,
    "Rust": _rust,
    "C/C++": _cpp,
}


# --- helpers -----------------------------------------------------------------------


def _suite(language: str, root: Path, manager: str, manifest: Path, *, notes: Sequence[str] = ()) -> Suite:
    return Suite(language, root.as_posix(), manager, manifest.as_posix(), notes=list(notes))


def _read(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(MAX_READ_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _depth_first(path: Path) -> tuple[int, str]:
    return len(path.parts), path.as_posix()


def _top_most(folders: Iterable[Path]) -> list[Path]:
    """Folders that are not inside another of the folders."""
    kept: list[Path] = []
    for folder in sorted(set(folders), key=_depth_first):
        if not any(_within(folder, root) for root in kept):
            kept.append(folder)
    return kept


def _within(path: Path, root: Path) -> bool:
    return root == Path(".") or path == root or path.is_relative_to(root)


def _inside(paths: Iterable[Path], root: Path) -> list[Path]:
    return [path for path in paths if _within(path.parent, root)]


def _owner(path: Path, roots: Iterable[Path]) -> Path | None:
    """The deepest of the folders that contains the file."""
    owners = [root for root in roots if _within(path.parent, root)]
    return max(owners, key=lambda root: len(root.parts) if root != Path(".") else 0, default=None)


def _matches(path: Path, patterns: Iterable[str]) -> bool:
    """pytest-style matching: patterns without "/" against the file name, others against the path."""
    return any(fnmatchcase(path.as_posix() if "/" in pattern else path.name, pattern) for pattern in patterns)


def _posix(paths: Iterable[Path]) -> list[str]:
    return sorted(path.as_posix() for path in paths)


def _add(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)

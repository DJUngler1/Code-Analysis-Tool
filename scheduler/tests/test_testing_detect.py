"""Step 5.1 without Docker: finding the test suites of a project."""

import json

from conftest import commit_all, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.testing.detect import NO_CMAKE, NO_PACKAGE_MANAGER, detect_suites

pytestmark = requires_git


def suites_of(make_project, files):
    project = make_project(files)
    commit_all(project)
    acceptance = run_acceptance(project)
    assert acceptance.accepted, acceptance.message
    return detect_suites(acceptance)


def only(suites):
    assert len(suites) == 1, suites
    return suites[0]


# --- Python ---


def test_python_pytest_and_unittest(make_project):
    suites, not_checked = suites_of(make_project, {
        "pyproject.toml": "[project]\nname = 'x'\n\n[tool.pytest.ini_options]\naddopts = '-q'\n",
        "src/x/__init__.py": "def f():\n    return 1\n",
        "tests/conftest.py": "import pytest\n",
        "tests/test_f.py": "from x import f\n\n\ndef test_f():\n    assert f() == 1\n",
        "tests/test_helpers.py": "VALUE = 1\n",  # named like a test, but without tests
        "app/tests.py": ("import unittest\n\n\nclass AppTests(unittest.TestCase):\n"
                         "    def test_x(self):\n        self.assertTrue(True)\n"),
    })
    suite = only(suites)
    assert not_checked == {}
    assert (suite.language, suite.root, suite.manager, suite.manifest) == ("Python", ".", "pip", "pyproject.toml")
    assert suite.tests_found
    assert suite.frameworks == ["pytest", "unittest"]
    assert suite.test_files == ["app/tests.py", "tests/test_f.py"]
    assert "pytest configuration in pyproject.toml" in suite.evidence


def test_python_uses_python_files_of_the_pytest_configuration(make_project):
    suite = only(suites_of(make_project, {
        "requirements.txt": "requests\n",
        "requirements/dev.txt": "pytest==8.4.0\n",
        "pytest.ini": "[pytest]\npython_files = check_*.py\n",
        "app.py": "x = 1\n",
        "check_app.py": "def test_x():\n    assert True\n",
        "test_ignored.py": "def test_y():\n    assert True\n",  # not in python_files, no TestCase
    })[0])
    assert suite.root == "."  # requirements/dev.txt belongs to the project folder
    assert suite.test_files == ["check_app.py"]
    assert suite.evidence[:2] == ["pytest configuration in pytest.ini", "pytest is a dependency (requirements/dev.txt)"]


def test_python_pytest_toml_comes_first(make_project):
    suite = only(suites_of(make_project, {
        "pyproject.toml": "[project]\nname = 'x'\n\n[tool.pytest.ini_options]\npython_files = 'x_*.py'\n",
        "pytest.toml": '[pytest]\npython_files = ["check_*.py"]\n',
        "app.py": "x = 1\n",
        "check_app.py": "def test_x():\n    assert True\n",
        "x_app.py": "def test_y():\n    assert True\n",
    })[0])
    assert suite.test_files == ["check_app.py"]
    assert suite.evidence[0] == "pytest configuration in pytest.toml"


def test_python_without_tests(make_project):
    suites, _ = suites_of(make_project, {"requirements.txt": "requests\n", "main.py": "print(1)\n"})
    suite = only(suites)
    assert not suite.tests_found and suite.test_files == [] and suite.evidence == []


# --- JavaScript and TypeScript ---


def test_javascript_framework_files_and_nested_packages(make_project):
    suites, _ = suites_of(make_project, {
        "package.json": json.dumps({"scripts": {"test": "vitest run"}, "devDependencies": {"vitest": "^3.0.0"}}),
        "package-lock.json": "{}",
        "src/a.ts": "export const a = 1;\n" * 5,
        "src/a.test.ts": "import { test, expect } from 'vitest';\ntest('a', () => expect(1).toBe(1));\n",
        "src/__tests__/b.ts": "describe.each([1])('b', () => {});\n",
        "test/fixtures/data.js": "module.exports = 1;\n",  # in test/, but no test calls
        "packages/b/package.json": json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        "packages/b/index.js": "module.exports = 2;\n",
    })
    root, nested = suites
    assert (root.language, root.manifest, root.manager) == ("JavaScript/TypeScript", "package.json", "npm")
    assert root.tests_found and root.frameworks == ["Vitest"]
    assert root.test_files == ["src/__tests__/b.ts", "src/a.test.ts"]
    assert root.evidence[:2] == ["test script: vitest run", "vitest in devDependencies"]
    assert nested.manifest == "packages/b/package.json"
    assert not nested.tests_found and nested.evidence == []  # npm's placeholder script is no test


def test_javascript_node_test_runner_and_browser_frameworks(make_project):
    suites, _ = suites_of(make_project, {
        "package.json": json.dumps({"scripts": {"test": "node --test && playwright test"},
                                    "devDependencies": {"@playwright/test": "1.50.0"}}),
        "lib.js": "module.exports = 1;\n",
        "test/lib.js": "const test = require('node:test');\ntest('lib', () => {});\n",
    })
    suite = only(suites)
    assert suite.frameworks == ["Playwright", "node:test"]
    assert suite.test_files == ["test/lib.js"]
    assert suite.notes == ["Playwright run(s) the tests in a browser"]


def test_javascript_test_script_without_framework(make_project):
    suite = only(suites_of(make_project, {
        "package.json": json.dumps({"scripts": {"test": "eslint ."}}),
        "index.js": "module.exports = 1;\n",
    })[0])
    assert not suite.tests_found
    assert suite.notes == ["the test script uses no known test framework, and no test files were found"]


# --- Java ---


JUNIT5 = "import org.junit.jupiter.api.Test;\n\nclass ATest {\n    @Test\n    void a() {}\n}\n"


def test_java_maven_build_with_modules(make_project):
    suite = only(suites_of(make_project, {
        "pom.xml": "<project/>",
        "core/pom.xml": "<project/>",
        "core/src/main/java/A.java": "class A {}\n" * 10,
        "core/src/test/java/ATest.java": JUNIT5,
        "core/src/test/java/Fixtures.java": "class Fixtures {}\n",  # test folder, but no tests
        "core/src/main/java/Helper.java": "import org.junit.jupiter.api.Assertions;\nclass Helper {}\n",
    })[0])
    assert (suite.root, suite.manager, suite.manifest) == (".", "Maven", "pom.xml")
    assert suite.frameworks == ["JUnit Jupiter"]
    assert suite.test_files == ["core/src/test/java/ATest.java"]


def test_java_gradle_custom_test_folder_and_android(make_project):
    suite = only(suites_of(make_project, {
        "settings.gradle": "include 'app'\n",
        "build.gradle": "sourceSets.test.java.srcDirs = ['test']\n",
        "src/Main.java": "class Main {}\n" * 10,
        "test/MainTest.java": "import org.junit.Test;\nimport static org.junit.Assert.*;\n"
                              "public class MainTest { @Test public void a() {} }\n",
        "app/src/androidTest/java/UiTest.java": "import org.junit.Test;\nclass UiTest { @Test public void a() {} }\n",
        "legacy/OldTest.java": "import junit.framework.TestCase;\npublic class OldTest extends TestCase {}\n",
    })[0])
    assert suite.manager == "Gradle"
    assert suite.frameworks == ["JUnit 4", "JUnit 3"]
    assert suite.test_files == ["app/src/androidTest/java/UiTest.java", "legacy/OldTest.java", "test/MainTest.java"]
    assert suite.notes == [("1 of them are Android instrumented tests (src/androidTest), which need a device "
                            "or an emulator")]


# --- C# ---


TEST_PROJECT = ('<Project Sdk="Microsoft.NET.Sdk"><ItemGroup>'
                '<PackageReference Include="Microsoft.NET.Test.Sdk" Version="18.0.0" />'
                '<PackageReference Include="xunit.v3" Version="3.1.0" /></ItemGroup></Project>')


def test_csharp_test_projects(make_project):
    suites, _ = suites_of(make_project, {
        "App.sln": "",
        "App/App.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>',
        "App/Program.cs": "class Program { static void Main() {} }\n" * 5,
        "App.Tests/App.Tests.csproj": TEST_PROJECT,
        "App.Tests/UnitTest1.cs": "public class UnitTest1 { [Fact] public void A() {} }\n",
        "App.Tests/Helpers.cs": "static class Helpers {}\n",
        "Lib.Tests/Lib.Tests.csproj": '<Project Sdk="MSTest.Sdk/3.8.0"></Project>',
        "Lib.Tests/Test1.cs": "[TestClass] public class Test1 { [TestMethod] public void A() {} }\n",
        "Off/Off.csproj": "<Project><PropertyGroup><IsTestProject>false</IsTestProject></PropertyGroup>"
                          '<ItemGroup><PackageReference Include="xunit" /></ItemGroup></Project>',
    })
    app, lib = suites
    assert (app.root, app.manifest, app.frameworks) == ("App.Tests", "App.Tests/App.Tests.csproj", ["xUnit"])
    assert app.test_files == ["App.Tests/UnitTest1.cs"]
    assert app.evidence[:2] == ["Microsoft.NET.Test.Sdk in App.Tests/App.Tests.csproj",
                                "xunit.v3 in App.Tests/App.Tests.csproj"]
    assert (lib.frameworks, lib.test_files) == (["MSTest"], ["Lib.Tests/Test1.cs"])


def test_csharp_without_test_project(make_project):
    suite = only(suites_of(make_project, {
        "App/App.csproj": '<Project Sdk="Microsoft.NET.Sdk"></Project>',
        "App/Program.cs": "class Program { static void Main() {} }\n",
    })[0])
    assert (suite.manifest, suite.tests_found) == ("App/App.csproj", False)
    assert suite.notes == ["no test project (Microsoft.NET.Test.Sdk, xUnit, NUnit, MSTest, TUnit)"]


# --- Rust ---


def test_rust_workspace_with_unit_integration_and_doc_tests(make_project):
    suite = only(suites_of(make_project, {
        "Cargo.toml": '[workspace]\nmembers = ["crates/*"]\n',
        "crates/a/Cargo.toml": '[package]\nname = "a"\n',
        "crates/a/src/lib.rs": ("pub fn a() -> i32 { 1 }\n\n#[cfg(test)]\nmod tests {\n"
                                "    #[test]\n    fn a() { assert_eq!(super::a(), 1); }\n}\n"),
        "crates/a/tests/it.rs": "#[tokio::test]\nasync fn it() {}\n",
        "crates/a/tests/common/mod.rs": "pub fn setup() {}\n",
        "crates/b/Cargo.toml": '[package]\nname = "b"\n',
        "crates/b/src/lib.rs": "/// ```\n/// assert_eq!(b::b(), 2);\n/// ```\npub fn b() -> i32 { 2 }\n",
        "crates/b/src/text.rs": "/// ```text\n/// not rust\n/// ```\npub fn c() {}\n",
        "crates/c/Cargo.toml": '[package]\nname = "c"\n',
        "crates/c/src/main.rs": "/// ```\n/// x\n/// ```\nfn main() {}\n",  # binary: no doctests
    })[0])
    assert (suite.root, suite.manifest) == (".", "Cargo.toml")
    assert suite.frameworks == ["libtest", "doctests"]
    assert suite.test_files == ["crates/a/tests/it.rs"]
    assert suite.inline_test_files == ["crates/a/src/lib.rs"]
    assert suite.evidence[-1] == "1 more source files with code examples in doc comments (doctests)"


# --- C and C++ ---


def test_cpp_ctest_with_googletest(make_project):
    suite = only(suites_of(make_project, {
        "conanfile.txt": "[requires]\ngtest/1.15.0\n",
        "CMakeLists.txt": "project(x)\ninclude(CTest)\nadd_subdirectory(tests)\n",
        "src/a.cpp": "int a() { return 1; }\n" * 10,
        "tests/CMakeLists.txt": "add_executable(t test_a.cpp)\ngtest_discover_tests(t)\n",
        "tests/test_a.cpp": "#include <gtest/gtest.h>\nTEST(A, a) { EXPECT_EQ(1, 1); }\n",
        "third_party/lib/CMakeLists.txt": "add_test(NAME vendored COMMAND x)\n",  # skipped folder
    })[0])
    assert (suite.language, suite.manager, suite.manifest) == ("C/C++", "CMake", "CMakeLists.txt")
    assert suite.tests_found and suite.frameworks == ["GoogleTest"]
    assert suite.test_files == ["tests/test_a.cpp"]
    assert "tests registered with CTest in tests/CMakeLists.txt" in suite.evidence


def test_cpp_add_test_without_enable_testing(make_project):
    suite = only(suites_of(make_project, {
        "vcpkg.json": '{"dependencies": []}',
        "CMakeLists.txt": "project(x)\nadd_executable(t t.c)\nadd_test(NAME t COMMAND t)\n",
        "t.c": "int main(void) { return 0; }\n",
    })[0])
    assert not suite.tests_found
    assert suite.frameworks == ["CTest"]
    assert suite.notes == ["add_test() without enable_testing() or include(CTest): CTest finds no tests"]


def test_cpp_without_cmake_is_not_checked(make_project):
    suites, not_checked = suites_of(make_project, {"conanfile.txt": "[requires]\n", "a.c": "int a;\n"})
    assert suites == [] and not_checked == {"C/C++": NO_CMAKE}


# --- languages and the step result ---


def test_language_without_package_manager_is_not_checked(make_project):
    suites, not_checked = suites_of(make_project, {
        "requirements.txt": "", "app.py": "x = 1\n" * 50, "static/app.js": "let x = 1;\n",
    })
    assert [suite.language for suite in suites] == ["Python"]
    assert not_checked == {"JavaScript/TypeScript": NO_PACKAGE_MANAGER}

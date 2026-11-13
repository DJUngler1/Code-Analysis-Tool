# Test analysis

This document defines step 5 of the workflow, "Tests" (see `docs/toolflow_en.png`). It has four parts, built one after the other:

1. **Find the tests** ("Tests present?"): done.
2. **Run the tests** ("Document results, including failures"): done.
3. Measure the test coverage: planned.
4. Mutation testing: planned.

## 1. Finding the tests

Step 5 starts by looking for the project's tests, without running anything:
- It reads only the files git knows.
- It skips the folders step 1 skips (e.g. `node_modules/`, `build/`, `target/`, `vendor/`, `third_party/` and hidden folders).
- The project is not changed.

### Test suites

A **test suite** is the set of tests of one build unit. The units come from the package managers step 1 found:

| Language | One suite per | Package manager or build tool |
|---|---|---|
| Python | Top-most folder with a Python manifest (`requirements/*.txt` counts for the folder above) | pip, Poetry, uv, PDM, Pipenv, Conda (from step 1) |
| JavaScript, TypeScript | `package.json` (one suite each; a file belongs to the nearest `package.json` above it) | npm, Yarn, pnpm, Bun (from step 1) |
| Java | Top-most Maven or Gradle build | Maven, Gradle |
| C# | Test project (`.csproj`, see below) | NuGet |
| Rust | Top-most `Cargo.toml` (the workspace) | Cargo |
| C, C++ | `CMakeLists.txt` in the project folder | CMake |

A language without a package manager is not checked. The same applies to C/C++ without a `CMakeLists.txt` in the project folder.

### How tests are recognised

Each language uses the default conventions of its test frameworks:

| Language | Test files | Frameworks |
|---|---|---|
| Python | Files matching pytest's `python_files` (default `test_*.py`, `*_test.py`) with test functions (`def test…`), plain `Test…` classes or `import pytest`; files matching `test*.py` (unittest's default) with `unittest.TestCase` classes that have `test…` methods | pytest, unittest |
| JavaScript, TypeScript | Files named `*.test.*`, `*.spec.*`, `*-test.*`, `*_test.*`, `test-*.*`, `test.*`, `spec.*`, `*.cy.*`, or in a `__tests__/` or `test/` folder, that call `describe(`, `it(`, `test(` or `suite(` | From the test script and the dependencies: Jest, Vitest, Mocha, AVA, Jasmine, Playwright, Cypress, Karma, tap, uvu, `node:test`, Bun test |
| Java | `.java` files that import JUnit or TestNG and have a test (`@Test`, `@ParameterizedTest`, `@RepeatedTest`, `@TestFactory`, `@TestTemplate`, or `extends TestCase`), in any folder | JUnit Jupiter, JUnit 4, JUnit 3, TestNG (from the imports) |
| C# | `.cs` files of a test project with `[Fact]`, `[Theory]`, `[Test]`, `[TestCase]`, `[TestMethod]`, `[TestClass]`, `[TestFixture]`, … | xUnit, NUnit, MSTest, TUnit (from the package references) |
| Rust | Integration tests: files in a crate's `tests/` folder with `#[test]` (also `#[tokio::test]` and similar, `#[rstest]`, `#[test_case]`). Unit tests: source files with such tests (usually `#[cfg(test)]` modules). Doctests: Rust code blocks in doc comments of library crates | libtest, doctests |
| C, C++ | Files that include a test framework header (`gtest/`, `gmock/`, `catch2/`, `catch.hpp`, `doctest`, `boost/test/`, `CppUTest/`, `cmocka.h`, `unity.h`), or files in a `test/`, `tests/`, `unittest/` or `unittests/` folder | GoogleTest, Catch2, doctest, Boost.Test, CppUTest, cmocka, Unity (from the headers and from `conanfile.txt`/`conanfile.py`/`vcpkg.json`); `CTest` if none |

**When a suite counts as tested:**

| Language | Tests present when |
|---|---|
| Python | At least one test file |
| JavaScript, TypeScript | At least one test file, or a test script that calls a known framework |
| Java | At least one test file |
| C# | At least one test file in a test project |
| Rust | At least one test, integration test or doctest |
| C, C++ | `add_test`, `gtest_discover_tests`, `gtest_add_tests`, `catch_discover_tests` or `doctest_discover_tests` in a `CMakeLists.txt` or `*.cmake` file, and testing enabled with `enable_testing()` or `include(CTest)` |

**Further rules:**
- **pytest configuration:** pytest's own order applies: `pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `pyproject.toml` (`[tool.pytest]` or `[tool.pytest.ini_options]`), `tox.ini` (`[pytest]`), `setup.cfg` (`[tool:pytest]`). The first match is used, and its `python_files` replace the defaults.
- **npm placeholder:** the test script `npm init` writes (`echo "Error: no test specified" && exit 1`) is not a test.
- **C# test project:** a `.csproj` with `<IsTestProject>true</IsTestProject>`, `Sdk="MSTest.Sdk"`, or a package reference to `Microsoft.NET.Test.Sdk`, xUnit, NUnit, MSTest or TUnit. `<IsTestProject>false</IsTestProject>` excludes it.
- **C/C++ without registration:** test files without tests registered with CTest are noted. CTest cannot run them.

**Notes for later steps:**
- JavaScript tests with Playwright, Cypress or Karma run in a browser.
- Java files in `src/androidTest/` are Android instrumented tests, which need a device or an emulator.

### Test files and source files

`test_files` contain only tests. Rust's unit tests and doctests live in source files; those files are listed separately (`inline_test_files`, doctests only in the evidence), because they are also source code. The coverage (part 3) will leave out only the `test_files`.

### Output

The tool prints one line per suite (frameworks and number of files with tests, or "no tests found"). Then it prints the languages without tests and those that were not checked.

## 2. Running the tests

The tests of every suite with tests run in a container of the language's test image. The images are built with the tool images (`docker compose -f docker/docker-compose.yml --profile tools build`).

| Language | Image | Contents |
|---|---|---|
| Python | `test-python` (`docker/tests/python/`) | Python 3.13.18 (full image with a C compiler), uv 0.13.7, Poetry 2.5.1, PDM 2.29.7, Pipenv 2026.11.0 |
| JavaScript, TypeScript | `test-node` | Node.js 22.23.4 (full image), npm, Yarn and pnpm via corepack, Bun 1.4.4; no browsers |
| Java | `test-java` | JDK 21, Maven 3.9.16, Gradle 9.9.1 |
| C# | `test-dotnet` | .NET SDK 10.0.403 with the .NET and ASP.NET Core runtimes 8.0.34 and 9.0.23 |
| Rust | `test-rust` | Rust 1.99.1 (full image with a C compiler and OpenSSL headers) |
| C, C++ | `test-cpp` | clang 22, CMake/CTest 3.31.6, Ninja, Conan 2.34.0, vcpkg 2026.10.16 |

### Two phases: download online, test offline

The tests execute the project's code. Each suite therefore runs in two phases:

1. **Download** (network): the package manager downloads the dependencies into a cache in the step folder. Where it can do that without running code of the project or its packages, it does.
2. **Build and test** (no network, `--network none`): the build and the tests use only what phase 1 downloaded.

| Language | Phase 1: download (online) | Phase 2: test (offline) | Report |
|---|---|---|---|
| Python | Install the project and its test dependencies into `.venv` in the copy: `uv sync --frozen --all-extras --all-groups` (uv), `poetry install --all-extras --all-groups` (Poetry), `pdm install -G :all` (PDM), `pipenv sync --dev` or `pipenv install --dev` (Pipenv); pip projects: `uv pip install` of the project (`-e .` with its extras `test`, `tests`, `testing`, `dev`), those dependency groups, `requirements.txt` and requirements files for tests or development. Then pytest 9.2.0, if the environment has no pytest. | `.venv/bin/python -m pytest --junitxml=… --continue-on-collection-errors -p no:cacheprovider` | JUnit XML |
| JavaScript, TypeScript | Install without lifecycle scripts: `npm ci --ignore-scripts` (`npm install` without a lock file), `yarn install --frozen-lockfile --ignore-scripts` (Yarn 1), `yarn install --immutable --mode=skip-build` (Yarn 2+), `pnpm install --frozen-lockfile --ignore-scripts`, `bun install --frozen-lockfile --ignore-scripts`; at the folder with the lock file (workspace root) | The packages' install scripts (`npm rebuild`, `pnpm rebuild`, `yarn rebuild`, Yarn 1: `yarn install --offline`), then the framework, see below | Jest JSON, JUnit XML |
| Java (Maven) | `mvn -fae test -Dtest='*#codeAnalysisSelectsNoTest'`: compiles the project and starts Surefire with a filter that matches no test method, so Surefire downloads its test provider; no test is executed | `mvn -o -fae test -Dmaven.test.failure.ignore=true` | JUnit XML (`target/surefire-reports/`) |
| Java (Gradle) | An init script resolves the external dependencies of every configuration of every project and lists the `test` tasks of the Java projects | `gradle --offline --continue <test tasks>`; Android projects are left out (their unit tests need the Android SDK) | JUnit XML (`build/test-results/`) |
| C# | `dotnet restore <test project>` | `dotnet test <test project> --no-restore --logger "trx;LogFilePrefix=results"` | TRX |
| Rust | `cargo fetch` (runs no build scripts) | `cargo test --workspace --no-fail-fast --offline` | libtest's text output |
| C, C++ | Configure the CMake build in Debug: Conan (`conan install --build=missing -s build_type=Debug`) or vcpkg (its CMake toolchain) download and build the dependencies | `cmake --build`, then `ctest --output-junit … --output-on-failure` | JUnit XML |

Projects with a Maven or Gradle wrapper (`mvnw`, `gradlew`) use it. Package caches (Maven repository, Gradle home, NuGet, Cargo, npm/Yarn/pnpm/Bun, uv/Poetry/PDM/pip) live in `step5-tests/cache/` during the run and are deleted afterwards.

**Network for the tests:** off by default. `config/tests.toml` switches it on (`[tests] network = true`). The tests can then reach the internet and services in your local network: only do this if you trust the project's code. The tool then prints a warning. Phase 1 always has network.

**Time limit:** `[tests] time_limit_minutes` in `config/tests.toml` (default 60, `0` for no limit) applies to each test command. Phase 1 has a limit of 60 minutes.

### JavaScript and TypeScript frameworks

| Framework | Command (with the arguments of the test script if it is a single call of the framework, e.g. `vitest run --dir src`) |
|---|---|
| Vitest | `vitest run … --reporter=default --reporter=junit --outputFile.junit=<report>` |
| Jest | `jest … --ci --json --outputFile=<report>` |
| Mocha | `mocha … --reporter xunit --reporter-option output=<report>` |
| `node:test` | `node --test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination=<report> --test …` |
| Bun test | `bun test … --reporter=junit --reporter-outfile=<report>` |
| other test script | `npm test` (or `yarn test`, `pnpm test`, `bun run test`): only its exit code (`partial`) |
| Playwright, Cypress, Karma only | not run (`skipped`): the image has no browser |

`CI=true` is set for all of them (no watch mode).

### Two states: after step 2 and HEAD

The tests run on a copy of the project as step 2 left it (the main result, as in steps 3 and 4). If step 2 changed the project (`git status` is not empty), the same suites also run on a copy of the last commit (HEAD). The tool compares the results test by test. Tests that ended differently, or ran in only one state, are listed in the output and in `summary.json` (`differences_to_head`), with a note that they can also be flaky tests. If one state has no test results at all (e.g. the build failed), the status and the number of tests per outcome of both states are compared instead of listing every test. Without differences, the comparison is not mentioned.

The HEAD copy is written from a temporary git index (`read-tree`, `checkout-index`): the project's repository is not changed, and files marked `export-ignore` are included.

### Test results

Each test has an id that is the same in both states, its outcome (`passed`, `failed`, `error`, `skipped`), its duration if the report has one, and the shortened failure message.

| Report | Id |
|---|---|
| JUnit XML | `<classname>.<name>`; with Node.js's placeholder class name `test`: the names of the enclosing suites (describe blocks) and the test; with a file name as class name: `<file> > <name>` |
| TRX | `<namespace>.<class>.<test>` (with the parameters of a theory); with several target frameworks: `[net8.0]` etc. appended |
| Jest JSON | `<file> > <full name>` |
| libtest | `<crate> <target>::<test>`; doctests without the line number (formatting moves it) |

Python projects get `SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0`: the copy is a git repository without commits, so setuptools-scm cannot derive a version from it. Conda environments are not supported (`skipped`).

## Status

| Situation | Result |
|---|---|
| Tests found | Suite with `tests_found: true` |
| No tests in a language | Suite(s) with `tests_found: false`; the language is listed under `languages_without_tests` and printed ("noted in the report") |
| Language not checked | Listed under `languages_not_checked` with the reason (no package manager, or C/C++ without `CMakeLists.txt`) |

Status of a test run:

| Status | Meaning |
|---|---|
| `ok` | The tests ran and their report was read. Failing tests are a result, not a failure of the run |
| `partial` | Some tests ran, but not all (e.g. a module did not compile, or the time limit was reached), or only the exit code is known |
| `failed` | No test result: the build failed, the report is missing, or the time limit was reached before any result |
| `skipped` | Not run: Conda, or JavaScript tests that need a browser |

Step 5 is incomplete (exit code 8) if detection failed, `config/tests.toml` is invalid, Docker or a test image is missing, or a run is `failed`.

## Results

Step 5 writes into the run folder of step 2: `OUTPUT_DIR/<project>/<date_time>Z/step5-tests/`.

| Path | Content |
|---|---|
| `summary.json` | `detection`: the test suites (language, folder, package manager or build tool, manifest, `tests_found`, frameworks, `test_files`, `inline_test_files`, evidence, notes), `languages_without_tests`, `languages_not_checked`. `tests`: network and time limit, per run the tool, version, status, duration, number of tests per outcome, the failed tests, commands with exit codes, notes; `differences_to_head` only if the states differ |
| `tests.json` | Every test of the runs after step 2: suite, id, outcome, duration, message |
| `<nn>-<language>-<folder>/run/` | Logs of each command (`<label>.stdout.txt`, `<label>.stderr.txt`) and the raw reports of the run after step 2 |
| `<nn>-<language>-<folder>/run-head/` | The same for the run at HEAD (only if step 2 changed the project) |

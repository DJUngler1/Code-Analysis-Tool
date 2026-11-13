# Test analysis

This document defines step 5 of the workflow, "Tests" (see `docs/toolflow_en.png`). It has four parts, built one after the other:

1. **Find the tests** ("Tests present?"): done.
2. **Run the tests** ("Document results, including failures"): done.
3. **Measure the test coverage** ("no minimum coverage"): done.
4. **Mutation testing**: done.

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

## 3. Measuring the test coverage

Right after the tests of a suite (state after step 2), the coverage is measured in a run of its own, in the same copy (the dependencies and builds are already there). The instrumentation must not change the test results of part 2, and the run at HEAD has no coverage. It runs offline like the tests; only adding the coverage tool to the copy has network. A suite whose tests did not run is not measured (`skipped`).

One tool per language, the same for every project:

| Language | Tool | How | Lines | Branches |
|---|---|---|---|---|
| Python | coverage.py 7.17.3 (the project's own version if its environment has one) | `coverage run --rcfile=<own settings: branch = True> -m pytest -p no:cov …`, then `coverage xml`; the project's coverage settings are not used | yes | yes |
| JavaScript, TypeScript (Jest) | Jest's built-in coverage | `jest … --coverage --coverageReporters=cobertura --coverageDirectory=<out>` | yes | yes |
| JavaScript, TypeScript (Vitest) | `@vitest/coverage-v8` in the version of the project's Vitest (added to the copy) | `vitest run … --coverage.enabled=true --coverage.provider=v8 --coverage.reporter=cobertura --coverage.reportOnFailure=true` | yes | yes |
| JavaScript, TypeScript (Mocha, `node:test`, other test scripts) | c8 12.0.0 (V8 coverage) | `c8 --reporter=cobertura <test command>` | yes | yes |
| JavaScript, TypeScript (Bun) | – | not measured (`skipped`) | – | – |
| Java | JaCoCo 0.8.16 | Maven: `mvn -o org.jacoco:jacoco-maven-plugin:0.8.16:prepare-agent test org.jacoco:jacoco-maven-plugin:0.8.16:report`; Gradle: an init script applies the `jacoco` plugin (`toolVersion` 0.8.16, XML report, `ignoreFailures` for the tests), then `<test tasks> <jacocoTestReport tasks>` | yes | yes |
| C# | coverlet 10.1.0 (`coverlet.collector`, added to the copy unless the test project has it) | `dotnet test … --collect "XPlat Code Coverage"` with `DOTNET_ROLL_FORWARD=LatestMajor`; Microsoft.Testing.Platform mode: not measured | yes | yes |
| Rust | cargo-llvm-cov 0.9.1 (LLVM source-based coverage) | `cargo llvm-cov --workspace --ignore-run-fail --offline --cobertura` | yes | no (needs nightly); doctests not measured |
| C, C++ | gcovr 8.8 with llvm-cov 22 | a second CMake build (Debug) with `--coverage`, CTest, then `gcovr --gcov-executable "llvm-cov-22 gcov" --cobertura` | yes | yes |

Failing tests do not stop the measurement (`maven.test.failure.ignore`, `ignoreFailures`, `--ignore-run-fail`, `reportOnFailure`). There is no minimum coverage.

**C#:** coverlet's collector runs on the runtime of the .NET SDK and received no data from a test host on an older runtime (`net8.0`). The coverage run therefore uses the newest runtime of the image (`DOTNET_ROLL_FORWARD=LatestMajor`); the test run of part 2 uses the runtime the project targets.

### Which files count

The coverage of a suite counts the source files of its language below its folder, without:

- test code: the test files of part 1, files in test folders (`test/`, `tests/`, `__tests__/`, `spec/`, `testing/`, `unittest/`, `benches/`, `src/test/`, `androidTest/`, …) and `conftest.py`;
- generated files (step 1) and files of nested suites of the same language (e.g. a `package.json` further down);
- build scripts and tool configuration (`setup.py`, `noxfile.py`, `build.rs`, `*.config.js`, `.d.ts`), documentation and examples (`docs/`, `examples/`, …).

For C#, a suite is a test project: its source files are the C# files outside the test projects. Rust's source files with `#[cfg(test)]` modules count as source files, including those modules.

Files of the report outside this list (e.g. test files, files outside the project) are left out. Source files without any coverage data were not loaded by any test; they are listed as `not_loaded` (for C/C++ only `.c`/`.cpp` files, not headers). Their lines are not counted, because only the tools know which lines are executable. JaCoCo reports every class of a tested module, also classes no test ran (with 0 covered lines); for Java, `not_loaded` are therefore the files of modules without a coverage report.

Per file the result has the lines (executable lines and lines run by a test), the branches if the tool reports them, and the line numbers no test ran (`"6-8, 12"`). The rates in `summary.json` are covered / total over the files with data.

## 4. Mutation testing

After the coverage, the tests of each suite are tested with mutants (small changes to the source code): a test suite that does not fail for a mutant did not detect that change. It runs in the same copy (state after step 2), offline like the tests; adding the tool to the copy has network.

| Language | Tool | How the files are selected | How failing tests are left out |
|---|---|---|---|
| Python | mutmut 3.8.1 (added to `.venv`) | `source_paths` in `[tool.mutmut]` of the copy's `pyproject.toml` (replacing the project's own) | `pytest_add_cli_args_test_selection = ["--deselect", "<node id>", …]` |
| JavaScript, TypeScript | StrykerJS 10.1.1 with the runner of the framework (`jest`, `vitest`, `mocha`; otherwise the command runner with the test command), added to the copy | `mutate` in an own Stryker configuration file (the project's is not used) | not possible: the suite is `skipped` if a test failed |
| Java | PIT 1.30.2 (with `pitest-junit5-plugin` 1.2.3 for JUnit Jupiter). Maven: `pitest-maven` added to `<build><plugins>` of the copy's `pom.xml`, `mvn -o test-compile org.pitest:pitest-maven:1.30.2:mutationCoverage`; Gradle: `gradle-pitest-plugin` 1.19.0 from an init script, the `pitest` task of each Java project | `targetClasses` (the classes of the files and their nested classes), `targetTests=*` | `skipFailingTests` |
| C# | Stryker.NET 5.1.0, in the test project's folder; with several referenced projects the one with the most selected files (`--project`) | `--mutate **/<path>` | `--test-case-filter "FullyQualifiedName!=<test>&…"` |
| Rust | cargo-mutants 27.1.0 | `--file <path>` | `-- -- --skip <test>` (passed to the test binaries) |
| C, C++ | – | not supported (`skipped`) | – |

Every tool uses its default mutators.

### Which files are mutated

`[mutation]` in `config/tests.toml`, read at every run:

| Setting | Default | Meaning |
|---|---|---|
| `mode` | `"sample"` | `"sample"`: a random sample of the source files the tests cover (part 3: at least one line run by a test), drawn with a fixed seed, so the same project gives the same sample. `"full"`: every source file of the suite (as counted in part 3) |
| `sample_files` | `20` | Size of the sample per suite; a suite with fewer covered files mutates all of them |
| `seed` | `1` | Seed of the sample |
| `time_limit_minutes` | `60` | Time limit of each mutation run; `0` means no limit |

Without coverage data (part 3 failed), the sample is drawn from all source files of the suite. The selected files, whether they are a sample, and the number of candidates are in `summary.json`.

**Time limit:** when it is reached, the container is stopped and the run gets `partial` with the mutants evaluated so far: mutmut and cargo-mutants write their results while they run; PIT, StrykerJS and Stryker.NET only at the end, so they have none then (noted). The configured limit does not make step 5 incomplete.

### Results

Each mutant has its file, line, the tool's mutator, the change (the tool's description or the new code), and its state. The states follow the mutation-testing-elements schema of Stryker; the other tools are mapped to it:

| State | mutmut | PIT | cargo-mutants |
|---|---|---|---|
| `Killed` | killed | KILLED | CaughtMutant |
| `Survived` | survived | SURVIVED | MissedMutant |
| `NoCoverage` | no tests | NO_COVERAGE | – |
| `Timeout` | timeout | TIMED_OUT | Timeout |
| `CompileError` | caught by type check | NON_VIABLE | Unviable |
| `RuntimeError` | suspicious, segfault | MEMORY_ERROR, RUN_ERROR | – |
| `Ignored` | skipped | EQUIVALENT | – |
| `Pending` | not checked | NOT_STARTED, STARTED | – |

**Score:** detected / valid mutants, as Stryker defines it: (`Killed` + `Timeout`) / (`Killed` + `Timeout` + `Survived` + `NoCoverage`). Mutants that did not compile, ended with an error, were ignored or not run do not count. For `Survived` and `NoCoverage` mutants, the source line is stored too (for step 6). mutmut gives the line only for those: its line of killed mutants is empty.

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

Status of a coverage run: `ok` (report read), `partial` (time limit reached), `failed` (no report), `skipped` (the tests did not run, or no tool for the framework).

Status of a mutation run: `ok` (report read), `partial` (time limit reached), `failed` (the tool wrote no report), `skipped` (the tests did not run, StrykerJS with failing tests, C/C++).

Step 5 is incomplete (exit code 8) if detection failed, `config/tests.toml` is invalid, Docker or a test image is missing, or a test, coverage or mutation run is `failed`.

## Results

Step 5 writes into the run folder of step 2: `OUTPUT_DIR/<project>/<date_time>Z/step5-tests/`.

| Path | Content |
|---|---|
| `summary.json` | `detection`: the test suites (language, folder, package manager or build tool, manifest, `tests_found`, frameworks, `test_files`, `inline_test_files`, evidence, notes), `languages_without_tests`, `languages_not_checked`. `tests`: network and time limit, per run the tool, version, status, duration, number of tests per outcome, the failed tests, commands with exit codes, notes; `differences_to_head` only if the states differ. `coverage`: per suite the tool, version, status, files, files not loaded, lines and branches (total, covered, rate), commands, notes. `mutation`: the settings, and per suite the tool, version, status, mutated files, sample yes/no, candidates, excluded tests, mutants per state, score, commands, notes |
| `tests.json` | Every test of the runs after step 2: suite, id, outcome, duration, message |
| `coverage.json` | Per suite: each source file with lines, covered lines, branches, covered branches, uncovered lines; the source files not loaded by any test |
| `mutants.json` | Every mutant: suite, file, line, mutator, change, state, source line (undetected mutants) |
| `<nn>-<language>-<folder>/run/` | Logs of each command (`<label>.stdout.txt`, `<label>.stderr.txt`) and the raw reports of the run after step 2 |
| `<nn>-<language>-<folder>/run-head/` | The same for the run at HEAD (only if step 2 changed the project) |
| `<nn>-<language>-<folder>/coverage/` | Logs and the raw coverage report(s) |
| `<nn>-<language>-<folder>/mutation/` | Logs, the settings written for the tool and its raw report |

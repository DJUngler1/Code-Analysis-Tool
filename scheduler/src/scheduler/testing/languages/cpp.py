"""C/C++: configure the CMake build (online: Conan or vcpkg download and build the dependencies),
then build and run CTest offline.

The build type is Debug: in Release builds, NDEBUG switches assert() off, and
tests that check with assert() would always pass.

Coverage: a second build with clang's --coverage (gcov format), CTest, then
gcovr reads the counters with llvm-cov 22 (`llvm-cov gcov`).
"""

from __future__ import annotations

from scheduler.linting.languages.cpp import cmake_configure_script
from scheduler.linting.session import slug
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
from scheduler.testing.model import CoverageRun, MutationRun, SuiteRun
from scheduler.testing.mutation import new_mutation, skip
from scheduler.testing.reports import ReportError, parse_junit

IMAGE = "code-analysis/test-cpp:latest"
BUILD_TYPE = "Debug"
# ctest: 0 all passed, 8 a test failed.
COMPLETED = (0, 8)
COVERAGE_FLAGS = ('-DCMAKE_C_FLAGS=--coverage -DCMAKE_CXX_FLAGS=--coverage -DCMAKE_EXE_LINKER_FLAGS=--coverage '
                  "-DCMAKE_SHARED_LINKER_FLAGS=--coverage")


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "CTest")
    run.version = ctx.runner.image_versions(IMAGE).get("ctest")
    manifests = [hit.manifest for language in ("C", "C++") for hit in ctx.acceptance.package_managers.get(language, ())]
    script, how = cmake_configure_script(job.workspace, manifests, build_type=BUILD_TYPE)
    build = ctx.step_dir / "build" / slug(f"{job.state}-{job.suite.root}")
    build.mkdir(parents=True, exist_ok=True)
    env = _env(ctx)
    configured = ctx.run(run, IMAGE, ["sh", "-c", script, "sh", str(job.workspace), str(build)],
                         workdir=job.workspace, log_dir=job.out_dir, label="configure", online=True, env=env)
    if not note_prepare(run, configured, f"configuring with {how}"):
        return judge(ctx, run, configured, [], completed=False, what="configuring the build")
    built = ctx.run(run, IMAGE, ["cmake", "--build", str(build)], workdir=job.workspace, log_dir=job.out_dir,
                    label="build", online=False, env=env)
    note_prepare(run, built, "the build")
    report = job.out_dir / "ctest-junit.xml"
    result = ctx.run(run, IMAGE, ["ctest", "--test-dir", str(build), "--output-junit", str(report),
                                  "--output-on-failure"],
                     workdir=job.workspace, log_dir=job.out_dir, label="ctest", online=False, env=env)
    tests = []
    if report.is_file():
        try:
            tests = parse_junit(report)
        except ReportError as exc:
            run.notes.append(f"the JUnit report could not be read: {exc}")
    return judge(ctx, run, result, tests,
                 completed=result.exit_code in COMPLETED and report.is_file() and built.exit_code == 0,
                 what="ctest")


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    coverage = new_coverage(ctx, job, "gcovr")
    coverage.version = ctx.runner.image_versions(IMAGE).get("gcovr")
    manifests = [hit.manifest for language in ("C", "C++") for hit in ctx.acceptance.package_managers.get(language, ())]
    script, how = cmake_configure_script(job.workspace, manifests, build_type=BUILD_TYPE, cmake_options=COVERAGE_FLAGS)
    build = ctx.step_dir / "build" / slug(f"coverage-{job.suite.root}")
    build.mkdir(parents=True, exist_ok=True)
    env = _env(ctx)
    configured = ctx.run(coverage, IMAGE, ["sh", "-c", script, "sh", str(job.workspace), str(build)],
                         workdir=job.workspace, log_dir=job.out_dir, label="configure", online=True, env=env)
    if not note_prepare(coverage, configured, f"configuring with {how} and --coverage"):
        return finish_coverage(ctx, coverage, job, None, configured, what="the coverage build")
    built = ctx.run(coverage, IMAGE, ["cmake", "--build", str(build)], workdir=job.workspace, log_dir=job.out_dir,
                    label="build", online=False, env=env)
    note_prepare(coverage, built, "the build")
    ctx.run(coverage, IMAGE, ["ctest", "--test-dir", str(build)], workdir=job.workspace, log_dir=job.out_dir,
            label="ctest", online=False, env=env)
    report = job.out_dir / "cobertura.xml"
    result = ctx.run(coverage, IMAGE, ["gcovr", "--root", str(job.workspace), "--gcov-executable", "llvm-cov-22 gcov",
                                       "--cobertura", str(report), str(build)],
                     workdir=job.workspace, log_dir=job.out_dir, label="gcovr", online=False, env=env)
    files = read_report(coverage, report, lambda path: parse_cobertura(path, job.workspace))
    return finish_coverage(ctx, coverage, job, files, result, what="gcovr")


def _env(ctx: TestContext) -> dict[str, str]:
    cache = ctx.cache_dir / "cpp"
    env = {"VCPKG_DOWNLOADS": str(cache / "vcpkg-downloads"), "VCPKG_DEFAULT_BINARY_CACHE": str(cache / "vcpkg-binary")}
    for folder in env.values():
        (ctx.step_dir / folder).mkdir(parents=True, exist_ok=True)
    return env


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    """Not supported for C/C++ (decision of 2026-11-13)."""
    return skip(new_mutation(ctx, job, "-"), "mutation testing is not supported for C/C++")

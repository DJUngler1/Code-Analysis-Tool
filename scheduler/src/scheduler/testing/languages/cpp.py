"""C/C++: configure the CMake build (online: Conan or vcpkg download and build the dependencies),
then build and run CTest offline.

The build type is Debug: in Release builds, NDEBUG switches assert() off, and
tests that check with assert() would always pass.
"""

from __future__ import annotations

from scheduler.linting.languages.cpp import cmake_configure_script
from scheduler.linting.session import slug
from scheduler.testing.context import Job, TestContext, judge, new_run, note_prepare
from scheduler.testing.model import SuiteRun
from scheduler.testing.reports import ReportError, parse_junit

IMAGE = "code-analysis/test-cpp:latest"
BUILD_TYPE = "Debug"
# ctest: 0 all passed, 8 a test failed.
COMPLETED = (0, 8)


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "CTest")
    run.version = ctx.runner.image_versions(IMAGE).get("ctest")
    manifests = [hit.manifest for language in ("C", "C++") for hit in ctx.acceptance.package_managers.get(language, ())]
    script, how = cmake_configure_script(job.workspace, manifests, build_type=BUILD_TYPE)
    build = ctx.step_dir / "build" / slug(f"{job.state}-{job.suite.root}")
    build.mkdir(parents=True, exist_ok=True)
    cache = ctx.cache_dir / "cpp"
    env = {"VCPKG_DOWNLOADS": str(cache / "vcpkg-downloads"), "VCPKG_DEFAULT_BINARY_CACHE": str(cache / "vcpkg-binary")}
    for folder in env.values():
        (ctx.step_dir / folder).mkdir(parents=True, exist_ok=True)
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

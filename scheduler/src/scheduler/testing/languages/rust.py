"""Rust: `cargo fetch` (online, runs no build scripts), then `cargo test` offline."""

from __future__ import annotations

from scheduler.testing.context import Job, TestContext, judge, new_run, note_prepare
from scheduler.testing.model import SuiteRun
from scheduler.testing.reports import libtest_ran, parse_libtest

IMAGE = "code-analysis/test-rust:latest"
# cargo test: 0 all passed, 101 tests failed (or the build failed).
COMPLETED = (0, 101)
BUILD_FAILED = "error: could not compile"


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "cargo test")
    run.version = ctx.runner.image_versions(IMAGE).get("cargo")
    env = {"CARGO_HOME": str(ctx.cache_dir / "cargo"), "CARGO_TERM_COLOR": "never"}
    fetched = ctx.run(run, IMAGE, ["cargo", "fetch"], workdir=job.folder, log_dir=job.out_dir, label="fetch",
                      online=True, env=env)
    note_prepare(run, fetched, "cargo fetch")
    # Cargo's progress (stderr) and the test output (stdout) in one stream, in
    # order: the "Running <target>" lines name the target of each test.
    result = ctx.run(run, IMAGE, ["sh", "-c", "cargo test --workspace --no-fail-fast --offline 2>&1"],
                     workdir=job.folder, log_dir=job.out_dir, label="cargo-test", online=False, env=env)
    tests = parse_libtest(result.stdout)
    build_failed = BUILD_FAILED in result.stdout
    if build_failed:
        run.notes.append("a test target did not compile")
    return judge(ctx, run, result, tests,
                 completed=result.exit_code in COMPLETED and libtest_ran(result.stdout) and not build_failed,
                 what="cargo test")

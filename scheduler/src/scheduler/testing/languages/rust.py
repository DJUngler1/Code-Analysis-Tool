"""Rust: `cargo fetch` (online, runs no build scripts), then `cargo test` offline.

Coverage: cargo-llvm-cov (LLVM source-based coverage) runs the tests again,
instrumented. Doctests and branch coverage need a nightly toolchain and are
not measured.

Mutation testing: cargo-mutants on the selected files (--file); tests that
failed in 5.2 are skipped (libtest's --skip).
"""

from __future__ import annotations

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
from scheduler.testing.mutation import finish_mutation, new_mutation, parse_cargo_mutants
from scheduler.testing.reports import ReportError, libtest_ran, parse_libtest

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


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    coverage = new_coverage(ctx, job, "cargo-llvm-cov")
    coverage.version = ctx.runner.image_versions(IMAGE).get("cargo-llvm-cov")
    env = {"CARGO_HOME": str(ctx.cache_dir / "cargo"), "CARGO_TERM_COLOR": "never"}
    report = job.out_dir / "cobertura.xml"
    # --ignore-run-fail: a report also when tests fail.
    result = ctx.run(coverage, IMAGE, ["cargo", "llvm-cov", "--workspace", "--ignore-run-fail", "--offline",
                                       "--cobertura", "--output-path", str(report)],
                     workdir=job.folder, log_dir=job.out_dir, label="llvm-cov", online=False, env=env)
    files = read_report(coverage, report, lambda path: parse_cobertura(path, job.workspace))
    return finish_coverage(ctx, coverage, job, files, result, what="cargo llvm-cov")


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    mutation = new_mutation(ctx, job, "cargo-mutants")
    mutation.version = ctx.runner.image_versions(IMAGE).get("cargo-mutants")
    env = {"CARGO_HOME": str(ctx.cache_dir / "cargo"), "CARGO_TERM_COLOR": "never", "CARGO_NET_OFFLINE": "true"}
    root = job.suite.root
    command = ["cargo", "mutants", "--workspace", "--no-shuffle", "--output", str(job.out_dir)]
    for file in files:
        command += ["--file", file if root == "." else file.removeprefix(f"{root}/")]
    skips = [name for test in failing if (name := test.partition("::")[2])]
    if skips:
        # After "--": arguments of cargo test; after a second "--": arguments of the test binaries.
        command += ["--", "--", *[argument for name in skips for argument in ("--skip", name)]]
        mutation.excluded_tests = skips
    result = ctx.run(mutation, IMAGE, command, workdir=job.folder, log_dir=job.out_dir, label="cargo-mutants",
                     online=False, env=env, timeout="mutation")
    report = job.out_dir / "mutants.out" / "outcomes.json"
    mutants = None
    if report.is_file():
        try:
            mutants = parse_cargo_mutants(report, job.folder, job.workspace)
        except ReportError as exc:
            mutation.notes.append(f"outcomes.json could not be read: {exc}")
    if result.exit_code == 4:  # cargo-mutants: the baseline (unmutated) tests failed
        mutation.notes.append("the tests failed without mutants (cargo-mutants' baseline)")
        mutants = None
    return finish_mutation(ctx, mutation, job, mutants, result, what="cargo-mutants")

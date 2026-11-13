"""Java: Maven or Gradle (the project's wrapper if it has one).

Online phase:
- Maven: a test run whose filter matches every test class but no test method,
  so that Maven downloads everything the test phase needs, including the
  Surefire provider it only resolves when it starts the tests. It compiles the
  project; no test is executed.
- Gradle: an init script resolves the external dependencies of every
  configuration of every project. Gradle runs the build scripts for it. The
  offline run starts the `test` tasks of the Java projects the script lists
  (not those of Android projects, which need the Android SDK).
The tests then run offline (`mvn -o`, `gradle --offline`).
"""

from __future__ import annotations

import re
from pathlib import Path

from scheduler.testing.context import Job, TestContext, judge, new_run, note_prepare
from scheduler.testing.model import SuiteRun, TestCase
from scheduler.testing.reports import ReportError, parse_junit

IMAGE = "code-analysis/test-java:latest"
NO_TEST = "*#codeAnalysisSelectsNoTest"  # every class, no method
GRADLE_MARKER = "code-analysis-not-resolved: "  # printed by the init script, one line per configuration
GRADLE_TEST_TASK = "code-analysis-test-task: "  # the test task of a Java project
GRADLE_ANDROID = "code-analysis-android: "  # a project with the Android Gradle plugin
GRADLE_INIT_SCRIPT = """\
// Code-Analysis-Tool: download the external dependencies of every resolvable
// configuration of every project, so that the tests can run offline. A
// configuration that cannot be resolved is reported; the others still are.
// Also lists the test tasks of the Java projects and the Android projects:
// Android unit tests need the Android SDK, and a test task Gradle cannot
// plan would stop the whole test run.
allprojects {
    tasks.register('codeAnalysisResolve') {
        notCompatibleWithConfigurationCache('resolves all configurations')
        doLast {
            configurations.findAll { it.canBeResolved }.each { configuration ->
                try {
                    configuration.incoming.artifactView {
                        lenient = true
                        componentFilter { it instanceof org.gradle.api.artifacts.component.ModuleComponentIdentifier }
                    }.files.files
                } catch (Exception e) {
                    logger.quiet("code-analysis-not-resolved: ${project.path} ${configuration.name}: "
                        + "${e.message?.readLines()?.first()}")
                }
            }
            if (project.plugins.hasPlugin('com.android.base')) {
                logger.quiet("code-analysis-android: ${project.path}")
            } else if (project.plugins.hasPlugin('java') && project.tasks.findByName('test') != null) {
                logger.quiet("code-analysis-test-task: ${project.tasks.getByName('test').path}")
            }
        }
    }
}
"""
_FAILED_TASK = re.compile(r"Execution failed for task '([^']+)'")
MAX_NOTES = 5


def run_suite(ctx: TestContext, job: Job) -> SuiteRun:
    if job.suite.manager == "Maven":
        return _maven(ctx, job)
    return _gradle(ctx, job)


def _maven(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "Maven (Surefire)")
    folder = job.folder
    launcher = ["sh", "./mvnw"] if (folder / "mvnw").is_file() else ["mvn"]
    run.version = ctx.runner.image_versions(IMAGE).get("maven") if launcher == ["mvn"] else "Maven wrapper (mvnw)"
    common = ["-B", "-Dstyle.color=never", f"-Dmaven.repo.local={ctx.cache_dir / 'm2'}"]
    env = {"MAVEN_USER_HOME": str(ctx.cache_dir / "maven-user-home")}  # the wrapper's Maven distribution
    prepared = ctx.run(run, IMAGE, [*launcher, *common, "-fae", "test", f"-Dtest={NO_TEST}",
                                    "-Dsurefire.failIfNoSpecifiedTests=false", "-DfailIfNoTests=false"],
                       workdir=folder, log_dir=job.out_dir, label="download", online=True, env=env)
    note_prepare(run, prepared, "downloading the dependencies with Maven")
    result = ctx.run(run, IMAGE, [*launcher, *common, "-o", "-fae", "test", "-Dmaven.test.failure.ignore=true"],
                     workdir=folder, log_dir=job.out_dir, label="test", online=False, env=env)
    tests = _reports(run, folder, "**/target/surefire-reports/TEST-*.xml", job.out_dir)
    # With maven.test.failure.ignore, failing tests do not fail the build: 0 means every module was tested.
    return judge(ctx, run, result, tests, completed=result.exit_code == 0, what="mvn test")


def _gradle(ctx: TestContext, job: Job) -> SuiteRun:
    run = new_run(ctx, job, "Gradle")
    folder = job.folder
    launcher = ["sh", "./gradlew"] if (folder / "gradlew").is_file() else ["gradle"]
    run.version = ctx.runner.image_versions(IMAGE).get("gradle") if launcher == ["gradle"] else \
        "Gradle wrapper (gradlew)"
    env = {"GRADLE_USER_HOME": str(ctx.cache_dir / "gradle-home")}
    script = job.out_dir / "code-analysis-resolve.gradle"
    script.write_text(GRADLE_INIT_SCRIPT, encoding="utf-8")
    prepared = ctx.run(run, IMAGE, [*launcher, "--no-daemon", "--console=plain", "--init-script", str(script),
                                    "codeAnalysisResolve"],
                       workdir=folder, log_dir=job.out_dir, label="download", online=True, env=env)
    note_prepare(run, prepared, "downloading the dependencies with Gradle")
    unresolved = _marked(prepared.stdout, GRADLE_MARKER)
    if unresolved:
        more = f" and {len(unresolved) - MAX_NOTES} more" if len(unresolved) > MAX_NOTES else ""
        run.notes.append(f"not downloaded: {'; '.join(unresolved[:MAX_NOTES])}{more}")
    tasks = _marked(prepared.stdout, GRADLE_TEST_TASK)
    android = _marked(prepared.stdout, GRADLE_ANDROID)
    if android:
        run.notes.append(f"Android projects not tested (their unit tests need the Android SDK): {', '.join(android)}")
    result = ctx.run(run, IMAGE, [*launcher, "--no-daemon", "--console=plain", "--offline", "--continue",
                                  *(tasks or ["test"])],
                     workdir=folder, log_dir=job.out_dir, label="test", online=False, env=env)
    tests = _reports(run, folder, "**/build/test-results/**/TEST-*.xml", job.out_dir)
    output = result.stdout + result.stderr
    # Gradle fails the build when a test fails; only other failures mean that tests are missing.
    other_failures = [task for task in _FAILED_TASK.findall(output) if not task.endswith(":test")]
    if other_failures:
        run.notes.append(f"failed tasks: {', '.join(other_failures[:MAX_NOTES])}")
    completed = result.exit_code == 0 or (bool(tests) and not other_failures and "There were failing tests" in output)
    return judge(ctx, run, result, tests, completed=completed, what="gradle test")


def _marked(output: str, marker: str) -> list[str]:
    return [line.split(marker, 1)[1].strip() for line in output.splitlines() if marker in line]


def _reports(run: SuiteRun, folder: Path, pattern: str, out_dir: Path) -> list[TestCase]:
    """Read the JUnit XML reports and keep a copy of them with the raw output."""
    tests: list[TestCase] = []
    copies = out_dir / "reports"
    for index, report in enumerate(sorted(folder.glob(pattern)), start=1):
        relative = report.relative_to(folder)
        try:
            tests += parse_junit(report)
        except ReportError as exc:
            run.notes.append(f"a test report could not be read: {exc}")
        target = copies / f"{index:04d}-{relative.name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(report.read_bytes())
    return tests

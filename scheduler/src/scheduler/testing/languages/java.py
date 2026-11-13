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

Coverage: JaCoCo, the same pinned version for Maven (its plugin, called by
coordinates on the command line) and Gradle (its jacoco plugin, applied by an
init script). Failing tests must not stop the report (testFailureIgnore,
ignoreFailures).

Mutation testing: PIT (with the JUnit 5 plugin for JUnit Jupiter tests).
Maven: the pitest-maven plugin is added to the copy's pom.xml, because its
JUnit 5 plugin can only be a plugin dependency and skipFailingTests has no
command-line property. Gradle: gradle-pitest-plugin from an init script.
skipFailingTests leaves out the tests that fail without mutants.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from scheduler.testing.context import Job, TestContext, finish_coverage, judge, new_coverage, new_run, note_prepare
from scheduler.testing.coverage_reports import parse_jacoco
from scheduler.testing.model import CoverageRun, FileCoverage, Mutant, MutationRun, SuiteRun, TestCase
from scheduler.testing.mutation import finish_mutation, new_mutation, parse_pit
from scheduler.testing.reports import ReportError, parse_junit
from scheduler.testing.sources import suite_sources

IMAGE = "code-analysis/test-java:latest"
JACOCO_VERSION = "0.8.16"
JACOCO_PLUGIN = f"org.jacoco:jacoco-maven-plugin:{JACOCO_VERSION}"
PITEST_VERSION = "1.30.2"
PITEST_JUNIT5_VERSION = "1.2.3"
GRADLE_PITEST_VERSION = "1.19.0"
PITEST_PLUGIN = f"org.pitest:pitest-maven:{PITEST_VERSION}"
POM = "http://maven.apache.org/POM/4.0.0"
_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
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
GRADLE_JACOCO_SCRIPT = """\
// Code-Analysis-Tool: measure the test coverage of every Java project with
// JaCoCo """ + JACOCO_VERSION + """ (XML report); failing tests must not stop the report.
// Every call names the project: in an init script, a bare apply() would go
// to the Gradle object.
allprojects { p ->
    p.plugins.withId('java') {
        p.pluginManager.apply('jacoco')
        p.jacoco.toolVersion = '""" + JACOCO_VERSION + """'
        p.tasks.withType(Test).configureEach { it.ignoreFailures = true }
        // The report task does not depend on the test task (Gradle's JaCoCo plugin): run it after the tests.
        p.tasks.matching { it.name == 'jacocoTestReport' }.configureEach { report ->
            report.mustRunAfter(p.tasks.withType(Test))
            report.reports.xml.required = true
            report.reports.html.required = false
            report.reports.csv.required = false
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


def coverage_suite(ctx: TestContext, job: Job) -> CoverageRun:
    coverage = new_coverage(ctx, job, "JaCoCo")
    coverage.version = f"JaCoCo {JACOCO_VERSION}"
    folder = job.folder
    if job.suite.manager == "Maven":
        launcher = ["sh", "./mvnw"] if (folder / "mvnw").is_file() else ["mvn"]
        common = ["-B", "-Dstyle.color=never", f"-Dmaven.repo.local={ctx.cache_dir / 'm2'}"]
        env = {"MAVEN_USER_HOME": str(ctx.cache_dir / "maven-user-home")}
        # The plugin's goal "help" downloads the plugin with its dependencies (agent, report).
        prepared = ctx.run(coverage, IMAGE, [*launcher, *common, f"{JACOCO_PLUGIN}:help"], workdir=folder,
                           log_dir=job.out_dir, label="download", online=True, env=env)
        note_prepare(coverage, prepared, "downloading JaCoCo")
        result = ctx.run(coverage, IMAGE, [*launcher, *common, "-o", "-fae", "-Dmaven.test.failure.ignore=true",
                                           f"{JACOCO_PLUGIN}:prepare-agent", "test", f"{JACOCO_PLUGIN}:report"],
                         workdir=folder, log_dir=job.out_dir, label="jacoco", online=False, env=env)
        pattern = "**/target/site/jacoco/jacoco.xml"
    else:
        launcher = ["sh", "./gradlew"] if (folder / "gradlew").is_file() else ["gradle"]
        env = {"GRADLE_USER_HOME": str(ctx.cache_dir / "gradle-home")}
        resolve = job.out_dir / "code-analysis-resolve.gradle"
        resolve.write_text(GRADLE_INIT_SCRIPT, encoding="utf-8")
        jacoco = job.out_dir / "code-analysis-jacoco.gradle"
        jacoco.write_text(GRADLE_JACOCO_SCRIPT, encoding="utf-8")
        scripts = ["--init-script", str(resolve), "--init-script", str(jacoco)]
        prepared = ctx.run(coverage, IMAGE, [*launcher, "--no-daemon", "--console=plain", *scripts,
                                             "codeAnalysisResolve"],
                           workdir=folder, log_dir=job.out_dir, label="download", online=True, env=env)
        note_prepare(coverage, prepared, "downloading JaCoCo with Gradle")
        tests = _marked(prepared.stdout, GRADLE_TEST_TASK) or ["test"]
        reports = [task.rsplit(":", 1)[0] + ":jacocoTestReport" if ":" in task else "jacocoTestReport"
                   for task in tests]
        result = ctx.run(coverage, IMAGE, [*launcher, "--no-daemon", "--console=plain", "--offline", "--continue",
                                           *scripts, *tests, *reports],
                         workdir=folder, log_dir=job.out_dir, label="jacoco", online=False, env=env)
        pattern = "**/build/reports/jacoco/test/jacocoTestReport.xml"
    java_files = suite_sources(ctx.acceptance, ctx.suites, job.suite)
    files: dict[str, FileCoverage] = {}
    found = sorted(folder.glob(pattern))
    for index, report in enumerate(found, start=1):
        try:
            files.update(parse_jacoco(report, java_files))
        except ReportError as exc:
            coverage.notes.append(f"a JaCoCo report could not be read: {exc}")
        target = job.out_dir / "reports" / f"{index:04d}-jacoco.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(report.read_bytes())
    if found and result.exit_code != 0:
        coverage.notes.append("some modules have no coverage report (see the log)")
    return finish_coverage(ctx, coverage, job, files if found else None, result, what="JaCoCo")


def mutation_suite(ctx: TestContext, job: Job, files: list[str], failing: list[str]) -> MutationRun:
    mutation = new_mutation(ctx, job, "PIT")
    mutation.version = f"PIT {PITEST_VERSION}"
    folder = job.folder
    classes = target_classes(job.workspace, files)
    junit5 = "JUnit Jupiter" in job.suite.frameworks
    if failing:  # skipFailingTests leaves them out
        mutation.excluded_tests = failing
    if "TestNG" in job.suite.frameworks:
        mutation.notes.append("TestNG tests need PIT's TestNG plugin, which the tool does not add")
    if job.suite.manager == "Maven":
        launcher = ["sh", "./mvnw"] if (folder / "mvnw").is_file() else ["mvn"]
        common = ["-B", "-Dstyle.color=never", f"-Dmaven.repo.local={ctx.cache_dir / 'm2'}"]
        env = {"MAVEN_USER_HOME": str(ctx.cache_dir / "maven-user-home")}
        add_pitest_plugin(folder / "pom.xml", junit5=junit5)
        prepared = ctx.run(mutation, IMAGE, [*launcher, *common, f"{PITEST_PLUGIN}:help"], workdir=folder,
                           log_dir=job.out_dir, label="download", online=True, env=env)
        note_prepare(mutation, prepared, "downloading PIT")
        result = ctx.run(mutation, IMAGE, [*launcher, *common, "-o", "-fae", "test-compile",
                                           f"{PITEST_PLUGIN}:mutationCoverage", f"-DtargetClasses={','.join(classes)}",
                                           "-DtargetTests=*", "-DoutputFormats=XML", "-DtimestampedReports=false",
                                           "-DfailWhenNoMutations=false"],
                         workdir=folder, log_dir=job.out_dir, label="pitest", online=False, env=env,
                         timeout="mutation")
        pattern = "**/target/pit-reports/mutations.xml"
    else:
        launcher = ["sh", "./gradlew"] if (folder / "gradlew").is_file() else ["gradle"]
        env = {"GRADLE_USER_HOME": str(ctx.cache_dir / "gradle-home")}
        resolve = job.out_dir / "code-analysis-resolve.gradle"
        resolve.write_text(GRADLE_INIT_SCRIPT, encoding="utf-8")
        pitest = job.out_dir / "code-analysis-pitest.gradle"
        pitest.write_text(gradle_pitest_script(classes, junit5=junit5), encoding="utf-8")
        scripts = ["--init-script", str(resolve), "--init-script", str(pitest)]
        prepared = ctx.run(mutation, IMAGE, [*launcher, "--no-daemon", "--console=plain", *scripts,
                                             "codeAnalysisResolve"],
                           workdir=folder, log_dir=job.out_dir, label="download", online=True, env=env)
        note_prepare(mutation, prepared, "downloading PIT with Gradle")
        tests = _marked(prepared.stdout, GRADLE_TEST_TASK) or [":test"]
        tasks = [task.rsplit(":", 1)[0] + ":pitest" for task in tests]
        result = ctx.run(mutation, IMAGE, [*launcher, "--no-daemon", "--console=plain", "--offline", "--continue",
                                           "--init-script", str(pitest), *tasks],
                         workdir=folder, log_dir=job.out_dir, label="pitest", online=False, env=env,
                         timeout="mutation")
        pattern = "**/build/reports/pitest/mutations.xml"
    java_files = suite_sources(ctx.acceptance, ctx.suites, job.suite)
    mutants: list[Mutant] = []
    found = sorted(folder.glob(pattern))
    for index, report in enumerate(found, start=1):
        try:
            mutants += parse_pit(report, java_files)
        except ReportError as exc:
            mutation.notes.append(f"a PIT report could not be read: {exc}")
        target = job.out_dir / "reports" / f"{index:04d}-mutations.xml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(report.read_bytes())
    if found and result.exit_code != 0 and not result.timed_out:
        mutation.notes.append("PIT failed for some modules (see the log)")
    return finish_mutation(ctx, mutation, job, mutants if found else None, result, what="PIT")


def target_classes(workspace: Path, files: list[str]) -> list[str]:
    """PIT's targetClasses for source files: "<package>.<Name>" and its nested classes "<package>.<Name>$*"."""
    classes = []
    for file in files:
        text = (workspace / file).read_text(encoding="utf-8", errors="replace") \
            if (workspace / file).is_file() else ""
        package = _PACKAGE.search(text)
        name = Path(file).stem
        qualified = f"{package.group(1)}.{name}" if package else name
        classes += [qualified, f"{qualified}$*"]
    return classes


def add_pitest_plugin(pom: Path, *, junit5: bool) -> None:
    """Add pitest-maven (skipFailingTests, the JUnit 5 plugin if needed) to <build><plugins> of the copy's pom."""
    ET.register_namespace("", POM)
    tree = ET.parse(pom)
    root = tree.getroot()
    ns = f"{{{POM}}}" if root.tag.startswith(f"{{{POM}}}") else ""
    build = root.find(f"{ns}build")
    if build is None:
        build = ET.SubElement(root, f"{ns}build")
    plugins = build.find(f"{ns}plugins")
    if plugins is None:
        plugins = ET.SubElement(build, f"{ns}plugins")
    for plugin in list(plugins):
        if plugin.findtext(f"{ns}artifactId") == "pitest-maven":
            plugins.remove(plugin)
    plugin = ET.SubElement(plugins, f"{ns}plugin")
    for tag, value in (("groupId", "org.pitest"), ("artifactId", "pitest-maven"), ("version", PITEST_VERSION)):
        ET.SubElement(plugin, f"{ns}{tag}").text = value
    configuration = ET.SubElement(plugin, f"{ns}configuration")
    ET.SubElement(configuration, f"{ns}skipFailingTests").text = "true"
    if junit5:
        dependency = ET.SubElement(ET.SubElement(plugin, f"{ns}dependencies"), f"{ns}dependency")
        for tag, value in (("groupId", "org.pitest"), ("artifactId", "pitest-junit5-plugin"),
                           ("version", PITEST_JUNIT5_VERSION)):
            ET.SubElement(dependency, f"{ns}{tag}").text = value
    tree.write(pom, encoding="utf-8", xml_declaration=True)


def gradle_pitest_script(classes: list[str], *, junit5: bool) -> str:
    targets = ", ".join(json.dumps(name).replace("$", "\\$") for name in classes)
    junit = f"\n            junit5PluginVersion = '{PITEST_JUNIT5_VERSION}'" if junit5 else ""
    return f"""\
// Code-Analysis-Tool: mutation testing with PIT {PITEST_VERSION} (gradle-pitest-plugin {GRADLE_PITEST_VERSION}).
initscript {{
    repositories {{ gradlePluginPortal() }}
    dependencies {{ classpath 'info.solidsoft.gradle.pitest:gradle-pitest-plugin:{GRADLE_PITEST_VERSION}' }}
}}
allprojects {{ p ->
    p.plugins.withId('java') {{
        p.pluginManager.apply(info.solidsoft.gradle.pitest.PitestPlugin)
        p.pitest {{
            pitestVersion = '{PITEST_VERSION}'{junit}
            targetClasses = [{targets}]
            targetTests = ['*']
            outputFormats = ['XML']
            timestampedReports = false
            failWhenNoMutations = false
            skipFailingTests = true
        }}
    }}
}}
"""


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

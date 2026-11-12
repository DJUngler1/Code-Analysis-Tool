"""Step 4 without Docker: parsers (real outputs in tests/fixtures/dependencies/), lock files, orchestration."""

import json
from datetime import datetime
from pathlib import Path

import pytest
from conftest import commit_all, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.cli import format_dependencies
from scheduler.config import Settings
from scheduler.dependencies import audits, lockfiles, osv, run_dependencies
from scheduler.dependencies.model import DependencyFinding, severity_from_cvss, severity_from_label
from scheduler.dependencies.run import scope_counts
from scheduler.linting.docker import DockerError
from scheduler.linting.model import CommandResult, Status

FIXTURES = Path(__file__).parent / "fixtures" / "dependencies"
NOW = datetime(2026, 11, 11, 12, 0, 0)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


# --- severity -----------------------------------------------------------------------


@pytest.mark.parametrize(("score", "expected"), [
    (None, "unknown"), (0.0, "unknown"), (0.1, "low"), (3.9, "low"), (4.0, "medium"), (6.9, "medium"),
    (7.0, "high"), (8.9, "high"), (9.0, "critical"), (10.0, "critical"),
])
def test_severity_from_cvss(score, expected):
    assert severity_from_cvss(score) == expected


def test_severity_from_label():
    assert [severity_from_label(label) for label in ("CRITICAL", "High", "moderate", "info", None, "?")] == [
        "critical", "high", "medium", "low", "unknown", "unknown"]


# --- OSV-Scanner -----------------------------------------------------------------------


def test_parse_osv():
    data = json.loads(fixture("osv-scanner.json"))
    findings = osv.parse_osv(data, workspace=Path("/projects/dep"), managers={"cargo/Cargo.lock": "Cargo"})
    by_manifest: dict[str, list] = {}
    for finding in findings:
        by_manifest.setdefault(finding.manifest, []).append(finding)
    # One finding per group: Django 2.0 has 35 records but 24 distinct vulnerabilities.
    django = [finding for finding in by_manifest["pip/requirements.txt"] if finding.package == "django"]
    assert len(django) == 24
    first = next(finding for finding in django if finding.id == "PYSEC-2018-2")
    assert {"CVE-2018-14574", "GHSA-5hg3-6c2f-f3wr"} <= set(first.aliases)
    assert (first.cvss_score, first.severity, first.ecosystem) == (6.1, "medium", "PyPI")
    assert "2.0.8" in first.fixed_versions and first.url == "https://osv.dev/vulnerability/PYSEC-2018-2"
    smallvec = by_manifest["cargo/Cargo.lock"][0]
    assert (smallvec.package, smallvec.version, smallvec.manager, smallvec.ecosystem) == (
        "smallvec", "1.6.0", "Cargo", "crates.io")
    # Maven: the transitive log4j-api comes from pom.xml too.
    assert {finding.package for finding in by_manifest["maven/pom.xml"]} == {
        "org.apache.logging.log4j:log4j-core", "org.apache.logging.log4j:log4j-api"}
    lodash = by_manifest["npm/package-lock.json"]
    assert all(finding.summary for finding in lodash)  # from the GitHub advisory of the group


# --- audits -----------------------------------------------------------------------------


def test_parse_npm():
    findings = audits.parse_npm(fixture("npm-audit.json"), "package.json")
    assert len(findings) == 5 and {finding.package for finding in findings} == {"lodash"}
    injection = next(finding for finding in findings if finding.id == "GHSA-35jh-r3h4-6jhm")
    assert (injection.severity, injection.cvss_score) == ("high", 7.2)
    assert injection.cvss_vector.startswith("CVSS:3.1/") and injection.summary == "Command Injection in lodash"


def test_parse_yarn_classic_and_berry():
    classic = audits.parse_yarn(fixture("yarn1-audit.ndjson"), "package.json")
    berry = audits.parse_yarn(fixture("yarn4-audit.ndjson"), "package.json")
    assert {finding.id for finding in classic} == {finding.id for finding in berry}
    assert len(classic) == 5 and all(finding.version == "4.17.15" for finding in classic + berry)
    assert any("CVE-2021-23337" in finding.aliases for finding in classic)


def test_parse_pnpm_and_bun():
    pnpm = audits.parse_pnpm(fixture("pnpm-audit.json"), "package.json")
    bun = audits.parse_bun(fixture("bun-audit.json"), "package.json")
    assert {finding.id for finding in pnpm} == {finding.id for finding in bun}
    assert len(pnpm) == 5 and pnpm[0].version == "4.17.15"


def test_parse_cargo():
    (finding,) = audits.parse_cargo(fixture("cargo-audit.json"), "Cargo.toml")
    assert (finding.id, finding.package, finding.version) == ("RUSTSEC-2021-0003", "smallvec", "1.6.0")
    assert ">=1.6.1" in finding.fixed_versions and finding.severity == "unknown"


def test_parse_dotnet():
    (finding,) = audits.parse_dotnet(fixture("dotnet-list-vulnerable.json"), "app.csproj")
    assert (finding.package, finding.version, finding.id, finding.severity) == (
        "Newtonsoft.Json", "12.0.1", "GHSA-5crp-9r3c-p9vr", "high")


def test_parse_pip_audit():
    findings = audits.parse_pip_audit(fixture("pip-audit.json"), "requirements.txt")
    assert len(findings) == 34 and {finding.package for finding in findings} == {"django"}
    assert all(finding.severity == "unknown" for finding in findings)  # pip-audit reports none


def test_parse_conan():
    # Structure of Conan's JSON formatter (conan/cli/formatters/audit/vulnerabilities.py).
    output = json.dumps({"data": {"zlib/1.2.11": {"vulnerabilities": {"edges": [{"node": {
        "name": "CVE-2018-25032", "severity": "High", "cvss": {"preferredBaseScore": 7.5},
        "description": "zlib before 1.2.12 allows memory corruption", "references": ["https://nvd.nist.gov/x"],
        "vulnerablePackages": {"edges": [{"node": {"fixVersions": [{"version": "1.2.12"}]}}]}}}]}}}})
    (finding,) = audits.parse_conan(output, "conanfile.txt")
    assert (finding.package, finding.version, finding.id, finding.severity, finding.fixed_versions) == (
        "zlib", "1.2.11", "CVE-2018-25032", "high", ("1.2.12",))


# --- lock files and orchestration ---------------------------------------------------------


class FakeRunner:
    """Docker: lock commands create their lock file; tools print the given outputs."""

    def __init__(self, outputs=None, fail=()):
        self.outputs = outputs or {}
        self.fail = set(fail)
        self.calls = []

    def check_available(self):
        pass

    def ensure_image(self, image):
        pass

    def image_versions(self, image):
        return {"osv-scanner": "2.7.1", "npm": "npm 10.9.9", "cargo": "cargo 1.99.1", "pip-audit": "pip-audit 2.10.1"}

    def run(self, image, command, *, workdir, mounts, env=None, timeout, read_only=(), network=None):
        command = list(command)
        text = " ".join(command)
        self.calls.append({"image": image, "command": command, "workdir": workdir, "mounts": mounts, "env": env})
        for key in self.fail:
            if key in text:
                return CommandResult(tuple(command), 2, "", f"{key} broke", 0.1)
        if "--package-lock-only" in text:
            (workdir / "package-lock.json").write_text("{}")
        elif "generate-lockfile" in text:
            (workdir / "Cargo.lock").write_text("# lock")
        elif command[:2] == ["osv-scanner", "scan"]:
            workspace = Path(command[-1])
            report = {"results": [{"source": {"path": str(workspace / "rs" / "Cargo.lock"), "type": "lockfile"},
                                   "packages": [{"package": {"name": "smallvec", "version": "1.6.0",
                                                             "ecosystem": "crates.io"},
                                                 "vulnerabilities": [{"id": "RUSTSEC-2021-0003", "affected": []},
                                                                     {"id": "GHSA-43w2-9j62-hq99",
                                                                      "summary": "Buffer overflow", "affected": []}],
                                                 "groups": [{"ids": ["RUSTSEC-2021-0003", "GHSA-43w2-9j62-hq99"],
                                                             "aliases": ["CVE-2021-25900"],
                                                             "max_severity": "9.8"}]}]}]}
            Path(command[command.index("--output-file") + 1]).write_text(json.dumps(report))
            return CommandResult(tuple(command), 1, "", "", 1.0)
        for key, output in self.outputs.items():
            if key in text:
                return CommandResult(tuple(command), 1, output, "", 0.5)
        return CommandResult(tuple(command), 0, "", "", 0.1)


def settings(tmp_path: Path, token: str | None = None) -> Settings:
    return Settings(projects_dir=None, host_home=None, ollama_base_url="x", output_dir=tmp_path / "results",
                    conan_audit_token=token)


def mixed_project(make_project) -> Path:
    project = make_project({
        "web/index.js": "module.exports = 1;\n",
        "web/package.json": '{"name": "x", "dependencies": {"lodash": "4.17.15"}}\n',
        "rs/src/main.rs": "fn main() {}\n",
        "rs/Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\n',
        "cpp/main.cpp": "int main() { return 0; }\n",
        "cpp/vcpkg.json": '{"dependencies": ["zlib"]}\n',
        "py/app.py": "import django\n",
        "py/requirements.txt": "django==2.0\n",
    })
    commit_all(project)
    return project


@requires_git
def test_run_dependencies(make_project, tmp_path):
    project = mixed_project(make_project)
    runner = FakeRunner(outputs={"npm audit": fixture("npm-audit.json"), "cargo audit": fixture("cargo-audit.json"),
                                 "pip-audit": fixture("pip-audit.json")})
    result = run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=runner,
                              now=lambda: NOW, out=lambda _: None)
    assert result.complete
    step = tmp_path / "run" / "step4-dependencies"

    coverage = {entry.manager: entry for entry in result.coverage}
    assert (coverage["npm"].lockfile, coverage["npm"].lockfile_state) == ("web/package-lock.json", "generated")
    assert (coverage["Cargo"].lockfile, coverage["Cargo"].lockfile_state) == ("rs/Cargo.lock", "generated")
    assert coverage["pip"].lockfile_state == "not needed"
    assert (coverage["vcpkg"].osv, coverage["vcpkg"].audit) == (False, None)
    assert (step / "lockfiles" / "web" / "package-lock.json").is_file()
    assert (step / "lockfiles" / "rs" / "Cargo.lock").is_file()

    # Every container works on the copy in the step folder, never on the project.
    assert all(call["mounts"] == [step] for call in runner.calls)
    assert all(project not in (call["workdir"], *call["workdir"].parents) for call in runner.calls)
    lock_npm = next(call for call in runner.calls if "--package-lock-only" in call["command"])
    assert lock_npm["workdir"] == step / "workspace" / "web" and "--ignore-scripts" in lock_npm["command"]

    tools = {(run.tool, run.manifest): run for run in result.tool_runs}
    assert tools[("osv-scanner", None)].status is Status.OK
    assert len(tools[("npm audit", "web/package.json")].findings) == 5
    cargo = tools[("cargo audit", "rs/Cargo.toml")].findings[0]
    # cargo-audit reports no severity: taken from OSV's finding of the same advisory.
    assert (cargo.severity, cargo.cvss_score, cargo.severity_source) == (
        "critical", 9.8, "osv-scanner (RUSTSEC-2021-0003)")
    assert {"CVE-2021-25900", "GHSA-43w2-9j62-hq99"} <= set(cargo.aliases)  # aliases from OSV
    osv_finding = tools[("osv-scanner", None)].findings[0]
    assert (osv_finding.manifest, osv_finding.manager) == ("rs/Cargo.lock", "Cargo")

    summary = json.loads((step / "summary.json").read_text())
    assert summary["complete"] is True and summary["findings_by_severity"]["critical"] >= 2
    assert {entry["manager"] for entry in summary["coverage"]} == {"npm", "Cargo", "pip", "vcpkg"}
    findings = json.loads((step / "findings.json").read_text())
    assert len(findings) == len(result.findings)
    assert not (step / "workspace").exists()
    status = __import__("subprocess").run(["git", "-C", str(project), "status", "--porcelain"],
                                          capture_output=True, text=True)
    assert status.stdout == ""


@requires_git
def test_failures_are_reported_per_tool(make_project, tmp_path):
    project = mixed_project(make_project)
    runner = FakeRunner(outputs={"npm audit": fixture("npm-audit.json")}, fail=("generate-lockfile", "pip-audit"))
    result = run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=runner,
                              now=lambda: NOW, out=lambda _: None)
    assert not result.complete
    coverage = {entry.manager: entry for entry in result.coverage}
    assert coverage["Cargo"].lockfile_state == "failed" and "generate-lockfile broke" in coverage["Cargo"].notes[-1]
    statuses = {run.tool: run.status for run in result.tool_runs}
    assert statuses["pip-audit"] is Status.FAILED and statuses["npm audit"] is Status.OK
    assert statuses["osv-scanner"] is Status.OK  # the others still ran


@requires_git
def test_a_failed_lock_file_alone_makes_the_step_incomplete(make_project, tmp_path):
    project = mixed_project(make_project)
    runner = FakeRunner(outputs={"npm audit": fixture("npm-audit.json"), "cargo audit": fixture("cargo-audit.json"),
                                 "pip-audit": fixture("pip-audit.json")}, fail=("generate-lockfile",))
    result = run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=runner,
                              now=lambda: NOW, out=lambda _: None)
    assert all(run.status is not Status.FAILED for run in result.tool_runs)
    assert not result.complete and [entry.manager for entry in result.incomplete_lockfiles] == ["Cargo"]
    assert "Lock file failed: Cargo (rs/Cargo.toml): generating the lock file failed" in format_dependencies(result)


class GradleRunner(FakeRunner):
    """Gradle locks app/ and lib/, but reports the configurations it could not lock."""

    def __init__(self, not_locked):
        super().__init__()
        self.not_locked = not_locked

    def run(self, image, command, *, workdir, **kwargs):
        if "codeAnalysisLock" in command:
            for module in ("app", "lib"):
                (workdir / module / "gradle.lockfile").write_text("org.example:x:1.0=runtimeClasspath\n")
            stdout = "> Task :app:codeAnalysisLock\n" + "".join(
                f"{lockfiles.GRADLE_MARKER}{line}\n" for line in self.not_locked)
            return CommandResult(tuple(command), 0, stdout, "", 1.0)
        return super().run(image, command, workdir=workdir, **kwargs)


def gradle_project(make_project) -> Path:
    project = make_project({"settings.gradle": "include 'app', 'lib'\n",
                            "app/build.gradle": "plugins { id 'java' }\n", "app/src/main/java/A.java": "class A {}\n",
                            "lib/build.gradle": "plugins { id 'java' }\n", "lib/src/main/java/B.java": "class B {}\n"})
    commit_all(project)
    return project


@requires_git
def test_gradle_lock_files_of_all_modules(make_project, tmp_path):
    result = run_dependencies(run_acceptance(gradle_project(make_project)), settings(tmp_path), tmp_path / "run",
                              runner=GradleRunner([]), now=lambda: NOW, out=lambda _: None)
    entry = result.coverage[0]
    assert (entry.manager, entry.manifest, entry.lockfile_state) == ("Gradle", "settings.gradle", "generated")
    for module in ("app", "lib"):
        assert (tmp_path / "run/step4-dependencies/lockfiles" / module / "gradle.lockfile").is_file()
    assert result.complete


@requires_git
def test_gradle_configurations_that_cannot_be_locked(make_project, tmp_path):
    not_locked = [f":lib config{number}: unresolved com.example:missing:1.0" for number in range(7)]
    result = run_dependencies(run_acceptance(gradle_project(make_project)), settings(tmp_path), tmp_path / "run",
                              runner=GradleRunner(not_locked), now=lambda: NOW, out=lambda _: None)
    entry = result.coverage[0]
    assert entry.lockfile_state == "partial" and entry.osv  # the other configurations are still checked
    note = entry.notes[-1]
    assert note.startswith("7 Gradle configuration(s) could not be resolved completely")
    assert ":lib config4: unresolved com.example:missing:1.0" in note and ":lib config5" not in note
    assert "2 more in lockfile-logs/01-gradle.stdout.txt" in note
    assert not result.complete and result.incomplete_lockfiles == [entry]
    assert "Lock file incomplete: Gradle (settings.gradle): 7 Gradle configuration(s)" in format_dependencies(result)


@pytest.mark.parametrize(("configurations", "expected"), [
    (("runtimeClasspath", "testRuntimeClasspath"), "runtime"),
    (("releaseRuntimeClasspath",), "runtime"),
    (("compileClasspath", "gwt", "gwtSdk"), "compile"),
    (("debugAndroidTestRuntimeClasspath", "_internal-unified-test-platform-core"), "test"),
    (("grettyRunnerTomcat9", "annotationProcessor", "kotlinCompilerClasspath"), "tooling"),
    ((), None),
])
def test_gradle_scope(configurations, expected):
    assert lockfiles.gradle_scope(configurations) == expected


def test_gradle_findings_get_configurations_and_scope(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app/gradle.lockfile").write_text(
        "# This is a Gradle generated file for dependency locking.\n"
        "org.eclipse.jetty:jetty-server:9.4.44.v20210927=compileClasspath,gwt\n"
        "org.apache.tomcat.embed:tomcat-embed-core:9.0.58=grettyRunnerTomcat9\n"
        "empty=annotationProcessor\n")
    findings = [
        DependencyFinding("osv-scanner", "Maven", "org.eclipse.jetty:jetty-server", "9.4.44.v20210927", "GHSA-1",
                          manifest="app/gradle.lockfile"),
        DependencyFinding("osv-scanner", "Maven", "org.apache.tomcat.embed:tomcat-embed-core", "9.0.58", "GHSA-2",
                          manifest="app/gradle.lockfile"),
        DependencyFinding("osv-scanner", "npm", "lodash", "4.17.15", "GHSA-3", manifest="web/package-lock.json"),
    ]
    jetty, tomcat, lodash = lockfiles.add_gradle_scopes(findings, tmp_path)
    assert (jetty.configurations, jetty.scope) == (("compileClasspath", "gwt"), "compile")
    assert (tomcat.configurations, tomcat.scope) == (("grettyRunnerTomcat9",), "tooling")
    assert (lodash.configurations, lodash.scope) == ((), None)
    assert scope_counts([jetty, tomcat, tomcat, lodash]) == {"compile": 1, "tooling": 2}


def test_gradle_init_script_reports_with_the_marker():
    assert lockfiles.GRADLE_MARKER.strip() in lockfiles.GRADLE_INIT_SCRIPT
    assert "resolutionResult" in lockfiles.GRADLE_INIT_SCRIPT  # dependency graph only, no artifacts


def test_error_text_prefers_gradles_explanation():
    stderr = ("\nFAILURE: Build failed with an exception.\n\n* Where:\nInitialization script line: 7\n\n"
              "* What went wrong:\nExecution failed for task ':android:codeAnalysisLock'.\n"
              "> SDK location not found.\n\n* Try:\n> Run with --stacktrace option.\n\nBUILD FAILED in 54s\n")
    assert lockfiles._error_text(stderr) == ("Execution failed for task ':android:codeAnalysisLock'. "
                                             "> SDK location not found.")
    assert lockfiles._error_text("a\nb\nc\nd") == "b | c | d"


@requires_git
def test_conan_audit_only_with_a_token(make_project, tmp_path):
    project = make_project({"main.cpp": "int main() { return 0; }\n", "conanfile.txt": "[requires]\nzlib/1.2.11\n",
                            "conan.lock": "{}\n"})
    commit_all(project)
    runner = FakeRunner()
    result = run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=runner,
                              now=lambda: NOW, out=lambda _: None)
    (entry,) = result.coverage
    assert entry.lockfile_state == "committed" and entry.audit is None
    assert any("CONAN_AUDIT_TOKEN" in note for note in entry.notes)
    assert any("no ConanCenter ecosystem" in note for note in entry.notes)

    runner = FakeRunner(outputs={"conan audit": json.dumps({"data": {}})})
    result = run_dependencies(run_acceptance(project), settings(tmp_path, token="secret"), tmp_path / "run2",
                              runner=runner, now=lambda: NOW, out=lambda _: None)
    conan = next(call for call in runner.calls if "conan audit" in " ".join(call["command"]))
    assert conan["env"] == {"CONAN_AUDIT_TOKEN": "secret"}
    assert "secret" not in " ".join(conan["command"])  # passed through the environment
    summary = json.loads((tmp_path / "run2" / "step4-dependencies" / "summary.json").read_text())
    assert "secret" not in json.dumps(summary)


@requires_git
def test_python_lock_files_are_exported_for_pip_audit(make_project, tmp_path):
    project = make_project({"app.py": "import jinja2\n", "uv.lock": "version = 1\n",
                            "pyproject.toml": '[project]\nname = "x"\nversion = "0"\n'})
    commit_all(project)
    runner = FakeRunner(outputs={"pip-audit": json.dumps({"dependencies": []})})
    run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=runner,
                     now=lambda: NOW, out=lambda _: None)
    script = next(call for call in runner.calls if "pip-audit" in " ".join(call["command"]))["command"][2]
    assert script.startswith("( uv export --frozen")
    assert "pip-audit -f json --progress-spinner off -r /tmp/requirements.txt --no-deps --disable-pip" in script


@requires_git
def test_docker_unavailable(make_project, tmp_path):
    project = mixed_project(make_project)

    class NoDocker(FakeRunner):
        def check_available(self):
            raise DockerError("Docker is not reachable")

    result = run_dependencies(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=NoDocker(),
                              now=lambda: NOW, out=lambda _: None)
    assert result.errors == ["Docker is not reachable"] and not result.complete
    assert json.loads((tmp_path / "run/step4-dependencies/summary.json").read_text())["errors"]


def test_units_use_workspace_roots(tmp_path):
    # npm workspaces: one lock file at the root covers the packages below.
    assert lockfiles._topmost(Path("a/b/package.json"), [("npm", Path("package.json")),
                                                        ("npm", Path("a/b/package.json"))], "npm") == Path(".")

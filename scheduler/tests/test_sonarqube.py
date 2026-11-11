"""Step 3, SonarQube, with a fake Web API and a fake Docker runner (no server, no containers).

The API answers are built from real answers of SonarQube 26.9 (tests/fixtures/sonarqube/).
"""

import json
import shlex
from datetime import datetime
from pathlib import Path

import pytest
from conftest import commit_all, opengrep_config, requires_git

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting.model import CommandResult, Status
from scheduler.static_analysis import run_static_analysis, sonarqube
from scheduler.static_analysis.model import AnalysisRun
from scheduler.static_analysis.sonar_api import SonarError

FIXTURES = Path(__file__).parent / "fixtures" / "sonarqube"
NOW = datetime(2026, 11, 11, 12, 0, 0)
PASSWORD = "Secret-Password-123!"
KEY_PREFIX = "code-analysis_"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


class FakeServer:
    """SonarQube's Web API as far as sonarqube.py uses it."""

    def __init__(self, key: str, *, password: str = PASSWORD, statuses=("UP",), task_statuses=("SUCCESS",),
                 issues=None, hotspots=()):
        self.key = key
        self.password = password
        self.statuses = list(statuses)
        self.task_statuses = list(task_statuses)
        self.issues = [dict(issue, component=issue["component"].replace("code-analysis_sq-multi", key))
                       for issue in (fixture("issues.json") if issues is None else issues)]
        self.hotspots = list(hotspots)
        self.projects: set[str] = set()
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict, str | None]] = []

    def connect(self, login=None, password=""):
        return FakeApi(self, login, password)


class FakeApi:
    def __init__(self, server: FakeServer, login, password):
        self.server, self.login, self.password = server, login, password

    def get(self, path, params=None):
        return self._call("GET", path, dict(params or {}))

    def post(self, path, params=None):
        return self._call("POST", path, dict(params or {}))

    def _call(self, method, path, params):
        server = self.server
        server.calls.append((method, path, params, self.login))
        if path == "api/system/status":
            status = server.statuses.pop(0) if len(server.statuses) > 1 else server.statuses[0]
            if status == "DOWN":
                raise SonarError("SonarQube is not reachable at http://sonarqube:9000: connection refused")
            return {"status": status, "version": "26.9.0.129388"}
        if path == "api/authentication/validate":
            return {"valid": self.login == "admin" and self.password == server.password}
        if path == "api/users/change_password":
            assert self.password == server.password == params["previousPassword"]
            server.password = params["password"]
            return {}
        if path == "api/user_tokens/generate":
            server.tokens[params["name"]] = "squ_token"
            return {"token": "squ_token", "name": params["name"]}
        if path == "api/user_tokens/revoke":
            server.tokens.pop(params["name"])
            return {}
        if path == "api/projects/search":
            return {"components": [{"key": key} for key in server.projects if key == params["projects"]]}
        if path == "api/projects/create":
            server.projects.add(params["project"])
            return {}
        if path == "api/ce/task":
            status = server.task_statuses.pop(0) if len(server.task_statuses) > 1 else server.task_statuses[0]
            return {"task": {"id": params["id"], "status": status, "warnings": ["a warning of the server"]}}
        if path == "api/issues/search":
            return _page(server.issues, params, "issues")
        if path == "api/hotspots/search":
            return _page(server.hotspots, params, "hotspots")
        if path == "api/measures/component":
            return fixture("measures.json")
        if path == "api/measures/component_tree":
            return _page(fixture("files.json"), params, "components")
        if path == "api/qualityprofiles/search":
            return {"profiles": fixture("quality-profiles.json")}
        if path == "api/plugins/installed":
            return {"plugins": [{"key": "python", "version": "5.31 (build 36502)"}, {"key": "cxx", "version": "2.4"}]}
        if path == "api/rules/search":
            cwe = {"python:S4790": ["1240"], "python:S4830": ["295"]}.get(params["rule_key"], [])
            owasp = {"python:S4790": ["a2"], "python:S4830": ["a2", "a5"]}.get(params["rule_key"], [])
            return {"rules": [{"key": params["rule_key"], "name": "a rule", "lang": "py", "sysTags": ["cwe"]}],
                    "facets": [{"property": "cwe", "values": [{"val": v, "count": 1} for v in cwe]},
                               {"property": "owaspTop10-2021", "values": [{"val": v, "count": 1} for v in owasp]}]}
        raise AssertionError(f"unexpected call {method} {path}")


def _page(items, params, field):
    size, page = int(params["ps"]), int(params["p"])
    return {field: items[(page - 1) * size:page * size], "paging": {"pageIndex": page, "pageSize": size,
                                                                       "total": len(items)}}


class FakeRunner:
    """Docker: the scanner writes report-task.txt; builds create bytecode and a classpath."""

    def __init__(self, *, scan_exit=0, dotnet_exit=0, build_exit=0):
        self.scan_exit, self.dotnet_exit, self.build_exit = scan_exit, dotnet_exit, build_exit
        self.calls = []

    def check_available(self):
        pass

    def ensure_image(self, image):
        pass

    def image_versions(self, image):
        return {"sonar-scanner": "SonarScanner CLI 8.3.1.16491"} if "scanner" in image else {"dotnet-sonarscanner": "12.1.0"}

    def run(self, image, command, *, workdir, mounts, env=None, timeout, read_only=(), network=None):
        command = list(command)
        self.calls.append({"image": image, "command": command, "workdir": workdir, "mounts": mounts, "env": env})
        if command[0] in ("mvn", "gradle") or command[:2] in (["sh", "./mvnw"], ["sh", "./gradlew"]):
            if self.build_exit == 0:
                (workdir / "target" / "classes").mkdir(parents=True)
                jar = workdir.parent / "m2" / "lib.jar"
                jar.parent.mkdir(parents=True, exist_ok=True)
                jar.write_text("jar")
                (workdir / sonarqube.CLASSPATH_FILE).write_text(f"{jar}:/missing.jar")
            return CommandResult(tuple(command), self.build_exit, "build out", "", 1.0)
        exit_code = self.dotnet_exit if image == sonarqube.DOTNET_IMAGE else self.scan_exit
        if exit_code == 0:
            Path(env["CODE_ANALYSIS_REPORT"]).write_text("projectKey=x\nceTaskId=task-1\n")
        return CommandResult(tuple(command), exit_code, "scanner out", "scanner err", 2.0)


def settings(tmp_path: Path, password: str | None = PASSWORD) -> Settings:
    return Settings(projects_dir=None, host_home=None, ollama_base_url="x", output_dir=tmp_path / "results",
                    config_dir=opengrep_config(tmp_path / "config"), sonarqube_admin_password=password)


def analyse(project, tmp_path, server, runner, password=PASSWORD):
    return run_static_analysis(run_acceptance(project), settings(tmp_path, password), tmp_path / "run",
                               runner=runner, now=lambda: NOW, out=lambda _: None, tools=["SonarQube"],
                               sonar_api=server.connect, sleep=lambda _: None)


def python_project(make_project):
    project = make_project({
        "requirements.txt": "",
        "pyproject.toml": '[project]\nname = "x"\nrequires-python = ">=3.10"\n',
        "src/app.py": "x = 1\n",
        "api/message_pb2.py": "# Generated by the protocol buffer compiler.  DO NOT EDIT!\nimport x\n",
        "sonar-project.properties": "sonar.exclusions=**/*\n",
    })
    commit_all(project)
    return project


def option(command, name):
    return next(arg.split("=", 1)[1] for arg in command if arg.startswith(f"-D{name}="))


@requires_git
def test_cli_scan(make_project, tmp_path):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project))
    runner = FakeRunner()
    result = analyse(project, tmp_path, server, runner)

    (run,) = result.tool_runs
    assert run.status is Status.OK and result.complete
    assert run.version == "SonarQube 26.9.0.129388"
    call = runner.calls[0]
    sonar_dir = tmp_path / "run" / "step3-static-analysis" / "sonarqube"
    assert call["image"] == sonarqube.SCANNER_IMAGE
    assert call["mounts"] == [tmp_path / "run" / "step3-static-analysis"]  # not the project
    assert call["env"]["SONAR_TOKEN"] == "squ_token"
    assert option(call["command"], "project.settings") == str(sonar_dir / "sonar-project.properties")
    assert option(call["command"], "sonar.working.directory") == sonarqube.SCANNER_WORK_DIR
    assert call["workdir"] == sonar_dir / "workspace"

    properties = (sonar_dir / "sonar-project.properties").read_text()
    assert "sonar.projectKey=code-analysis_project1\n" in properties
    assert "sonar.cxx.file.suffixes=.c,.h,.cc,.cpp,.cxx,.c++,.hh,.hpp,.hxx,.h++,.ipp\n" in properties
    assert "sonar.rust.clippy.enabled=false\n" in properties
    assert "sonar.python.version=3.10\n" in properties
    exclusions = next(line for line in properties.splitlines() if line.startswith("sonar.exclusions="))
    assert "**/node_modules/**" in exclusions and "api/message_pb2.py" in exclusions
    assert "**/*" not in exclusions  # the project's sonar-project.properties is not used

    rules = {finding.rule: finding for finding in run.findings}
    weak_hash = rules["python:S4790"]
    assert (weak_hash.file, weak_hash.line, weak_hash.column) == ("src/crypto.py", 6, 20)
    assert (weak_hash.severity, weak_hash.tool_severity, weak_hash.category) == ("error", "HIGH", "security")
    assert weak_hash.cwe == ("CWE-1240",) and weak_hash.owasp == ("A02:2021",)
    assert weak_hash.ruleset == "Sonar way" and weak_hash.subcategory == ("vulnerability",)
    assert rules["python:S1481"].severity == "note" and rules["python:S1481"].category == "maintainability"
    assert run.measures["duplicated_lines_density"] == "27.1"
    assert run.rulesets == [{"name": "Sonar way", "language": "py", "key": "77e0adf5-e11c-4dc8-98a3-640d3ddfee9f"}]
    assert "SonarQube: a warning of the server" in run.notes
    assert [entry["file"] for entry in run.details["duplicated_files"]] == ["src/dup_a.py", "src/dup_b.py"]

    assert server.tokens == {}  # revoked
    assert server.projects == {"code-analysis_project1"}
    assert not (sonar_dir / "workspace").exists()
    summary = json.loads((tmp_path / "run" / "step3-static-analysis" / "summary.json").read_text())
    assert summary["tools"][0]["measures"]["ncloc"] == "73"
    assert summary["tools"][0]["details"]["web_interface"].endswith("?id=code-analysis_project1")
    assert all("squ_token" not in json.dumps(command) for command in summary["tools"][0]["commands"])
    assert (sonar_dir / "api" / "issues.json").is_file()


@requires_git
def test_workspace_copy(make_project, tmp_path):
    project = make_project({"a.py": "x = 1\n", "vendor/lib.py": "y = 2\n", ".mvn/wrapper/x.properties": "a=b\n",
                            ".gitignore": "ignored.txt\n", "ignored.txt": "secret\n"})
    commit_all(project)
    (project / "untracked.py").write_text("z = 3\n")
    workspace = tmp_path / "workspace"
    assert sonarqube.prepare_workspace(project, workspace) == 5
    assert (workspace / "vendor/lib.py").is_file() and (workspace / ".mvn/wrapper/x.properties").is_file()
    assert (workspace / "untracked.py").is_file() and not (workspace / "ignored.txt").exists()
    assert (workspace / ".git").is_dir()


@requires_git
def test_first_start_replaces_the_default_password(make_project, tmp_path):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project), password="admin")
    result = analyse(project, tmp_path, server, FakeRunner())
    assert server.password == PASSWORD
    assert result.tool_runs[0].status is Status.OK
    assert any("first start" in note for note in result.tool_runs[0].notes)


@requires_git
def test_wrong_password_and_missing_password(make_project, tmp_path):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project), password="something else")
    run = analyse(project, tmp_path, server, FakeRunner()).tool_runs[0]
    assert run.status is Status.FAILED and "rejects the password" in run.notes[-1]

    run = analyse(project, tmp_path, server, FakeRunner(), password=None).tool_runs[0]
    assert run.status is Status.FAILED and "SONARQUBE_ADMIN_PASSWORD is not set" in run.notes[0]


@requires_git
def test_waits_for_the_server_and_gives_up(make_project, tmp_path, monkeypatch):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project), statuses=("STARTING", "STARTING", "UP"))
    assert analyse(project, tmp_path, server, FakeRunner()).tool_runs[0].status is Status.OK

    monkeypatch.setattr(sonarqube, "UP_TIMEOUT_SECONDS", 6)
    server = FakeServer(sonarqube.project_key(project), statuses=("DOWN",))
    run = analyse(project, tmp_path, server, FakeRunner()).tool_runs[0]
    assert run.status is Status.FAILED and "docker compose -f docker/docker-compose.yml up -d" in run.notes[-1]


@requires_git
def test_scanner_failure_still_revokes_the_token(make_project, tmp_path):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project))
    run = analyse(project, tmp_path, server, FakeRunner(scan_exit=2)).tool_runs[0]
    assert run.status is Status.FAILED and run.notes[-1].startswith("SonarScanner failed (exit code 2)")
    assert server.tokens == {}


@requires_git
def test_failed_server_task(make_project, tmp_path):
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project), task_statuses=("PENDING", "IN_PROGRESS", "FAILED"))
    run = analyse(project, tmp_path, server, FakeRunner()).tool_runs[0]
    assert run.status is Status.FAILED and "could not process the analysis: FAILED" in run.notes[-1]


@requires_git
def test_issues_are_fetched_page_by_page(make_project, tmp_path, monkeypatch):
    monkeypatch.setattr(sonarqube, "PAGE_SIZE", 2)
    project = python_project(make_project)
    server = FakeServer(sonarqube.project_key(project))
    run = analyse(project, tmp_path, server, FakeRunner()).tool_runs[0]
    assert len(run.findings) == 5
    pages = [call[2]["p"] for call in server.calls if call[1] == "api/issues/search"]
    assert pages == [1, 2, 3]


def csharp_project(make_project):
    project = make_project({
        "app.csproj": '<Project Sdk="Microsoft.NET.Sdk"/>\n',
        "Program.cs": "class P { static void Main() {} }\n",
        "web/site.js": "var x = 1;\n",
    })
    commit_all(project)
    return project


@requires_git
def test_csharp_uses_the_dotnet_scanner(make_project, tmp_path):
    project = csharp_project(make_project)
    runner = FakeRunner()
    run = analyse(project, tmp_path, FakeServer(sonarqube.project_key(project)), runner).tool_runs[0]
    assert run.status is Status.OK and run.details["scanner"] == "SonarScanner for .NET"
    (call,) = runner.calls
    assert call["image"] == sonarqube.DOTNET_IMAGE
    script = call["command"][2]
    begin = shlex.split(script.splitlines()[0])
    assert begin[:3] == ["dotnet", "sonarscanner", "begin"]
    assert "/k:code-analysis_project1" in begin and "/n:project1" in begin
    assert any(arg.startswith("/v:") for arg in begin)
    assert "/d:sonar.cs.roslyn.ignoreIssues=true" in begin and "/d:sonar.scanner.scanAll=true" in begin
    assert f"/d:sonar.projectBaseDir={tmp_path / 'run/step3-static-analysis/sonarqube/workspace'}" in begin
    assert "/d:sonar.token=$SONAR_TOKEN" in begin  # the token itself is not in the command
    assert "dotnet build app.csproj --nologo || failed=1" in script
    assert 'dotnet sonarscanner end "/d:sonar.token=$SONAR_TOKEN" || exit 12' in script


@requires_git
def test_csharp_build_failure_falls_back_to_the_cli_scanner(make_project, tmp_path):
    project = csharp_project(make_project)
    runner = FakeRunner(dotnet_exit=11)
    run = analyse(project, tmp_path, FakeServer(sonarqube.project_key(project)), runner).tool_runs[0]
    assert [call["image"] for call in runner.calls] == [sonarqube.DOTNET_IMAGE, sonarqube.SCANNER_IMAGE]
    assert run.status is Status.PARTIAL
    assert any(note.startswith("C# not analysed: SonarScanner for .NET failed at dotnet build") for note in run.notes)


def java_project(make_project):
    project = make_project({
        "pom.xml": "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>\n",
        "src/main/java/x/A.java": "package x; class A {}\n",
    })
    commit_all(project)
    return project


@requires_git
def test_java_is_compiled_first(make_project, tmp_path):
    project = java_project(make_project)
    runner = FakeRunner()
    run = analyse(project, tmp_path, FakeServer(sonarqube.project_key(project)), runner).tool_runs[0]
    assert run.status is Status.OK
    build, scan = runner.calls
    sonar_dir = tmp_path / "run" / "step3-static-analysis" / "sonarqube"
    assert build["command"][:5] == ["mvn", "-B", "-q", "-DskipTests", f"-Dmaven.repo.local={sonar_dir / 'm2'}"]
    assert "dependency:build-classpath" in build["command"] and build["workdir"] == sonar_dir / "workspace"
    properties = (sonar_dir / "sonar-project.properties").read_text()
    assert f"sonar.java.binaries={sonar_dir / 'workspace' / 'target' / 'classes'}\n" in properties
    assert f"sonar.java.libraries={sonar_dir / 'm2' / 'lib.jar'}\n" in properties  # missing jars left out
    assert "sonar.java.source=17\n" in properties
    assert run.details["java_bytecode_folders"] == ["target/classes"]
    assert not (sonar_dir / "m2").exists()


@requires_git
def test_java_build_failure_analyses_without_bytecode(make_project, tmp_path):
    project = java_project(make_project)
    run = analyse(project, tmp_path, FakeServer(sonarqube.project_key(project)), FakeRunner(build_exit=1)).tool_runs[0]
    assert run.status is Status.PARTIAL
    assert any("Java analysed without (all) bytecode" in note and "(maven: exit code 1)" in note for note in run.notes)
    properties = (tmp_path / "run/step3-static-analysis/sonarqube/sonar-project.properties").read_text()
    assert "no-bytecode" in properties


def test_helpers():
    assert sonarqube.project_key(Path("/x/My Project!")) == "code-analysis_My_Project"
    assert sonarqube.java_build_roots([Path("pom.xml"), Path("core/pom.xml"), Path("tools/build.gradle")]) == [
        (Path("."), "maven")]
    assert sonarqube.java_build_roots([Path("a/pom.xml"), Path("b/settings.gradle"), Path("b/sub/build.gradle")]) == [
        (Path("a"), "maven"), (Path("b"), "gradle")]
    patterns = sonarqube.exclusions(["gen/x_pb2.py"])
    assert "**/vendor/**" in patterns and "**/.*/**" in patterns and patterns[-1] == "gen/x_pb2.py"


def test_hotspot_finding():
    hotspot = {"key": "h1", "ruleKey": "python:S4721", "component": "k:src/a.py", "securityCategory": "command-injection",
               "vulnerabilityProbability": "HIGH", "message": "Make sure ...", "line": 3,
               "textRange": {"startLine": 3, "endLine": 3, "startOffset": 4, "endOffset": 9}}
    rules = {"python:S4721": {"language": "py", "cwe": ["CWE-78"], "owasp": ["A03:2021"]}}
    finding = sonarqube.hotspot_finding(hotspot, "k", rules, {"py": "Sonar way"}, Path("/p"))
    assert (finding.severity, finding.category, finding.subcategory) == ("error", "security",
                                                                         ("hotspot", "command-injection"))
    assert (finding.column, finding.end_column, finding.cwe, finding.ruleset) == (5, 10, ("CWE-78",), "Sonar way")


@requires_git
def test_tools_run_in_parallel_and_independently(make_project, tmp_path, monkeypatch):
    project = python_project(make_project)

    def broken(ctx):
        raise RuntimeError("boom")

    def fine(ctx):
        return AnalysisRun(tool="sonarqube", status=Status.OK)

    from scheduler.static_analysis import run as run_module
    monkeypatch.setattr(run_module, "TOOLS", (("OpenGrep", broken), ("SonarQube", fine)))
    result = run_static_analysis(run_acceptance(project), settings(tmp_path), tmp_path / "run", runner=FakeRunner(),
                                 now=lambda: NOW, out=lambda _: None)
    assert [(run.tool, run.status) for run in result.tool_runs] == [("opengrep", Status.FAILED),
                                                                     ("sonarqube", Status.OK)]
    assert result.tool_runs[0].notes == ["internal error: boom"]
    assert (tmp_path / "run/step3-static-analysis/opengrep-internal-error.txt").is_file()


@pytest.mark.parametrize(("impacts", "expected"), [
    ([{"softwareQuality": "MAINTAINABILITY", "severity": "LOW"}, {"softwareQuality": "SECURITY", "severity": "BLOCKER"}],
     ("error", "BLOCKER", "security")),
    ([], ("warning", "MAJOR", None)),
])
def test_issue_severity(impacts, expected):
    issue = {"rule": "py:S1", "component": "k:a.py", "impacts": impacts, "severity": "MAJOR", "type": "BUG"}
    finding = sonarqube.issue_finding(issue, "k", {}, {}, Path("/p"))
    assert (finding.severity, finding.tool_severity, finding.category) == expected


class ProfileApi:
    """Quality profile endpoints for use_cxx_profile."""

    def __init__(self, existing: bool):
        self.calls = []
        self.profiles = [{"key": "builtin", "name": "Sonar way", "isBuiltIn": True, "activeRuleCount": 0}]
        if existing:
            self.profiles.append({"key": "own", "name": sonarqube.CXX_PROFILE, "activeRuleCount": 22})

    def get(self, path, params=None):
        self.calls.append(("GET", path, dict(params or {})))
        assert path == "api/qualityprofiles/search" and params == {"language": "cxx"}
        return {"profiles": self.profiles}

    def post(self, path, params=None):
        self.calls.append(("POST", path, dict(params or {})))
        if path == "api/qualityprofiles/copy":
            self.profiles.append({"key": "own", "name": params["toName"], "activeRuleCount": 0})
            return {"key": "own", "name": params["toName"]}
        if path == "api/qualityprofiles/activate_rules":
            self.profiles[-1]["activeRuleCount"] = 23
        if path == "api/qualityprofiles/deactivate_rule":
            self.profiles[-1]["activeRuleCount"] -= 1
        return {}


@pytest.mark.parametrize("existing", [False, True])
def test_cxx_profile(existing):
    api = ProfileApi(existing)
    info = sonarqube.use_cxx_profile(api, "code-analysis_x")
    posts = [(path, params) for method, path, params in api.calls if method == "POST"]
    copies = [params for path, params in posts if path == "api/qualityprofiles/copy"]
    assert copies == ([] if existing else [{"fromKey": "builtin", "toName": "Sonar way + sonar-cxx rules"}])
    # Every run: all own rules except templates, then the excluded ones off again.
    assert ("api/qualityprofiles/activate_rules",
            {"targetKey": "own", "repositories": "cxx", "is_template": "false"}) in posts
    assert ("api/qualityprofiles/deactivate_rule", {"key": "own", "rule": "cxx:FileHeader"}) in posts
    assert ("api/qualityprofiles/add_project",
            {"language": "cxx", "project": "code-analysis_x", "qualityProfile": "Sonar way + sonar-cxx rules"}) in posts
    assert info["active_rules"] == 22 and "cxx:FileHeader" in info["excluded_rules"]

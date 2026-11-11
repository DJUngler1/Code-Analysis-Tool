"""SonarQube Community Build: analyse a copy of the project on the server, fetch the results.

1. The server (compose service `sonarqube`) must be UP.
2. Log in as admin. On the first start the default password admin/admin is
   replaced by SONARQUBE_ADMIN_PASSWORD. Each run gets its own analysis token,
   revoked at the end.
3. Copy the project (files git knows, as step 2 left them) into the run folder:
   builds don't touch the project.
4. Java: compile with the project's Maven or Gradle (bytecode is required).
5. Scan: SonarScanner for .NET around `dotnet build` if the project has C#
   (the CLI can't analyse C#), otherwise SonarScanner CLI. Fixed parameters,
   the project's sonar-project.properties is ignored.
6. Wait for the server to process the report, then fetch issues, security
   hotspots, metrics, duplications and rule metadata through the Web API.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath

from scheduler.acceptance.filetree import SKIP_DIRS
from scheduler.config import Settings
from scheduler.linting.docker import DockerError
from scheduler.linting.files import source_files
from scheduler.linting.languages.csharp import build_targets
from scheduler.linting.model import CommandResult, Status
from scheduler.linting.session import last_lines
from scheduler.linting.snapshots import head_commit
from scheduler.linting.versions import java_version, python_version
from scheduler.static_analysis.context import AnalysisContext, language_of
from scheduler.static_analysis.model import AnalysisFinding, AnalysisRun
from scheduler.static_analysis.sonar_api import HttpSonarApi, SonarApi, SonarError

TOOL = "sonarqube"
SCANNER_IMAGE = "code-analysis/sonar-scanner:latest"
DOTNET_IMAGE = "code-analysis/sonar-dotnet:latest"
WEB_INTERFACE = "http://localhost:9000"  # port published in docker-compose.yml
ADMIN_LOGIN = "admin"
DEFAULT_ADMIN_PASSWORD = "admin"  # SonarQube's password before the first change
UP_TIMEOUT_SECONDS = 5 * 60
TASK_TIMEOUT_SECONDS = 30 * 60
BUILD_TIMEOUT_SECONDS = 30 * 60
SCAN_TIMEOUT_SECONDS = 60 * 60
POLL_SECONDS = 3
PAGE_SIZE = 500
MAX_RESULTS = 10_000  # the Web API returns at most this many issues per search
# The scanner works in the container: the Rust analyzer extracted into a
# working directory on a macOS bind mount could not be started.
SCANNER_WORK_DIR = "/tmp/scannerwork"
# sonar-cxx's sensor is off by default; these are its recommended C and C++ suffixes.
CXX_SUFFIXES = ".c,.h,.cc,.cpp,.cxx,.c++,.hh,.hpp,.hxx,.h++,.ipp"
MEASURES = (
    "ncloc", "lines", "files", "functions", "classes", "statements",
    "complexity", "cognitive_complexity",
    "duplicated_lines", "duplicated_lines_density", "duplicated_blocks", "duplicated_files",
    "bugs", "vulnerabilities", "code_smells", "security_hotspots",
    "reliability_rating", "security_rating", "sqale_rating", "sqale_index", "sqale_debt_ratio",
    "comment_lines", "comment_lines_density",
)
FILE_MEASURES = "ncloc,complexity,cognitive_complexity,duplicated_lines,duplicated_blocks"
# sonar-cxx's "Sonar way" activates no rule. This copy of it activates the
# plugin's own rules (repository "cxx", without rule templates).
CXX_PROFILE = "Sonar way + sonar-cxx rules"
# Left out of that profile, with the reason (docs/staticAnalysis/README.md).
CXX_EXCLUDED_RULES = {
    # Without a configured header pattern (default: empty) it reported every
    # file as BLOCKER, also files with a copyright and SPDX header.
    "cxx:FileHeader": "reports every file without a configured header pattern",
}
# Step-1 language -> SonarQube language key (quality profiles).
SONAR_LANGUAGES = {
    "C": "cxx", "C++": "cxx", "C#": "cs", "Java": "java", "JavaScript": "js",
    "TypeScript": "ts", "Python": "py", "Rust": "rust",
}
_IMPACT_ORDER = ("BLOCKER", "HIGH", "MEDIUM", "LOW", "INFO")
_SEVERITIES = {"BLOCKER": "error", "HIGH": "error", "CRITICAL": "error", "MEDIUM": "warning", "MAJOR": "warning",
               "LOW": "note", "MINOR": "note", "INFO": "note"}

GRADLE_INIT_SCRIPT = """\
// Code-Analysis-Tool: writes each Java project's compile classpath for SonarQube.
allprojects {
    plugins.withId('java') {
        def classpath = project.sourceSets.main.compileClasspath
        def target = project.file('code-analysis-classpath.txt')
        tasks.register('codeAnalysisClasspath') {
            doLast { target.text = classpath.files.join('\\n') }
        }
    }
}
"""
CLASSPATH_FILE = "code-analysis-classpath.txt"


def run_sonarqube(ctx: AnalysisContext) -> AnalysisRun:
    run = AnalysisRun(tool=TOOL, status=Status.FAILED, output_dir=TOOL)
    settings = ctx.settings
    if not settings.sonarqube_admin_password:
        run.notes.append("SONARQUBE_ADMIN_PASSWORD is not set in docker/.env (see docker/.env.example)")
        return run
    try:
        for image in _images(ctx):
            ctx.runner.ensure_image(image)
    except DockerError as exc:
        run.notes.append(str(exc))
        return run

    connect = ctx.sonar_api or _http_api(settings)
    try:
        status = _wait_until_up(connect(), ctx)
        run.version = f"SonarQube {status.get('version')}"
        admin = _login(connect, settings.sonarqube_admin_password, run)
        token_name = f"code-analysis-{uuid.uuid4().hex[:12]}"
        token = admin.post("api/user_tokens/generate", {"name": token_name, "type": "GLOBAL_ANALYSIS_TOKEN"})["token"]
        try:
            _analyse(ctx, run, admin, token)
        finally:
            try:
                admin.post("api/user_tokens/revoke", {"login": ADMIN_LOGIN, "name": token_name})
            except SonarError as exc:
                run.notes.append(f"could not revoke the analysis token {token_name}: {exc}")
    except SonarError as exc:
        run.status = Status.FAILED
        run.notes.append(str(exc))
    return run


def _images(ctx: AnalysisContext) -> list[str]:
    return [SCANNER_IMAGE, DOTNET_IMAGE] if "C#" in ctx.languages else [SCANNER_IMAGE]


def _http_api(settings: Settings) -> Callable[..., SonarApi]:
    def connect(login: str | None = None, password: str = "") -> SonarApi:
        return HttpSonarApi(settings.sonarqube_url, login=login, password=password)

    return connect


def _wait_until_up(api: SonarApi, ctx: AnalysisContext) -> dict:
    waited = 0.0
    while True:
        try:
            status = api.get("api/system/status")
        except SonarError as exc:
            status, error = {}, exc
        else:
            error = None
            if status.get("status") == "UP":
                return status
        if waited >= UP_TIMEOUT_SECONDS:
            detail = error or f"status {status.get('status')}"
            raise SonarError(f"SonarQube is not up ({detail}). Start it with:\n"
                             "  docker compose -f docker/docker-compose.yml up -d")
        if waited == 0:
            ctx.out(f"  {'SonarQube':<22} waiting for the server ...")
        ctx.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def _login(connect: Callable[..., SonarApi], password: str, run: AnalysisRun) -> SonarApi:
    admin = connect(ADMIN_LOGIN, password)
    if admin.get("api/authentication/validate").get("valid"):
        return admin
    default = connect(ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD)
    if not default.get("api/authentication/validate").get("valid"):
        raise SonarError("SonarQube rejects the password of the user admin: SONARQUBE_ADMIN_PASSWORD in "
                         "docker/.env differs from the password set in SonarQube")
    default.post("api/users/change_password",
                 {"login": ADMIN_LOGIN, "previousPassword": DEFAULT_ADMIN_PASSWORD, "password": password})
    run.notes.append("first start: replaced SonarQube's default admin password with SONARQUBE_ADMIN_PASSWORD")
    return admin


def _analyse(ctx: AnalysisContext, run: AnalysisRun, api: SonarApi, token: str) -> None:
    sonar_dir = ctx.step_dir / TOOL
    sonar_dir.mkdir(parents=True, exist_ok=True)
    key = project_key(ctx.project)
    if not api.get("api/projects/search", {"projects": key}).get("components"):
        api.post("api/projects/create", {"project": key, "name": ctx.project.name})
    run.details.update({"project_key": key, "web_interface": f"{WEB_INTERFACE}/dashboard?id={key}"})
    if {"C", "C++"} & set(ctx.languages):
        run.details["cxx_profile"] = use_cxx_profile(api, key)

    workspace = sonar_dir / "workspace"
    try:
        copied = prepare_workspace(ctx.project, workspace)
        run.details["files_copied"] = copied
        properties = analysis_properties(ctx, key)
        degraded: list[str] = []
        if "Java" in ctx.languages:
            properties.update(_build_java(ctx, run, workspace, sonar_dir, degraded))
        report = sonar_dir / "report-task.txt"
        report.unlink(missing_ok=True)
        targets = build_targets(ctx.manifests("C#")) if "C#" in ctx.languages else []
        scanned = False
        if targets:
            scanned = _scan_dotnet(ctx, run, workspace, sonar_dir, properties, targets, token, report, degraded)
        elif "C#" in ctx.languages:
            degraded.append("C# not analysed: no solution or project file (.sln, .slnx, .csproj)")
        if not scanned:
            result = _scan_cli(ctx, run, workspace, sonar_dir, properties, token, report)
            if result.exit_code != 0 or not report.is_file():
                run.notes.append(f"SonarScanner failed ({_reason(result)}): {last_lines(result.stdout + result.stderr)}")
                return
        task = _wait_for_task(api, _task_id(report), ctx)
        _write_json(sonar_dir / "api" / "ce-task.json", task)
        if task.get("status") != "SUCCESS":
            run.notes.append(f"SonarQube could not process the analysis: {task.get('status')} "
                             f"{task.get('errorMessage', '')}".strip())
            return
        run.notes += [f"SonarQube: {warning}" for warning in task.get("warnings", [])]
        _collect(ctx, run, api, key, sonar_dir)
        run.notes += degraded
        run.status = Status.PARTIAL if degraded else Status.OK
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        for cache in ("m2", "gradle-home"):
            shutil.rmtree(sonar_dir / cache, ignore_errors=True)


def use_cxx_profile(api: SonarApi, key: str) -> dict:
    """Use the plugin's own C/C++ rules for the project.

    sonar-cxx's default profile "Sonar way" activates none of its rules; its
    wiki says to copy that profile and activate rules in the copy. The copy
    gets every rule of the repository "cxx" (the plugin's own checks) except
    the rule templates and CXX_EXCLUDED_RULES, with the plugin's default
    parameters. Rules of other
    tools (clang-tidy, cppcheck, ...) stay off: their reports are not imported.
    Repeated on every run, so rules of a newer plugin version are added too.
    """
    profiles = api.get("api/qualityprofiles/search", {"language": "cxx"}).get("profiles", [])
    profile = next((entry for entry in profiles if entry.get("name") == CXX_PROFILE), None)
    if profile is None:
        builtin = next(entry for entry in profiles if entry.get("isBuiltIn") or entry.get("name") == "Sonar way")
        profile = api.post("api/qualityprofiles/copy", {"fromKey": builtin["key"], "toName": CXX_PROFILE})
    api.post("api/qualityprofiles/activate_rules",
             {"targetKey": profile["key"], "repositories": "cxx", "is_template": "false"})
    for rule in CXX_EXCLUDED_RULES:
        api.post("api/qualityprofiles/deactivate_rule", {"key": profile["key"], "rule": rule})
    api.post("api/qualityprofiles/add_project", {"language": "cxx", "project": key, "qualityProfile": CXX_PROFILE})
    current = next((entry for entry in api.get("api/qualityprofiles/search", {"language": "cxx"}).get("profiles", [])
                    if entry.get("name") == CXX_PROFILE), profile)
    return {"name": CXX_PROFILE, "active_rules": current.get("activeRuleCount"),
            "excluded_rules": dict(CXX_EXCLUDED_RULES)}


# --- project copy and parameters ------------------------------------------------


def project_key(project: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", project.name).strip("_") or "project"
    return f"code-analysis_{name}"


def prepare_workspace(project: Path, workspace: Path) -> int:
    """Copy the files git knows (tracked, or untracked and not ignored) into the workspace.

    All of them, also vendored and generated ones: the build may need them.
    The analysis leaves them out with sonar.exclusions. The copy becomes a git
    repository of its own (nothing committed), so that SonarQube's text and
    secrets sensor sees all files, not only those of known languages.
    """
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True)
    copied = 0
    for relative in source_files(project, None, skip_folders=False):
        source = project / relative
        if source.is_symlink() or not source.is_file():
            continue
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied += 1
    for args in (["init", "--quiet"], ["add", "--all"]):
        subprocess.run(["git", "-c", "safe.directory=*", "-C", str(workspace), *args],
                       capture_output=True, check=False, timeout=600)
    return copied


def exclusions(excluded_files: Iterable[str]) -> list[str]:
    """The folders step 1 skips, hidden folders, and generated files (same scope as steps 1 and 2)."""
    patterns = [f"**/{name}/**" for name in sorted(SKIP_DIRS)]
    patterns.append("**/.*/**")
    patterns += sorted(excluded_files)
    return patterns


def analysis_properties(ctx: AnalysisContext, key: str) -> dict[str, str]:
    properties = {
        "sonar.host.url": ctx.settings.scanner_url,
        "sonar.projectKey": key,
        "sonar.projectName": ctx.project.name,
        "sonar.projectVersion": head_commit(ctx.project) or "unknown",
        "sonar.sourceEncoding": "UTF-8",
        # The copy has no history: no blame information.
        "sonar.scm.disabled": "true",
        "sonar.exclusions": ",".join(exclusions(ctx.acceptance.languages.excluded)),
        # sonar-cxx: switch on its C/C++ sensor.
        "sonar.cxx.file.suffixes": CXX_SUFFIXES,
        # Clippy already ran in step 2.
        "sonar.rust.clippy.enabled": "false",
    }
    if "C#" in ctx.languages:
        # Compiler and .NET analyzer warnings (CS..., CA...) are imported by
        # default; dotnet format reports the analyzer fixes in step 2 already.
        properties["sonar.cs.roslyn.ignoreIssues"] = "true"
    if "Python" in ctx.languages:
        version = python_version(ctx.project, ctx.manifests("Python"))
        if version.version:
            properties["sonar.python.version"] = version.version
    if "Java" in ctx.languages:
        version = java_version(ctx.project, ctx.manifests("Java"))
        if version.version:
            properties["sonar.java.source"] = version.version
            if version.preview:
                properties["sonar.java.enablePreview"] = "true"
    return properties


# --- Java build -------------------------------------------------------------------


def _build_java(ctx: AnalysisContext, run: AnalysisRun, workspace: Path, sonar_dir: Path,
                degraded: list[str]) -> dict[str, str]:
    """Compile the Java code; bytecode and classpath for sonar.java.binaries / .libraries."""
    roots = java_build_roots(ctx.manifests("Java"))
    failed: list[str] = []
    init_script = sonar_dir / "code-analysis-classpath.gradle"
    init_script.write_text(GRADLE_INIT_SCRIPT, encoding="utf-8")
    for index, (root, tool) in enumerate(roots, start=1):
        directory = workspace / root
        if tool == "maven":
            launcher = ["sh", "./mvnw"] if (directory / "mvnw").is_file() else ["mvn"]
            command = [*launcher, "-B", "-q", "-DskipTests", f"-Dmaven.repo.local={sonar_dir / 'm2'}",
                       "compile", "dependency:build-classpath", f"-Dmdep.outputFile={CLASSPATH_FILE}"]
        else:
            launcher = ["sh", "./gradlew"] if (directory / "gradlew").is_file() else ["gradle"]
            command = [*launcher, "--no-daemon", "-q", "--init-script", str(init_script),
                       "classes", "codeAnalysisClasspath"]
        result = _run(ctx, run, SCANNER_IMAGE, command, directory, sonar_dir, f"java-build-{index}",
                      env={"GRADLE_USER_HOME": str(sonar_dir / "gradle-home")}, timeout=BUILD_TIMEOUT_SECONDS)
        if result.exit_code != 0:
            failed.append(f"{root.as_posix() or '.'} ({tool}: {_reason(result)})")
    binaries = sorted({path for pattern in ("**/target/classes", "**/build/classes/java/main")
                       for path in workspace.glob(pattern) if path.is_dir()})
    libraries: list[str] = []
    for classpath in sorted(workspace.rglob(CLASSPATH_FILE)):
        for entry in re.split(r"[:\n]", classpath.read_text(encoding="utf-8", errors="replace")):
            if entry.strip().endswith(".jar") and entry.strip() not in libraries and Path(entry.strip()).is_file():
                libraries.append(entry.strip())
    if failed or not binaries:
        reason = f"build failed: {'; '.join(failed)}" if failed else "no build file (pom.xml, build.gradle)"
        degraded.append(f"Java analysed without (all) bytecode, results are less precise ({reason})")
    if not binaries:
        empty = sonar_dir / "no-bytecode"
        empty.mkdir(exist_ok=True)
        binaries = [empty]
    run.details["java_bytecode_folders"] = [path.relative_to(workspace).as_posix() if path.is_relative_to(workspace)
                                            else "none" for path in binaries]
    properties = {"sonar.java.binaries": ",".join(str(path) for path in binaries)}
    if libraries:
        properties["sonar.java.libraries"] = ",".join(libraries)
    return properties


def java_build_roots(manifests: Iterable[Path]) -> list[tuple[Path, str]]:
    """Top-most Maven and Gradle builds: (folder relative to the project, "maven" or "gradle")."""
    found: dict[Path, str] = {}
    for manifest in manifests:
        tool = "maven" if manifest.name == "pom.xml" else "gradle"
        found.setdefault(manifest.parent, tool)
    roots: list[tuple[Path, str]] = []
    for folder in sorted(found, key=lambda path: (len(path.parts), path.as_posix())):
        if not any(folder.is_relative_to(root) for root, _ in roots):
            roots.append((folder, found[folder]))
    return roots


# --- scanners ---------------------------------------------------------------------


def _scan_cli(ctx: AnalysisContext, run: AnalysisRun, workspace: Path, sonar_dir: Path,
              properties: Mapping[str, str], token: str, report: Path) -> CommandResult:
    settings_file = sonar_dir / "sonar-project.properties"
    settings_file.write_text(_properties_file(properties), encoding="utf-8")
    script = ('sonar-scanner "$@"; code=$?; '
              f'cp {SCANNER_WORK_DIR}/report-task.txt "$CODE_ANALYSIS_REPORT" 2>/dev/null; exit $code')
    command = ["sh", "-c", script, "sonar-scanner",
               # Our parameters instead of the project's sonar-project.properties.
               f"-Dproject.settings={settings_file}",
               f"-Dsonar.projectBaseDir={workspace}",
               f"-Dsonar.working.directory={SCANNER_WORK_DIR}"]
    run.details["scanner"] = "SonarScanner CLI"
    return _run(ctx, run, SCANNER_IMAGE, command, workspace, sonar_dir, "sonar-scanner",
                env={"SONAR_TOKEN": token, "CODE_ANALYSIS_REPORT": str(report)}, timeout=SCAN_TIMEOUT_SECONDS)


def _scan_dotnet(ctx: AnalysisContext, run: AnalysisRun, workspace: Path, sonar_dir: Path,
                 properties: Mapping[str, str], targets: list[Path], token: str, report: Path,
                 degraded: list[str]) -> bool:
    """begin, dotnet build, end in one container (the scanner hooks into the build there)."""
    own_switches = {"sonar.projectKey": "/k:", "sonar.projectName": "/n:", "sonar.projectVersion": "/v:"}
    begin = ["dotnet", "sonarscanner", "begin",
             *(f"{switch}{properties[name]}" for name, switch in own_switches.items() if name in properties),
             *(f"/d:{name}={value}" for name, value in properties.items() if name not in own_switches),
             # Base folder of the paths in sonar.exclusions: the copy of the project.
             f"/d:sonar.projectBaseDir={workspace}",
             # Also the other languages of the project (JavaScript, Python, ...).
             "/d:sonar.scanner.scanAll=true"]
    lines = [f'{shlex.join(begin)} "/d:sonar.token=$SONAR_TOKEN" || exit 10', "failed=0"]
    lines += [f"dotnet build {shlex.quote(target.as_posix())} --nologo || failed=1" for target in targets]
    lines += ['[ "$failed" -eq 0 ] || exit 11',
              'dotnet sonarscanner end "/d:sonar.token=$SONAR_TOKEN" || exit 12',
              'cp .sonarqube/out/.sonar/report-task.txt "$CODE_ANALYSIS_REPORT"']
    run.details["scanner"] = "SonarScanner for .NET"
    result = _run(ctx, run, DOTNET_IMAGE, ["sh", "-c", "\n".join(lines)], workspace, sonar_dir, "sonar-dotnet",
                  env={"SONAR_TOKEN": token, "CODE_ANALYSIS_REPORT": str(report)}, timeout=SCAN_TIMEOUT_SECONDS)
    if result.exit_code == 0 and report.is_file():
        return True
    phase = {10: "begin", 11: "dotnet build", 12: "end"}.get(result.exit_code or -1, _reason(result))
    degraded.append(f"C# not analysed: SonarScanner for .NET failed at {phase} "
                    f"({last_lines(result.stdout + result.stderr)}); the other languages with SonarScanner CLI")
    shutil.rmtree(workspace / ".sonarqube", ignore_errors=True)
    return False


def _run(ctx: AnalysisContext, run: AnalysisRun, image: str, command: list[str], workdir: Path, sonar_dir: Path,
         label: str, *, env: Mapping[str, str], timeout: float) -> CommandResult:
    # The project is not mounted: the scanners work on the copy in the run folder.
    result = ctx.runner.run(image, command, workdir=workdir, mounts=[ctx.step_dir], env=env, timeout=timeout)
    run.commands.append(result)
    (sonar_dir / f"{label}.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (sonar_dir / f"{label}.stderr.txt").write_text(result.stderr, encoding="utf-8")
    return result


def _properties_file(properties: Mapping[str, str]) -> str:
    lines = ["# Parameters of the Code-Analysis-Tool (docs/staticAnalysis/README.md)."]
    for name, value in properties.items():
        escaped = value.replace("\\", "\\\\").replace("\n", "\\n")
        lines.append(f"{name}={escaped}")
    return "\n".join(lines) + "\n"


def _reason(result: CommandResult) -> str:
    return "timed out" if result.timed_out else f"exit code {result.exit_code}"


def _task_id(report: Path) -> str:
    values = dict(line.split("=", 1) for line in report.read_text(encoding="utf-8").splitlines() if "=" in line)
    if "ceTaskId" not in values:
        raise SonarError(f"no ceTaskId in {report.name}")
    return values["ceTaskId"]


def _wait_for_task(api: SonarApi, task_id: str, ctx: AnalysisContext) -> dict:
    waited = 0.0
    while True:
        task = api.get("api/ce/task", {"id": task_id}).get("task", {})
        if task.get("status") not in ("PENDING", "IN_PROGRESS"):
            return task
        if waited >= TASK_TIMEOUT_SECONDS:
            raise SonarError(f"SonarQube did not process the analysis within {TASK_TIMEOUT_SECONDS // 60} minutes")
        ctx.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


# --- results ----------------------------------------------------------------------


def _collect(ctx: AnalysisContext, run: AnalysisRun, api: SonarApi, key: str, sonar_dir: Path) -> None:
    raw = sonar_dir / "api"
    issues, total = _pages(api, "api/issues/search",
                           {"components": key, "resolved": "false", "additionalFields": "rules"}, "issues")
    hotspots, hotspot_total = _pages(api, "api/hotspots/search", {"project": key, "status": "TO_REVIEW"}, "hotspots")
    if total > len(issues) or hotspot_total > len(hotspots):
        run.notes.append(f"SonarQube returns at most {MAX_RESULTS} results per search: "
                         f"{total} issues and {hotspot_total} hotspots exist, see the web interface")
    measures = api.get("api/measures/component", {"component": key, "metricKeys": ",".join(MEASURES)})
    files, _ = _pages(api, "api/measures/component_tree",
                      {"component": key, "metricKeys": FILE_MEASURES, "qualifiers": "FIL"}, "components")
    profiles = api.get("api/qualityprofiles/search", {"project": key}).get("profiles", [])
    plugins = api.get("api/plugins/installed").get("plugins", [])
    rule_keys = sorted({issue["rule"] for issue in issues} | {hotspot["ruleKey"] for hotspot in hotspots})
    rules = {rule: _rule_metadata(api, rule) for rule in rule_keys}

    _write_json(raw / "issues.json", issues)
    _write_json(raw / "hotspots.json", hotspots)
    _write_json(raw / "measures.json", measures)
    _write_json(raw / "files.json", files)
    _write_json(raw / "quality-profiles.json", profiles)
    _write_json(raw / "plugins.json", plugins)
    _write_json(raw / "rules.json", rules)

    profile_names = {profile["language"]: profile["name"] for profile in profiles}
    run.findings = [issue_finding(issue, key, rules, profile_names, ctx.project) for issue in issues]
    run.findings += [hotspot_finding(hotspot, key, rules, profile_names, ctx.project) for hotspot in hotspots]
    run.measures = {measure["metric"]: measure.get("value", "")
                    for measure in measures.get("component", {}).get("measures", [])}
    run.files_scanned = int(run.measures["files"]) if run.measures.get("files", "").isdigit() else None
    wanted = {SONAR_LANGUAGES[language] for language in ctx.languages if language in SONAR_LANGUAGES}
    run.rulesets = [{"name": profile["name"], "language": profile["language"], "key": profile.get("key")}
                    for profile in profiles if profile["language"] in wanted]
    run.details["plugins"] = {plugin["key"]: plugin.get("version") for plugin in plugins
                              if plugin["key"] in {"java", "python", "javascript", "csharp", "cxx", "rust"}}
    run.details["duplicated_files"] = [
        {"file": component.get("path"), **{m["metric"]: m.get("value") for m in component.get("measures", [])}}
        for component in files
        if any(m["metric"] == "duplicated_lines" and m.get("value") not in (None, "0")
               for m in component.get("measures", []))
    ]
    versions = {}
    for image in _images(ctx):
        versions.update(ctx.runner.image_versions(image))
    run.details["scanner_versions"] = dict(versions)


def _pages(api: SonarApi, path: str, params: Mapping[str, object], field: str) -> tuple[list[dict], int]:
    items: list[dict] = []
    page, total = 1, 0
    while len(items) < MAX_RESULTS:
        data = api.get(path, {**params, "ps": PAGE_SIZE, "p": page})
        batch = data.get(field, [])
        paging = data.get("paging", {})
        total = int(paging.get("total", len(batch)))
        items += batch
        if not batch or len(items) >= total:
            break
        page += 1
    return items, total


def _rule_metadata(api: SonarApi, rule: str) -> dict:
    """Name, language, tags, CWE and OWASP Top 10 2021 of a rule (facets of a search for just this rule)."""
    data = api.get("api/rules/search", {"rule_key": rule, "f": "name,lang,sysTags,tags", "ps": 1,
                                        "facets": "cwe,owaspTop10-2021"})
    found = (data.get("rules") or [{}])[0]
    facets = {facet["property"]: [value["val"] for value in facet.get("values", []) if value.get("count")]
              for facet in data.get("facets", [])}
    return {
        "name": found.get("name"),
        "language": found.get("lang"),
        "tags": sorted({*found.get("sysTags", []), *found.get("tags", [])}),
        "cwe": [f"CWE-{value}" for value in facets.get("cwe", []) if value != "unknown"],
        "owasp": [f"A{int(value[1:]):02d}:2021" for value in facets.get("owaspTop10-2021", [])
                  if re.fullmatch(r"a\d+", value)],
    }


def issue_finding(issue: Mapping, key: str, rules: Mapping[str, Mapping], profile_names: Mapping[str, str],
                  project: Path) -> AnalysisFinding:
    impacts = sorted(issue.get("impacts", []), key=lambda impact: _rank(impact.get("severity")))
    top = impacts[0] if impacts else {}
    tool_severity = str(top.get("severity") or issue.get("severity") or "INFO")
    rule = str(issue.get("rule", "unknown"))
    meta = rules.get(rule, {})
    text_range = issue.get("textRange") or {}
    file = _file(issue.get("component"), key)
    return AnalysisFinding(
        tool=TOOL,
        ruleset=profile_names.get(str(meta.get("language"))),
        rule=rule,
        severity=_SEVERITIES.get(tool_severity, "warning"),
        tool_severity=tool_severity,
        message=str(issue.get("message", "")).strip(),
        language=language_of(file, project),
        file=file,
        line=text_range.get("startLine", issue.get("line")),
        column=_column(text_range.get("startOffset")),
        end_line=text_range.get("endLine"),
        end_column=_column(text_range.get("endOffset")),
        category=str(top["softwareQuality"]).lower() if top.get("softwareQuality") else None,
        subcategory=tuple(value for value in (str(issue.get("type", "")).lower(),) if value),
        cwe=tuple(meta.get("cwe", ())),
        owasp=tuple(meta.get("owasp", ())),
        fingerprint=issue.get("key"),
    )


def hotspot_finding(hotspot: Mapping, key: str, rules: Mapping[str, Mapping], profile_names: Mapping[str, str],
                    project: Path) -> AnalysisFinding:
    probability = str(hotspot.get("vulnerabilityProbability") or "MEDIUM")
    rule = str(hotspot.get("ruleKey", "unknown"))
    meta = rules.get(rule, {})
    text_range = hotspot.get("textRange") or {}
    file = _file(hotspot.get("component"), key)
    return AnalysisFinding(
        tool=TOOL,
        ruleset=profile_names.get(str(meta.get("language"))),
        rule=rule,
        severity=_SEVERITIES.get(probability, "warning"),
        tool_severity=f"{probability} (security hotspot)",
        message=str(hotspot.get("message", "")).strip(),
        language=language_of(file, project),
        file=file,
        line=text_range.get("startLine", hotspot.get("line")),
        column=_column(text_range.get("startOffset")),
        end_line=text_range.get("endLine"),
        end_column=_column(text_range.get("endOffset")),
        category="security",
        subcategory=tuple(value for value in ("hotspot", hotspot.get("securityCategory")) if value),
        cwe=tuple(meta.get("cwe", ())),
        owasp=tuple(meta.get("owasp", ())),
        fingerprint=hotspot.get("key"),
    )


def _rank(severity: object) -> int:
    return _IMPACT_ORDER.index(severity) if severity in _IMPACT_ORDER else len(_IMPACT_ORDER)


def _file(component: object, key: str) -> str | None:
    text = str(component or "")
    if not text.startswith(f"{key}:"):
        return None
    return PurePosixPath(text[len(key) + 1:]).as_posix()


def _column(offset: object) -> int | None:
    return offset + 1 if isinstance(offset, int) else None  # SonarQube offsets start at 0


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

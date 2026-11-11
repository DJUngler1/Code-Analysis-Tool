"""Step 3 with the real OpenGrep image (`pytest -m docker`).

Needs Docker and the image (`docker compose -f docker/docker-compose.yml
--profile tools build opengrep`). The test projects must lie in a folder Docker
can mount; set CODE_ANALYSIS_DOCKER_TEST_DIR to one (e.g. below your home
folder) if the temporary folder is not shared with Docker.
"""

import os
import shutil
import subprocess
import tomllib
import uuid
from pathlib import Path

import pytest
from conftest import REPOSITORY, commit_all, opengrep_config

from scheduler.acceptance import run_acceptance
from scheduler.config import Settings
from scheduler.linting.docker import DockerError, DockerRunner
from scheduler.linting.model import Status
from scheduler.static_analysis import run_static_analysis
from scheduler.static_analysis.opengrep import IMAGE
from scheduler.static_analysis.rulesets import load_config

pytestmark = pytest.mark.docker

CONFIG_DIR = REPOSITORY / "config"


@pytest.fixture(scope="module")
def runner():
    runner = DockerRunner(network="bridge")
    try:
        runner.check_available()
        runner.ensure_image(IMAGE)
    except DockerError as exc:
        pytest.skip(f"Docker or the OpenGrep image not available: {exc}")
    return runner


@pytest.fixture(scope="module")
def image_rulesets(runner) -> tuple[str, ...]:
    """Rule sets in the image, in the order of config/opengrep.toml."""
    in_image = {key.split(":", 1)[1] for key in runner.image_versions(IMAGE) if key.startswith("ruleset:")}
    defined = tomllib.loads((CONFIG_DIR / "opengrep.toml").read_text())["rulesets"]
    return tuple(name for name in defined if name in in_image)


@pytest.fixture
def workspace(tmp_path, runner):
    base = Path(os.environ.get("CODE_ANALYSIS_DOCKER_TEST_DIR") or tmp_path).resolve()
    folder = base / f"itest-{uuid.uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    probe = subprocess.run(
        ["docker", "run", "--rm", "--user", runner.user, "--mount", f"type=bind,source={folder},target={folder}",
         IMAGE, "touch", str(folder / "probe")],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        shutil.rmtree(folder, ignore_errors=True)
        pytest.skip(f"{base} cannot be mounted by Docker; set CODE_ANALYSIS_DOCKER_TEST_DIR")
    (folder / "probe").unlink()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def project(workspace: Path, files: dict[str, str]) -> Path:
    root = workspace / "project"
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    commit_all(root)
    return root


def analyse(root: Path, workspace: Path, runner, rulesets: tuple[str, ...]):
    config_dir = opengrep_config(workspace / "config", rulesets)
    settings = Settings(projects_dir=None, host_home=None, ollama_base_url="x",
                        output_dir=workspace / "results", config_dir=config_dir)
    acceptance = run_acceptance(root)
    assert acceptance.accepted, acceptance.message
    return run_static_analysis(acceptance, settings, workspace / "results" / "run", runner=runner, out=lambda _: None)


def found(result) -> set[tuple[str | None, str, str | None]]:
    return {(finding.ruleset, finding.rule, finding.file) for finding in result.findings}


FLASK_APP = '''import subprocess
from flask import Flask, request

app = Flask(__name__)


def run(cmd):
    return subprocess.check_output(cmd, shell=True)


@app.route("/ping")
def ping():
    host = request.args.get("host")
    return run("ping -c 1 " + host)
'''


def test_taint_across_functions_and_ignore_lists(workspace, runner, image_rulesets):
    root = project(workspace, {
        "requirements.txt": "flask\n",
        "src/app.py": FLASK_APP,
        "tests/test_app.py": FLASK_APP,
        # The project's ignore file would exclude src/ and, by replacing
        # OpenGrep's default list, include tests/: it is ignored.
        ".semgrepignore": "src/\n",
    })
    result = analyse(root, workspace, runner, image_rulesets)
    assert result.complete
    assert result.tool_runs[0].status is Status.OK
    # Source in ping(), sink in run(): found thanks to --taint-intrafile.
    injection = [finding for finding in result.findings
                 if finding.rule == "python.flask.security.injection.subprocess-injection"]
    assert [(finding.file, finding.line) for finding in injection] == [("src/app.py", 8)]
    assert not any(finding.file.startswith("tests/") for finding in result.findings)  # default ignore list
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True)
    assert status.stdout == ""  # read-only: the project is unchanged


def test_every_supported_language_gets_rules(workspace, runner, image_rulesets):
    base = next((name for name in ("opengrep-rules", "semgrep-rules") if name in image_rulesets), None)
    if base is None or "0xdea-semgrep-rules" not in image_rulesets:
        pytest.skip(f"the image has other rule sets: {', '.join(image_rulesets)}")
    root = project(workspace, {
        # A manifest per language: step 1 needs one for the primary language.
        "pom.xml": "<project/>\n",
        "package.json": '{"name": "x"}\n',
        "cs/app.csproj": '<Project Sdk="Microsoft.NET.Sdk"/>\n',
        "requirements.txt": "",
        "Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\n',
        "conanfile.txt": "[requires]\n",
        "src/main/java/x/Hash.java": "package x;\n\nimport java.security.MessageDigest;\n\npublic class Hash {\n"
                                     "    public byte[] hash(byte[] d) throws Exception {\n"
                                     '        return MessageDigest.getInstance("MD5").digest(d);\n    }\n}\n',
        "cs/Program.cs": "using System.IO;\nusing System.Runtime.Serialization.Formatters.Binary;\n\n"
                         "class Program {\n    static object Load(Stream s) {\n"
                         "        var f = new BinaryFormatter();\n        return f.Deserialize(s);\n    }\n}\n",
        "web/server.ts": 'import express from "express";\n\nconst app = express();\n'
                         'app.get("/", (req, res) => {\n  res.send(eval(req.query.code as string));\n});\n',
        "web/page.js": 'function show(input) {\n  document.getElementById("out").innerHTML = input;\n}\n',
        "app.py": "import pickle\n\n\ndef load(data):\n    return pickle.loads(data)\n",
        "rs/main.rs": "fn main() {\n    let args: Vec<String> = std::env::args().collect();\n"
                      '    println!("{}", args[0]);\n}\n',
        "c/main.c": "#include <stdio.h>\nint main(void) {\n    char buf[16];\n    gets(buf);\n    return 0;\n}\n",
        "cpp/copy.cpp": "#include <cstring>\nvoid copy(char *dst, const char *src) {\n    strcpy(dst, src);\n}\n",
    })
    result = analyse(root, workspace, runner, (base, "0xdea-semgrep-rules"))
    assert result.complete
    assert {
        (base, "java.lang.security.audit.crypto.use-of-md5", "src/main/java/x/Hash.java"),
        (base, "csharp.lang.security.insecure-deserialization.insecure-binaryformatter-deserialization",
         "cs/Program.cs"),
        (base, "javascript.browser.security.eval-detected", "web/server.ts"),
        (base, "javascript.browser.security.insecure-innerhtml", "web/page.js"),
        (base, "python.lang.security.deserialization.avoid-pickle", "app.py"),
        (base, "rust.lang.security.args", "rs/main.rs"),
        (base, "c.lang.security.insecure-use-gets-fn", "c/main.c"),
        ("0xdea-semgrep-rules", "rules.c.raptor-insecure-api-gets", "c/main.c"),
        ("0xdea-semgrep-rules", "rules.c.raptor-insecure-api-strcpy-strcat", "cpp/copy.cpp"),
    } <= found(result)
    languages = {finding.file: finding.language for finding in result.findings}
    assert languages["web/server.ts"] == "TypeScript" and languages["cpp/copy.cpp"] == "C++"


def test_image_contains_only_the_active_rule_sets(image_rulesets):
    # Inactive rule sets (by default semgrep-rules, whose license must be
    # accepted consciously) are not downloaded into the image.
    active = tuple(ruleset.name for ruleset in load_config(CONFIG_DIR).active)
    assert set(image_rulesets) == set(active), (
        "the image doesn't match config/opengrep.toml; rebuild it: "
        "docker compose -f docker/docker-compose.yml --profile tools build opengrep"
    )


def test_switching_to_semgrep_rules(workspace, runner, image_rulesets):
    root = project(workspace, {
        "requirements.txt": "",
        "app.py": "import pickle\n\n\ndef load(data):\n    return pickle.loads(data)\n",
    })
    result = analyse(root, workspace, runner, ("semgrep-rules",))
    if "semgrep-rules" not in image_rulesets:
        # Not active when the image was built: a clear hint instead of a scan.
        assert not result.complete
        assert "semgrep-rules not in the image" in result.tool_runs[0].notes[-1]
        return
    assert result.complete
    assert ("semgrep-rules", "python.lang.security.deserialization.avoid-pickle", "app.py") in found(result)
    assert [entry["name"] for entry in result.tool_runs[0].rulesets] == ["semgrep-rules"]

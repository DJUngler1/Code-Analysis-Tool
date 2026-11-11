"""config/opengrep.toml: active rule sets and their folders per language."""

import importlib.util
import re
import tomllib
from pathlib import Path

import pytest
from conftest import DEFAULT_RULESETS, opengrep_config

from scheduler.static_analysis.rulesets import RulesetError, load_config

REPOSITORY = Path(__file__).resolve().parents[2]
COMMIT = "f1d2b562b414783763fd02a6ed2736eaed622efa"


def write_config(tmp_path: Path, text: str) -> Path:
    (tmp_path / "opengrep.toml").write_text(text)
    return tmp_path


def test_repository_config(tmp_path):
    load_config(REPOSITORY / "config")  # the user's current choice is valid
    config = load_config(opengrep_config(tmp_path))
    assert [ruleset.name for ruleset in config.active] == list(DEFAULT_RULESETS)
    opengrep_rules, oxdea = config.active
    assert opengrep_rules.folders_for(["TypeScript"]) == ["javascript", "typescript"]
    assert opengrep_rules.folders_for(["JavaScript", "TypeScript"]) == ["javascript", "typescript"]
    assert opengrep_rules.folders_for(["C++"]) == []  # no C++ rules in the fork of semgrep-rules
    assert oxdea.folders_for(["C", "C++"]) == ["rules/c"]
    assert str(oxdea.image_path("rules/c")) == "/opt/code-analysis/opengrep/rulesets/0xdea-semgrep-rules/rules/c"


def test_every_defined_rule_set_can_be_activated(tmp_path):
    # Switching must work for every rule set the image contains.
    text = (REPOSITORY / "config" / "opengrep.toml").read_text()
    names = list(tomllib.loads(text)["rulesets"])
    assert "semgrep-rules" in names
    for name in names:
        switched = re.sub(r"(?m)^rulesets = \[.*\]$", f'rulesets = ["{name}"]', text)
        assert [ruleset.name for ruleset in load_config(write_config(tmp_path, switched)).active] == [name]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no rule set defined"),
        (f'[rulesets.a]\nrepository = "r"\ncommit = "{COMMIT}"\nfolders = {{ C = ["c"] }}\n',
         "must list the active rule sets"),
        (f'[opengrep]\nrulesets = ["b"]\n[rulesets.a]\nrepository = "r"\ncommit = "{COMMIT}"\n'
         'folders = { C = ["c"] }\n', "unknown rule set(s) in [opengrep] rulesets: b (defined: a)"),
        ('[opengrep]\nrulesets = ["a"]\n[rulesets.a]\nrepository = "r"\ncommit = "main"\nfolders = { C = ["c"] }\n',
         "full 40-character commit hash"),
        (f'[opengrep]\nrulesets = ["a"]\n[rulesets.a]\nrepository = "r"\ncommit = "{COMMIT}"\n'
         'folders = { Go = ["go"] }\n', "unknown language 'Go'"),
        (f'[opengrep]\nrulesets = ["a"]\n[rulesets.a]\nrepository = "r"\ncommit = "{COMMIT}"\n'
         'folders = { C = ["../c"] }\n', "must stay inside the repository"),
        ("[opengrep\n", "cannot be read"),
    ],
)
def test_invalid_config(tmp_path, text, message):
    with pytest.raises(RulesetError, match=re.escape(message)):
        load_config(write_config(tmp_path, text))


def test_missing_config(tmp_path):
    with pytest.raises(RulesetError, match="opengrep.toml is missing"):
        load_config(tmp_path)
    with pytest.raises(RulesetError, match="CONFIG_DIR"):
        load_config(None)


def test_build_script_accepts_the_repository_config(tmp_path):
    # The image build reads the same file with its own, smaller check.
    script = REPOSITORY / "docker" / "static-analysis" / "opengrep" / "fetch_rulesets.py"
    spec = importlib.util.spec_from_file_location("fetch_rulesets", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rulesets = tomllib.loads((REPOSITORY / "config" / "opengrep.toml").read_text())["rulesets"]
    assert all(module.check_definition(name, table) is None for name, table in rulesets.items())
    # Only active rule sets are downloaded: semgrep-rules only after activating it.
    config = tomllib.loads((opengrep_config(tmp_path) / "opengrep.toml").read_text())
    assert module.active_rulesets(config) == list(DEFAULT_RULESETS)
    assert "semgrep-rules" not in module.active_rulesets(config)
    with pytest.raises(ValueError, match="unknown rule set"):
        module.active_rulesets({"opengrep": {"rulesets": ["x"]}, "rulesets": {}})
    assert module.check_definition("x", {"repository": "r", "commit": "main", "folders": {"C": ["c"]}})
    assert module.is_rule_file(Path("python/flask/a.yaml"))
    assert not module.is_rule_file(Path("python/flask/a.test.yaml"))
    assert not module.is_rule_file(Path("python/flask/a.py"))
    assert not module.is_rule_file(Path(".github/workflows/x.yml"))
    assert len(module.RULE_ID.findall("rules:\n- id: a\n  pattern: x\n- id: b\n")) == 2
    assert len(module.RULE_ID.findall("rules:\n  - id: a\n    metadata:\n      - id: nested\n")) == 1

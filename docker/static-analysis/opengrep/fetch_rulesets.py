"""Build step of the OpenGrep image: fetch and check the active rule sets of config/opengrep.toml.

Usage: python3 fetch_rulesets.py <opengrep.toml> <target folder>

Only the rule sets listed in `[opengrep] rulesets` are fetched. Rule sets that
are merely defined are not downloaded: fetching and loading rules already
counts as using them, and some licenses (Semgrep Rules License v1.0) must be
accepted consciously, by activating the rule set and rebuilding the image.

For every active rule set:
  1. fetch the repository at the pinned commit;
  2. copy the rule files (*.yaml, *.yml; no *.test.yaml) of the configured
     folders, without the excluded files, and the license and readme files
     (the licenses require keeping their notices);
  3. let OpenGrep load all rules offline; any rule it rejects fails the build.

Results in the target folder:
  rulesets/<name>/        the rules, at their paths in the repository
  rulesets.json           name, repository, commit, license, rules per folder
and one line per rule set in /opt/code-analysis/versions.tsv
("ruleset:<name>\\t<commit>"), which the scheduler compares with the config.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

VERSIONS_FILE = Path("/opt/code-analysis/versions.tsv")
RULE_SUFFIXES = (".yaml", ".yml")
NOTICE_FILE = re.compile(r"^(license|licence|copying|notice|readme)(\..*)?$", re.IGNORECASE)
# One rule per "- id:" entry of the top-level `rules:` list.
RULE_ID = re.compile(r"^\s{0,4}-\s+id\s*:", re.MULTILINE)
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")


def main(config_path: str, target: str) -> int:
    config = tomllib.loads(Path(config_path).read_text(encoding="utf-8"))
    rulesets = config.get("rulesets") or {}
    if not rulesets:
        print(f"{config_path}: no [rulesets.<name>] defined", file=sys.stderr)
        return 1
    try:
        active = active_rulesets(config)
    except ValueError as exc:
        print(f"{config_path}: {exc}", file=sys.stderr)
        return 1
    inactive = [name for name in rulesets if name not in active]
    if inactive:
        print(f"not downloaded (not active): {', '.join(inactive)}")
    target_dir = Path(target)
    manifest = []
    for name in active:
        definition = rulesets[name]
        problem = check_definition(name, definition)
        if problem:
            print(f"{config_path}: rule set {name}: {problem}", file=sys.stderr)
            return 1
        destination = target_dir / "rulesets" / name
        with tempfile.TemporaryDirectory() as checkout:
            fetch(definition["repository"], definition["commit"], Path(checkout))
            folders = sorted({folder for folders in definition["folders"].values() for folder in folders})
            counts = copy_rules(Path(checkout), destination, folders, set(definition.get("exclude", [])))
        rejected = rejected_rules(destination, folders)
        if rejected:
            print(f"OpenGrep rejects rules of {name}; add them to `exclude` in opengrep.toml:", file=sys.stderr)
            for line in rejected:
                print(f"  {line}", file=sys.stderr)
            return 1
        manifest.append({
            "name": name,
            "repository": definition["repository"],
            "commit": definition["commit"],
            "license": definition.get("license", ""),
            "folders": definition["folders"],
            "excluded_files": sorted(definition.get("exclude", [])),
            "rules": counts,
        })
        print(f"{name}: " + ", ".join(f"{folder} {count['rules']} rules" for folder, count in counts.items()))
    (target_dir / "rulesets.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    with VERSIONS_FILE.open("a", encoding="utf-8") as versions:
        for entry in manifest:
            versions.write(f"ruleset:{entry['name']}\t{entry['commit']}\n")
    return 0


def active_rulesets(config: dict) -> list[str]:
    """Names in `[opengrep] rulesets`, without duplicates; each must be defined."""
    names = (config.get("opengrep") or {}).get("rulesets")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise ValueError("[opengrep] rulesets must list the active rule sets")
    defined = config.get("rulesets") or {}
    unknown = [name for name in names if name not in defined]
    if unknown:
        raise ValueError(f"unknown rule set(s) in [opengrep] rulesets: {', '.join(unknown)}")
    return list(dict.fromkeys(names))


def check_definition(name: str, definition: object) -> str | None:
    if not NAME.match(name):
        return "the name may only contain letters, digits, '.', '_' and '-'"
    if not isinstance(definition, dict):
        return "not a table"
    if not str(definition.get("repository", "")).strip():
        return "`repository` is missing"
    if not COMMIT.match(str(definition.get("commit", ""))):
        return "`commit` must be a full 40-character commit hash"
    folders = definition.get("folders")
    if not isinstance(folders, dict) or not folders:
        return "`folders` is missing (language -> list of rule folders)"
    for language, paths in folders.items():
        if not isinstance(paths, list) or not all(isinstance(path, str) and path for path in paths):
            return f"`folders.{language}` must be a list of folder names"
        if any(Path(path).is_absolute() or ".." in Path(path).parts for path in paths):
            return f"`folders.{language}` must stay inside the repository"
    return None


def fetch(repository: str, commit: str, checkout: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(checkout), *args], check=True)

    git("init", "--quiet")
    git("fetch", "--quiet", "--depth", "1", repository, commit)
    git("checkout", "--quiet", "FETCH_HEAD")


def copy_rules(checkout: Path, destination: Path, folders: list[str], exclude: set[str]) -> dict[str, dict]:
    destination.mkdir(parents=True)
    for notice in checkout.iterdir():
        if notice.is_file() and NOTICE_FILE.match(notice.name):
            shutil.copyfile(notice, destination / notice.name)
    counts: dict[str, dict] = {}
    for folder in folders:
        source = checkout / folder
        if not source.is_dir():
            raise SystemExit(f"rule folder {folder} does not exist in {checkout}")
        files = rules = 0
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(checkout)
            if not path.is_file() or not is_rule_file(relative) or relative.as_posix() in exclude:
                continue
            text = path.read_text(encoding="utf-8")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            files += 1
            rules += len(RULE_ID.findall(text))
        counts[folder] = {"files": files, "rules": rules}
    return counts


def is_rule_file(relative: Path) -> bool:
    name = relative.name.lower()
    if not name.endswith(RULE_SUFFIXES) or name.endswith((".test.yaml", ".test.yml")):
        return False
    return not any(part.startswith(".") for part in relative.parts)


def rejected_rules(destination: Path, folders: list[str]) -> list[str]:
    """Rules OpenGrep cannot load: scan an empty git repository with all of them, offline."""
    with tempfile.TemporaryDirectory() as empty:
        subprocess.run(["git", "-C", empty, "init", "--quiet"], check=True)
        report = Path(empty) / "report.json"
        command = ["opengrep", "scan", "--strict", "--disable-version-check", "--quiet",
                   f"--json-output={report}"]
        for folder in folders:
            command += ["--config", str(destination / folder)]
        result = subprocess.run([*command, empty], capture_output=True, text=True)
        errors = json.loads(report.read_text()).get("errors", []) if report.is_file() else []
        if result.returncode == 0 and not errors:
            return []
        lines = [f"{error.get('rule_id') or ''} {error.get('type')}: {str(error.get('message', '')).strip()}"
                 for error in errors]
        return lines or [f"opengrep exited with {result.returncode}: {result.stderr.strip()[-500:]}"]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))

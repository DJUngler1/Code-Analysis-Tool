"""OpenGrep's rule sets (config/opengrep.toml): which rules run for which language.

`[opengrep] rulesets` selects the rule sets a run uses. Only these active rule
sets are downloaded into the image code-analysis/opengrep when it is built
(docker/static-analysis/opengrep/fetch_rulesets.py); activating another one
needs a rebuild.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scheduler.acceptance.languages import SUPPORTED_LANGUAGES

CONFIG_FILE = "opengrep.toml"
# Where the image keeps the rule sets: <IMAGE_RULESETS_DIR>/<name>/<folder>.
IMAGE_RULESETS_DIR = PurePosixPath("/opt/code-analysis/opengrep/rulesets")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


class RulesetError(Exception):
    """config/opengrep.toml is missing or invalid."""


@dataclass(frozen=True)
class RuleSet:
    name: str
    repository: str
    commit: str
    license: str
    folders: Mapping[str, tuple[str, ...]]  # language of step 1 -> rule folders
    exclude: tuple[str, ...] = ()

    def folders_for(self, languages: Iterable[str]) -> list[str]:
        """Rule folders covering the languages, without duplicates, in config order."""
        wanted = set(languages)
        selected: list[str] = []
        for language, folders in self.folders.items():
            if language in wanted:
                selected += [folder for folder in folders if folder not in selected]
        return selected

    def image_path(self, folder: str) -> PurePosixPath:
        return IMAGE_RULESETS_DIR / self.name / folder


@dataclass(frozen=True)
class OpenGrepConfig:
    path: Path
    active: tuple[RuleSet, ...]


def load_config(config_dir: Path | None) -> OpenGrepConfig:
    if config_dir is None:
        raise RulesetError("no configuration folder: set CONFIG_DIR (docker-compose.yml mounts config/).")
    path = config_dir / CONFIG_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RulesetError(f"{path} is missing.") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RulesetError(f"{path} cannot be read: {exc}") from exc

    defined = data.get("rulesets")
    if not isinstance(defined, dict) or not defined:
        raise RulesetError(f"{path}: no rule set defined ([rulesets.<name>]).")
    names = (data.get("opengrep") or {}).get("rulesets")
    if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
        raise RulesetError(f"{path}: [opengrep] rulesets must list the active rule sets, e.g. [\"opengrep-rules\"].")
    unknown = [name for name in names if name not in defined]
    if unknown:
        raise RulesetError(
            f"{path}: unknown rule set(s) in [opengrep] rulesets: {', '.join(unknown)} "
            f"(defined: {', '.join(defined)})."
        )
    active = tuple(_ruleset(path, name, defined[name]) for name in dict.fromkeys(names))
    return OpenGrepConfig(path, active)


def _ruleset(path: Path, name: str, table: object) -> RuleSet:
    def fail(problem: str) -> RulesetError:
        return RulesetError(f"{path}: rule set {name}: {problem}")

    if not _NAME.match(name):
        raise fail("the name may only contain letters, digits, '.', '_' and '-'.")
    if not isinstance(table, dict):
        raise fail("not a table.")
    repository = str(table.get("repository", "")).strip()
    if not repository:
        raise fail("`repository` is missing.")
    commit = str(table.get("commit", "")).strip()
    if not _COMMIT.match(commit):
        raise fail("`commit` must be a full 40-character commit hash.")
    folders = table.get("folders")
    if not isinstance(folders, dict) or not folders:
        raise fail("`folders` is missing (language -> list of rule folders).")
    parsed: dict[str, tuple[str, ...]] = {}
    for language, paths in folders.items():
        if language not in SUPPORTED_LANGUAGES:
            raise fail(f"unknown language {language!r} in `folders` (known: {', '.join(SUPPORTED_LANGUAGES)}).")
        if not isinstance(paths, list) or not all(isinstance(folder, str) and folder for folder in paths):
            raise fail(f"`folders.{language}` must be a list of folder names.")
        if any(PurePosixPath(folder).is_absolute() or ".." in PurePosixPath(folder).parts for folder in paths):
            raise fail(f"`folders.{language}` must stay inside the repository.")
        parsed[language] = tuple(paths)
    exclude = table.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(file, str) for file in exclude):
        raise fail("`exclude` must be a list of rule files.")
    return RuleSet(name, repository, commit, str(table.get("license", "")), parsed, tuple(exclude))

"""The tool's configuration file, code-analysis.toml in the repository root.

Each step reads its own section ([opengrep], [tests], [llm]) through this
module, so errors name the file and the key the same way everywhere.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

CONFIG_FILE = "code-analysis.toml"


class ConfigFileError(Exception):
    """code-analysis.toml is missing, unreadable or has an invalid value."""


def read_config(path: Path | None) -> dict:
    """The whole file as a dict; {} when there is no configuration file (outside Docker, tests)."""
    if path is None:
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigFileError(f"{path} is missing. In the container it comes from {CONFIG_FILE} in the repository "
                              "root (docker/docker-compose.yml): check that the file is there. If it is, the "
                              "container still uses an old mount; recreate it from the repository root with "
                              "`docker compose -f docker/docker-compose.yml up -d --force-recreate`.") from exc
    except OSError as exc:
        raise ConfigFileError(f"{path} cannot be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"{path} is not valid TOML: {exc}") from exc


def section(data: dict, path: Path | None, name: str) -> dict:
    """The table `name` ("tests", "llm.report", ...); {} if absent."""
    table: object = data
    for key in name.split("."):
        table = table.get(key, {}) if isinstance(table, dict) else None
    if not isinstance(table, dict):
        raise ConfigFileError(f"{path}: [{name}] must be a table.")
    return table


def whole_number(table: dict, key: str, default: int, path: Path | None, name: str, *, minimum: int = 0) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        hint = ", 0 for no limit" if key.startswith("time_limit") else ""
        raise ConfigFileError(f"{path}: {name}.{key} must be a whole number of at least {minimum}{hint}.")
    return value


def number(table: dict, key: str, default: float, path: Path | None, name: str, *, minimum: float = 0.0,
           maximum: float | None = None) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum \
            or (maximum is not None and value > maximum):
        limit = f" and at most {maximum}" if maximum is not None else ""
        raise ConfigFileError(f"{path}: {name}.{key} must be a number of at least {minimum}{limit}.")
    return float(value)


def text(table: dict, key: str, default: str, path: Path | None, name: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigFileError(f"{path}: {name}.{key} must be a non-empty string.")
    return value.strip()


def flag(table: dict, key: str, default: bool, path: Path | None, name: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigFileError(f"{path}: {name}.{key} must be true or false.")
    return value

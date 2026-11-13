"""Settings of step 5 from config/tests.toml (read at every run)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "tests.toml"


class ConfigError(Exception):
    """config/tests.toml is missing or invalid."""


MODES = ("sample", "full")


@dataclass(frozen=True)
class MutationConfig:
    mode: str = "sample"  # "sample": seeded random sample of the covered source files; "full": all source files
    sample_files: int = 20  # per suite, in mode "sample"
    seed: int = 1
    time_limit_minutes: int = 60  # per mutation run; 0: no limit

    @property
    def time_limit_seconds(self) -> float | None:
        return self.time_limit_minutes * 60 if self.time_limit_minutes else None


@dataclass(frozen=True)
class TestsConfig:
    __test__ = False  # not a pytest test class

    # Network for the test runs; downloading the dependencies always has network.
    network: bool = False
    time_limit_minutes: int = 60  # per test run; 0: no limit
    mutation: MutationConfig = MutationConfig()

    @property
    def time_limit_seconds(self) -> float | None:
        return self.time_limit_minutes * 60 if self.time_limit_minutes else None


def load_config(config_dir: Path | None) -> TestsConfig:
    """The settings; the defaults when there is no config folder (outside Docker, tests)."""
    if config_dir is None:
        return TestsConfig()
    path = config_dir / CONFIG_FILE
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"{path} cannot be read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    tests = data.get("tests", {})
    if not isinstance(tests, dict):
        raise ConfigError(f"{path}: [tests] must be a table")
    network = tests.get("network", TestsConfig.network)
    if not isinstance(network, bool):
        raise ConfigError(f"{path}: tests.network must be true or false")
    minutes = _count(tests, "time_limit_minutes", TestsConfig.time_limit_minutes, path, "tests")
    mutation = data.get("mutation", {})
    if not isinstance(mutation, dict):
        raise ConfigError(f"{path}: [mutation] must be a table")
    mode = mutation.get("mode", MutationConfig.mode)
    if mode not in MODES:
        raise ConfigError(f"{path}: mutation.mode must be \"sample\" or \"full\"")
    sample_files = _count(mutation, "sample_files", MutationConfig.sample_files, path, "mutation", minimum=1)
    seed = _count(mutation, "seed", MutationConfig.seed, path, "mutation")
    mutation_minutes = _count(mutation, "time_limit_minutes", MutationConfig.time_limit_minutes, path, "mutation")
    return TestsConfig(network=network, time_limit_minutes=minutes,
                       mutation=MutationConfig(mode, sample_files, seed, mutation_minutes))


def _count(table: dict, key: str, default: int, path: Path, section: str, *, minimum: int = 0) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        hint = ", 0 for no limit" if key == "time_limit_minutes" else ""
        raise ConfigError(f"{path}: {section}.{key} must be a whole number of at least {minimum}{hint}")
    return value

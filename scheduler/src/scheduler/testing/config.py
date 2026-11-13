"""Settings of step 5 from config/tests.toml (read at every run)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = "tests.toml"


class ConfigError(Exception):
    """config/tests.toml is missing or invalid."""


@dataclass(frozen=True)
class TestsConfig:
    __test__ = False  # not a pytest test class

    # Network for the test runs; downloading the dependencies always has network.
    network: bool = False
    time_limit_minutes: int = 60  # per test run; 0: no limit

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
    minutes = tests.get("time_limit_minutes", TestsConfig.time_limit_minutes)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
        raise ConfigError(f"{path}: tests.time_limit_minutes must be a whole number of minutes, 0 for no limit")
    return TestsConfig(network=network, time_limit_minutes=minutes)

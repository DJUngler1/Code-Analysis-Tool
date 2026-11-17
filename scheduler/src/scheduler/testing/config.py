"""Settings of step 5 from code-analysis.toml, [tests] and [tests.mutation] (read at every run)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scheduler.tool_config import ConfigFileError, flag, read_config, section, whole_number

class ConfigError(Exception):
    """The [tests] section of code-analysis.toml is invalid."""


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


def load_config(path: Path | None) -> TestsConfig:
    """[tests] and [tests.mutation] of code-analysis.toml; the defaults without a file (outside Docker, tests)."""
    try:
        data = read_config(path)
        tests = section(data, path, "tests")
        mutation = section(data, path, "tests.mutation")
        network = flag(tests, "network", TestsConfig.network, path, "tests")
        minutes = whole_number(tests, "time_limit_minutes", TestsConfig.time_limit_minutes, path, "tests")
        mode = mutation.get("mode", MutationConfig.mode)
        if mode not in MODES:
            raise ConfigFileError(f"{path}: tests.mutation.mode must be \"sample\" or \"full\".")
        sample_files = whole_number(mutation, "sample_files", MutationConfig.sample_files, path, "tests.mutation",
                                    minimum=1)
        seed = whole_number(mutation, "seed", MutationConfig.seed, path, "tests.mutation")
        mutation_minutes = whole_number(mutation, "time_limit_minutes", MutationConfig.time_limit_minutes, path,
                                        "tests.mutation")
    except ConfigFileError as exc:
        raise ConfigError(str(exc)) from exc
    return TestsConfig(network=network, time_limit_minutes=minutes,
                       mutation=MutationConfig(mode, sample_files, seed, mutation_minutes))

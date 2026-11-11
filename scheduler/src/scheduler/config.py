"""Runtime settings, read from the environment set by docker/docker-compose.yml."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434"
# Network of the compose deployment; the tool containers join it.
DEFAULT_TOOLS_NETWORK = "code-analysis-net"
# The repository's config/ folder, used when running from the source tree.
REPOSITORY_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


@dataclass(frozen=True)
class Settings:
    # Host folder mounted into the container. Projects must be inside it.
    # None when running outside Docker: then any folder is allowed.
    projects_dir: Path | None
    # Home directory of the host user, used to expand "~" in entered paths.
    host_home: Path | None
    ollama_base_url: str
    # Host folder for the results of each run (step 2 onwards). Mounted at the
    # same path, so it can be passed on to the tool containers unchanged.
    output_dir: Path | None = None
    tools_network: str = DEFAULT_TOOLS_NETWORK
    # Tool configuration (opengrep.toml). docker-compose.yml mounts the
    # repository's config/ folder and sets CONFIG_DIR.
    config_dir: Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        projects_dir = env.get("PROJECTS_DIR", "").strip()
        host_home = env.get("HOST_HOME", "").strip()
        ollama_base_url = env.get("OLLAMA_BASE_URL", "").strip() or DEFAULT_OLLAMA_BASE_URL
        output_dir = env.get("OUTPUT_DIR", "").strip()
        tools_network = env.get("TOOLS_NETWORK", "").strip() or DEFAULT_TOOLS_NETWORK
        config_dir = env.get("CONFIG_DIR", "").strip()
        return cls(
            projects_dir=Path(projects_dir).resolve() if projects_dir else None,
            host_home=Path(host_home) if host_home else None,
            ollama_base_url=ollama_base_url.rstrip("/"),
            output_dir=Path(output_dir).resolve() if output_dir else None,
            tools_network=tools_network,
            config_dir=Path(config_dir) if config_dir else _repository_config_dir(),
        )


def _repository_config_dir() -> Path | None:
    return REPOSITORY_CONFIG_DIR if REPOSITORY_CONFIG_DIR.is_dir() else None

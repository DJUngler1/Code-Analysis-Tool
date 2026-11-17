"""Runtime settings, read from the environment set by docker/docker-compose.yml."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scheduler.tool_config import ConfigFileError, read_config

DEFAULT_OLLAMA_BASE_URL = "http://host.docker.internal:11434"
# Network of the compose deployment; the tool containers join it.
DEFAULT_TOOLS_NETWORK = "code-analysis-net"
# SonarQube server of the compose deployment (service name in the network).
DEFAULT_SONARQUBE_URL = "http://sonarqube:9000"
# The repository's code-analysis.toml, used when running from the source tree.
REPOSITORY_CONFIG_FILE = Path(__file__).resolve().parents[3] / "code-analysis.toml"


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
    # The tool's configuration file (code-analysis.toml). docker-compose.yml
    # mounts the repository's file and sets CONFIG_FILE.
    config_file: Path | None = None
    # SonarQube (step 3): server URL for the scheduler and for the scanner
    # containers (the same inside the compose network), and the password of
    # the SonarQube user "admin" (docker/.env).
    sonarqube_url: str = DEFAULT_SONARQUBE_URL
    sonarqube_scanner_url: str | None = None
    sonarqube_admin_password: str | None = None
    # Step 4: token of the free ConanCenter audit service (conan audit); optional.
    conan_audit_token: str | None = None

    @property
    def scanner_url(self) -> str:
        return self.sonarqube_scanner_url or self.sonarqube_url

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        projects_dir = env.get("PROJECTS_DIR", "").strip()
        host_home = env.get("HOST_HOME", "").strip()
        config_file = env.get("CONFIG_FILE", "").strip()
        config_path = Path(config_file) if config_file else _repository_config_file()
        # OLLAMA_BASE_URL (docker/.env) overrides [llm] base_url of the configuration file.
        ollama_base_url = env.get("OLLAMA_BASE_URL", "").strip() or _configured_base_url(config_path) \
            or DEFAULT_OLLAMA_BASE_URL
        output_dir = env.get("OUTPUT_DIR", "").strip()
        tools_network = env.get("TOOLS_NETWORK", "").strip() or DEFAULT_TOOLS_NETWORK
        sonarqube_url = env.get("SONARQUBE_URL", "").strip() or DEFAULT_SONARQUBE_URL
        return cls(
            projects_dir=Path(projects_dir).resolve() if projects_dir else None,
            host_home=Path(host_home) if host_home else None,
            ollama_base_url=ollama_base_url.rstrip("/"),
            output_dir=Path(output_dir).resolve() if output_dir else None,
            tools_network=tools_network,
            config_file=config_path,
            sonarqube_url=sonarqube_url.rstrip("/"),
            sonarqube_scanner_url=(env.get("SONARQUBE_SCANNER_URL", "").strip().rstrip("/") or None),
            sonarqube_admin_password=env.get("SONARQUBE_ADMIN_PASSWORD") or None,
            conan_audit_token=env.get("CONAN_AUDIT_TOKEN", "").strip() or None,
        )


def _repository_config_file() -> Path | None:
    return REPOSITORY_CONFIG_FILE if REPOSITORY_CONFIG_FILE.is_file() else None


def _configured_base_url(path: Path | None) -> str | None:
    """[llm] base_url, if the file has a valid one; errors are reported by the step that reads [llm]."""
    try:
        llm = read_config(path).get("llm", {})
    except ConfigFileError:
        return None
    value = llm.get("base_url") if isinstance(llm, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None

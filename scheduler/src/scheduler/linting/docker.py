"""Start the tool containers through the Docker socket.

Each tool command runs in a fresh container (`docker run --rm`) of the
tool's image. Project and run folder are mounted at the same absolute paths
as on the host: the scheduler sees them at those paths too, so it passes them
on unchanged.
"""

from __future__ import annotations

import os
import subprocess
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from scheduler.linting.model import CommandResult

DOCKER_SOCKET = Path("/var/run/docker.sock")
VERSIONS_FILE = "/opt/code-analysis/versions.tsv"
CONTAINER_HOME = "/tmp/home"
BUILD_HINT = "docker compose -f docker/docker-compose.yml --profile tools build"
QUICK_TIMEOUT_SECONDS = 60


class DockerError(Exception):
    """Docker is not usable or a tool image is missing."""


class ToolRunner(Protocol):
    """What the language modules need from Docker; tests provide a fake."""

    def check_available(self) -> None: ...

    def ensure_image(self, image: str) -> None: ...

    def image_versions(self, image: str) -> Mapping[str, str]: ...

    def run(
        self,
        image: str,
        command: Sequence[str],
        *,
        workdir: Path,
        mounts: Sequence[Path],
        env: Mapping[str, str] | None = None,
        timeout: float,
        read_only: Sequence[Path] = (),
        network: str | None = None,
    ) -> CommandResult: ...


def build_run_args(
    image: str,
    command: Sequence[str],
    *,
    name: str,
    workdir: Path,
    mounts: Sequence[Path],
    env: Mapping[str, str] | None,
    user: str,
    network: str,
    read_only: Sequence[Path] = (),
) -> list[str]:
    """Arguments of `docker run` for one tool command.

    `mounts` are writable, `read_only` mounts can't be changed by the tool.
    """
    args = [
        "docker", "run", "--rm", "--init",
        "--name", name,
        "--user", user,
        "--network", network,
        "--workdir", str(workdir),
        "--env", f"HOME={CONTAINER_HOME}",
    ]
    for key, value in (env or {}).items():
        args += ["--env", f"{key}={value}"]
    writable = _unique_mounts(mounts)
    # A writable folder inside a read-only one stays a mount of its own.
    protected = [path for path in _unique_mounts(read_only) if not _inside(path, writable)]
    for path in sorted([*writable, *protected], key=lambda path: (len(path.parts), str(path))):
        option = f"type=bind,source={path},target={path}"
        args += ["--mount", option if path in writable else f"{option},readonly"]
    return [*args, image, *command]


def _inside(path: Path, folders: Sequence[Path]) -> bool:
    return any(path == folder or path.is_relative_to(folder) for folder in folders)


def _unique_mounts(mounts: Sequence[Path]) -> list[Path]:
    """Drop mounts that lie inside another mount; Docker rejects duplicate targets."""
    resolved = sorted({Path(path) for path in mounts}, key=lambda path: (len(path.parts), str(path)))
    kept: list[Path] = []
    for path in resolved:
        if not _inside(path, kept):
            kept.append(path)
    return kept


class DockerRunner:
    def __init__(self, network: str, user: str | None = None) -> None:
        self.network = network
        self.user = user or f"{os.getuid()}:{os.getgid()}"
        self._versions: dict[str, dict[str, str]] = {}

    def check_available(self) -> None:
        try:
            result = _docker("version", "--format", "{{.Server.Version}}")
        except FileNotFoundError as exc:
            raise DockerError("the docker command is not installed in the scheduler image.") from exc
        except subprocess.TimeoutExpired as exc:
            raise DockerError("Docker did not answer.") from exc
        if result.returncode == 0:
            return
        message = result.stderr.strip() or result.stdout.strip()
        if "permission denied" in message.lower():
            raise DockerError(_permission_hint())
        raise DockerError(f"Docker is not reachable: {message}")

    def ensure_image(self, image: str) -> None:
        result = _docker("image", "inspect", "--format", "{{.Id}}", image)
        if result.returncode != 0:
            raise DockerError(f"the tool image {image} is missing. Build the tool images with:\n  {BUILD_HINT}")

    def image_versions(self, image: str) -> Mapping[str, str]:
        """Tool versions recorded in the image at build time (tool -> version)."""
        if image not in self._versions:
            result = _docker("run", "--rm", "--network", "none", image, "cat", VERSIONS_FILE)
            versions: dict[str, str] = {}
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    tool, _, version = line.partition("\t")
                    if tool and version:
                        versions[tool.strip()] = version.strip()
            self._versions[image] = versions
        return self._versions[image]

    def run(
        self,
        image: str,
        command: Sequence[str],
        *,
        workdir: Path,
        mounts: Sequence[Path],
        env: Mapping[str, str] | None = None,
        timeout: float,
        read_only: Sequence[Path] = (),
        network: str | None = None,
    ) -> CommandResult:
        """Run one command; `network` overrides the runner's network (e.g. "none")."""
        name = f"code-analysis-{image.rsplit('/', 1)[-1].split(':', 1)[0]}-{uuid.uuid4().hex[:8]}"
        args = build_run_args(
            image, command, name=name, workdir=workdir, mounts=mounts, env=env, user=self.user,
            network=network or self.network, read_only=read_only,
        )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=QUICK_TIMEOUT_SECONDS)
            return CommandResult(
                tuple(command),
                None,
                _text(exc.stdout),
                _text(exc.stderr) + f"\n[code-analysis] timed out after {timeout:.0f} s, container killed",
                time.monotonic() - started,
                timed_out=True,
            )
        return CommandResult(
            tuple(command), completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started
        )


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=QUICK_TIMEOUT_SECONDS,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _permission_hint() -> str:
    try:
        gid = DOCKER_SOCKET.stat().st_gid
    except OSError:
        return "no permission to use the Docker socket."
    return (
        "no permission to use the Docker socket. Set DOCKER_SOCKET_GID="
        f"{gid} in docker/.env and restart the container:\n"
        "  docker compose -f docker/docker-compose.yml up -d"
    )

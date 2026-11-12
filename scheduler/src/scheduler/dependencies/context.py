"""What the parts of step 4 share: the project, its copy, the run folder, Docker."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scheduler.acceptance import AcceptanceResult
from scheduler.config import Settings
from scheduler.linting.docker import ToolRunner
from scheduler.linting.model import CommandResult

TIMEOUT_SECONDS = 30 * 60


@dataclass
class DependencyContext:
    project: Path
    workspace: Path  # copy of the project: lock files are generated here
    step_dir: Path  # <run>/step4-dependencies
    acceptance: AcceptanceResult
    runner: ToolRunner
    settings: Settings
    out: Callable[[str], None] = print

    def hits(self) -> list[tuple[str, Path]]:
        """(package manager, manifest relative to the project) of every language, as found by step 1."""
        found: list[tuple[str, Path]] = []
        for hits in self.acceptance.package_managers.values():
            for hit in hits:
                if (hit.manager, hit.manifest) not in found:
                    found.append((hit.manager, hit.manifest))
        return found

    def run(self, image: str, command: Sequence[str], *, folder: Path, log_dir: Path, label: str,
            env: Mapping[str, str] | None = None, timeout: float = TIMEOUT_SECONDS) -> CommandResult:
        """Run a command in `folder` of the copy; stdout and stderr go to `log_dir`.

        Only the step folder is mounted (copy and outputs), never the project.
        Containers join the tools network: resolving and auditing need the internet.
        """
        result = self.runner.run(image, command, workdir=self.workspace / folder, mounts=[self.step_dir],
                                 env=env, timeout=timeout)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"{label}.stdout.txt").write_text(result.stdout, encoding="utf-8")
        (log_dir / f"{label}.stderr.txt").write_text(result.stderr, encoding="utf-8")
        return result

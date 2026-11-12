"""Lock files for OSV-Scanner: use committed ones, generate missing ones in the copy.

OSV-Scanner reads lock files, not plain manifests (exceptions: Maven's pom.xml,
resolved transitively, and requirements.txt / packages.config / .csproj with
their direct dependencies). A missing lock file is created with the package
manager's own lock command, which resolves the dependencies without
installing them or running project scripts where the tool allows it.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import Path

from scheduler.dependencies.context import DependencyContext
from scheduler.dependencies.model import SCOPES, Coverage, DependencyFinding
from scheduler.linting.languages.csharp import build_targets
from scheduler.linting.session import last_lines, slug

NODE_IMAGE = "code-analysis/lint-node:latest"
RUST_IMAGE = "code-analysis/lint-rust:latest"
DOTNET_IMAGE = "code-analysis/lint-dotnet:latest"
CPP_IMAGE = "code-analysis/lint-cpp:latest"
JAVA_IMAGE = "code-analysis/sonar-scanner:latest"  # JDK 21 and Gradle
PYTHON_IMAGE = "code-analysis/audit-python:latest"
LOCKFILES_DIR = "lockfiles"
LOG_DIR = "lockfile-logs"

GRADLE_MARKER = "code-analysis-not-locked: "  # printed by the init script, one line per configuration
GRADLE_INIT_SCRIPT = """\
// Code-Analysis-Tool: lock every configuration of every project and resolve its
// dependency graph (no artifact download), so that --write-locks writes
// gradle.lockfile for OSV-Scanner. Gradle does not lock a configuration that
// cannot be resolved completely: it is reported and the others are still locked
// (one module that needs e.g. the Android SDK must not stop the whole build).
allprojects {
    dependencyLocking { lockAllConfigurations() }
    tasks.register('codeAnalysisLock') {
        notCompatibleWithConfigurationCache('resolves all configurations')
        doLast {
            configurations.findAll { it.canBeResolved }.each { configuration ->
                def problem
                try {
                    def unresolved = configuration.incoming.resolutionResult.allDependencies
                        .findAll { it instanceof org.gradle.api.artifacts.result.UnresolvedDependencyResult }
                        .collect { it.attempted.displayName }.unique()
                    problem = unresolved ? "unresolved ${unresolved.join(', ')}" : null
                } catch (Exception e) {
                    problem = e.message?.readLines()?.first()
                }
                if (problem) {
                    logger.quiet("code-analysis-not-locked: ${project.path} ${configuration.name}: ${problem}")
                }
            }
        }
    }
}
"""
MAX_NOTES = 5

NODE_LOCKFILES = {
    "npm": ("package-lock.json", "npm-shrinkwrap.json"),
    "Yarn": ("yarn.lock",),
    "pnpm": ("pnpm-lock.yaml",),
    "Bun": ("bun.lock", "bun.lockb"),
}
PYTHON_LOCKFILES = {"Poetry": "poetry.lock", "uv": "uv.lock", "PDM": "pdm.lock", "Pipenv": "Pipfile.lock"}
AUDIT_TOOLS = {
    "npm": "npm audit", "Yarn": "yarn audit", "pnpm": "pnpm audit", "Bun": "bun audit",
    "Cargo": "cargo audit", "NuGet": "dotnet package list --vulnerable",
    "pip": "pip-audit", "Poetry": "pip-audit", "uv": "pip-audit", "PDM": "pip-audit", "Pipenv": "pip-audit",
    "Conan": "conan audit",
}


@dataclass(frozen=True)
class Unit:
    """One thing to lock and audit: a package manager in a folder (relative to the project)."""

    manager: str
    folder: Path
    manifest: Path  # the manifest of step 1 (for C#: the build target)


def units(ctx: DependencyContext) -> list[Unit]:
    """Manifests of step 1, reduced to the folders the package managers work in."""
    hits = ctx.hits()
    found: list[Unit] = []

    def key(unit: Unit) -> tuple:
        # pip: every requirements file on its own; others: one unit per folder.
        return (unit.manager, unit.folder, unit.manifest if unit.manager == "pip" else None)

    def add(unit: Unit) -> None:
        if all(key(unit) != key(other) for other in found):
            found.append(unit)

    for manager, manifest in hits:
        if manager in NODE_LOCKFILES:
            lock_folder = _nearest_with(ctx.workspace, manifest.parent, NODE_LOCKFILES[manager])
            add(Unit(manager, lock_folder if lock_folder is not None else _topmost(manifest, hits, manager),
                     manifest))
        elif manager in ("Cargo", "Gradle"):
            add(Unit(manager, _topmost(manifest, hits, manager), manifest))
        elif manager == "NuGet":
            continue  # by build target, below
        else:
            add(Unit(manager, manifest.parent, manifest))
    nuget = [manifest for manager, manifest in hits if manager == "NuGet"]
    for target in build_targets(nuget):
        add(Unit("NuGet", target.parent, target))
    return found


def prepare_lockfiles(ctx: DependencyContext, conan_token: bool) -> list[Coverage]:
    """Check each unit, generate missing lock files in the copy; returns the coverage."""
    coverage: list[Coverage] = []
    for index, unit in enumerate(units(ctx), start=1):
        entry = _coverage(ctx, unit, conan_token)
        if entry.lockfile_state == "missing":
            _generate(ctx, unit, entry, index)
        coverage.append(entry)
    return coverage


def _coverage(ctx: DependencyContext, unit: Unit, conan_token: bool) -> Coverage:
    manager, folder = unit.manager, unit.folder
    audit = AUDIT_TOOLS.get(manager)
    if manager == "Conan" and not conan_token:
        audit = None
    entry = Coverage(manager, unit.manifest.as_posix(), None, "missing", True, audit)
    if manager in NODE_LOCKFILES:
        lockfile = _existing(ctx.workspace, folder, NODE_LOCKFILES[manager])
        if lockfile == folder / "bun.lockb":
            entry.osv = False
            entry.notes.append("bun.lockb (binary) is not read by OSV-Scanner; bun audit covers it")
        _set_lockfile(entry, lockfile, folder / NODE_LOCKFILES[manager][0])
    elif manager == "Cargo":
        _set_lockfile(entry, _existing(ctx.workspace, folder, ("Cargo.lock",)), folder / "Cargo.lock")
    elif manager == "Gradle":
        _set_lockfile(entry, _existing(ctx.workspace, folder, ("gradle.lockfile",)), folder / "gradle.lockfile")
    elif manager == "Conan":
        _set_lockfile(entry, _existing(ctx.workspace, folder, ("conan.lock",)), folder / "conan.lock")
        entry.notes.append("the OSV database has no ConanCenter ecosystem: OSV-Scanner reads conan.lock "
                           "but finds no vulnerabilities for Conan packages")
    elif manager in PYTHON_LOCKFILES:
        _set_lockfile(entry, _existing(ctx.workspace, folder, (PYTHON_LOCKFILES[manager],)),
                      folder / PYTHON_LOCKFILES[manager])
        if manager in ("uv", "PDM") and entry.lockfile_state == "missing":
            entry.lockfile_state, entry.osv = "failed", False  # detected by their lock file: should not happen
    elif manager == "NuGet":
        locks = _nuget_projects(ctx.workspace, unit.manifest)
        entry.lockfile = "packages.lock.json"
        entry.lockfile_state = "committed" if locks and all(lock.is_file() for lock in locks) else "missing"
    elif manager == "Maven":
        entry.lockfile, entry.lockfile_state = unit.manifest.as_posix(), "not needed"
        entry.notes.append("OSV-Scanner resolves pom.xml transitively (deps.dev)")
        entry.audit = None
    elif manager == "pip":
        if unit.manifest.name.endswith(".txt"):
            entry.lockfile, entry.lockfile_state = unit.manifest.as_posix(), "not needed"
            entry.notes.append("OSV-Scanner reads requirements files best effort: for unpinned requirements "
                               "it picks a version that may not be the one installed")
        else:
            entry.lockfile_state, entry.osv = "not supported", False
            entry.notes.append(f"OSV-Scanner can't read {unit.manifest.name}; pip-audit resolves it")
    else:  # vcpkg, Conda
        entry.lockfile_state, entry.osv, entry.audit = "not supported", False, None
        entry.notes.append(f"neither OSV-Scanner nor an audit covers {manager}")
    return entry


def _set_lockfile(entry: Coverage, existing: Path | None, wanted: Path) -> None:
    entry.lockfile = (existing or wanted).as_posix()
    entry.lockfile_state = "committed" if existing else "missing"


def _generate(ctx: DependencyContext, unit: Unit, entry: Coverage, index: int) -> None:
    command, image = _lock_command(ctx, unit)
    if command is None:
        entry.lockfile_state, entry.osv = "failed", False
        entry.notes.append(f"no way to generate the lock file of {unit.manager}")
        return
    before = _lockfiles_in(ctx.workspace)
    label = f"{index:02d}-{slug(unit.manager)}"
    ctx.out(f"  {'lock file':<22} {unit.manager} ({unit.manifest.as_posix()}) ...")
    result = ctx.run(image, command, folder=unit.folder, log_dir=ctx.step_dir / LOG_DIR, label=label)
    created = sorted(_lockfiles_in(ctx.workspace) - before)
    if result.exit_code != 0 or not created:
        entry.lockfile_state, entry.osv = "failed", False
        entry.notes.append(f"generating the lock file failed (exit code {result.exit_code}): "
                           f"{_error_text(result.stderr or result.stdout)}")
        return
    entry.lockfile_state = "generated"
    not_locked = [line.split(GRADLE_MARKER, 1)[1].strip() for line in result.stdout.splitlines()
                  if GRADLE_MARKER in line]
    if not_locked:
        entry.lockfile_state = "partial"
        more = f"; {len(not_locked) - MAX_NOTES} more in {LOG_DIR}/{label}.stdout.txt" \
            if len(not_locked) > MAX_NOTES else ""
        entry.notes.append(f"{len(not_locked)} Gradle configuration(s) could not be resolved completely; Gradle "
                           f"did not lock them, so their dependencies are not checked: "
                           f"{' | '.join(not_locked[:MAX_NOTES])}{more}")
    for path in created:
        target = ctx.step_dir / LOCKFILES_DIR / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ctx.workspace / path, target)
    if unit.manager == "NuGet":
        entry.lockfile = ", ".join(path.as_posix() for path in created)


def _error_text(output: str) -> str:
    """Gradle's "What went wrong" section, else the last lines of the output."""
    lines = output.splitlines()
    if "* What went wrong:" in lines:
        start = lines.index("* What went wrong:") + 1
        end = next((index for index in range(start, len(lines)) if lines[index].startswith("* ")), len(lines))
        section = " ".join(line.strip() for line in lines[start:end] if line.strip())
        if section:
            return section
    return last_lines(output)


def _lock_command(ctx: DependencyContext, unit: Unit) -> tuple[list[str] | None, str]:
    manager, directory = unit.manager, ctx.workspace / unit.folder
    if manager == "npm":
        return ["npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"], NODE_IMAGE
    if manager == "Cargo":
        return ["cargo", "generate-lockfile"], RUST_IMAGE
    if manager == "NuGet":
        return ["dotnet", "restore", unit.manifest.name, "--use-lock-file"], DOTNET_IMAGE
    if manager == "Gradle":
        script = ctx.step_dir / LOG_DIR / "gradle-lock-all.gradle"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(GRADLE_INIT_SCRIPT, encoding="utf-8")
        launcher = ["sh", "./gradlew"] if (directory / "gradlew").is_file() else ["gradle"]
        return [*launcher, "--no-daemon", "-q", "--init-script", str(script), "codeAnalysisLock", "--write-locks"], \
            JAVA_IMAGE
    if manager == "Conan":
        return ["sh", "-c", "conan profile detect >/dev/null 2>&1; conan lock create ."], CPP_IMAGE
    if manager == "Poetry":
        return ["poetry", "lock", "--no-interaction"], PYTHON_IMAGE
    if manager == "Pipenv":
        return ["pipenv", "lock"], PYTHON_IMAGE
    return None, ""


def _lockfiles_in(workspace: Path) -> set[Path]:
    names = {"package-lock.json", "Cargo.lock", "packages.lock.json", "gradle.lockfile", "conan.lock",
             "poetry.lock", "Pipfile.lock"}
    return {path.relative_to(workspace) for path in workspace.rglob("*")
            if path.name in names and ".git" not in path.parts and "node_modules" not in path.parts}


def _existing(workspace: Path, folder: Path, names: Iterable[str]) -> Path | None:
    return next((folder / name for name in names if (workspace / folder / name).is_file()), None)


def _nearest_with(workspace: Path, folder: Path, names: Iterable[str]) -> Path | None:
    """The folder itself or the closest parent that has one of the files (npm/Yarn/pnpm workspaces)."""
    names = tuple(names)
    current = folder
    while True:
        if _existing(workspace, current, names):
            return current
        if current == Path(".") or current == current.parent:
            return None
        current = current.parent


def _topmost(manifest: Path, hits: Iterable[tuple[str, Path]], manager: str) -> Path:
    """Highest folder of this package manager that contains the manifest (workspace roots)."""
    folders = [other.parent for name, other in hits if name == manager]
    containing = [folder for folder in folders if manifest.parent.is_relative_to(folder)]
    return min(containing, key=lambda folder: len(folder.parts)) if containing else manifest.parent


def _nuget_projects(workspace: Path, target: Path) -> list[Path]:
    """packages.lock.json next to every project file of a build target (a solution covers its folder)."""
    root = workspace / target.parent
    projects = [target] if target.suffix == ".csproj" else sorted(path.relative_to(workspace)
                                                                  for path in root.rglob("*.csproj"))
    return [workspace / project.parent / "packages.lock.json" for project in projects]


def gradle_configurations(lockfile: Path) -> dict[str, tuple[str, ...]]:
    """"group:artifact:version" -> the configurations that contain it, from a gradle.lockfile."""
    entries: dict[str, tuple[str, ...]] = {}
    try:
        text = lockfile.read_text(encoding="utf-8")
    except OSError:
        return entries
    for line in text.splitlines():
        coordinates, separator, configurations = line.strip().partition("=")
        if separator and coordinates != "empty" and not coordinates.startswith("#"):
            entries[coordinates] = tuple(name for name in configurations.split(",") if name)
    return entries


def gradle_scope(configurations: Iterable[str]) -> str | None:
    """The most shipped scope of the configurations: runtime > compile > test > tooling.

    runtime/compile: *RuntimeClasspath/*CompileClasspath (also Android variants such as
    releaseRuntimeClasspath); test: every configuration with "test" in its name;
    tooling: the rest, e.g. annotationProcessor, plugin configurations such as
    Gretty's server runners, and the buildscript classpath.
    """
    def scope(name: str) -> str:
        lower = name.lower()
        if "test" in lower:
            return "test"
        if lower.endswith("runtimeclasspath"):
            return "runtime"
        if lower.endswith("compileclasspath"):
            return "compile"
        return "tooling"

    found = {scope(name) for name in configurations}
    return next((name for name in SCOPES if name in found), None)


def add_gradle_scopes(findings: list[DependencyFinding], workspace: Path) -> list[DependencyFinding]:
    """Tag the findings of Gradle lock files with their configurations and scope."""
    cache: dict[str, dict[str, tuple[str, ...]]] = {}
    tagged: list[DependencyFinding] = []
    for finding in findings:
        if finding.manifest and finding.manifest.endswith("gradle.lockfile"):
            if finding.manifest not in cache:
                cache[finding.manifest] = gradle_configurations(workspace / finding.manifest)
            configurations = cache[finding.manifest].get(f"{finding.package}:{finding.version}", ())
            finding = replace(finding, configurations=configurations, scope=gradle_scope(configurations))
        tagged.append(finding)
    return tagged

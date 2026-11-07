"""C and C++: clang-format (LLVM style with four changes), then clang-tidy.

clang-tidy needs the compile commands (include paths, macros, language
standard). In this order:
1. an existing compile_commands.json of the project,
2. one generated with CMake configure, after installing the dependencies with
   Conan or vcpkg (into the run folder, not the project),
3. none: default flags; the results are then marked as limited.
clang-tidy only reports (no --fix), see docs/linterConventions/README.md.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from scheduler.linting.files import source_files, write_file_list
from scheduler.linting.hiding import find_files, hidden_files
from scheduler.linting.model import Status, ToolOutcome, ToolRun
from scheduler.linting.reports import parse_clang_tidy
from scheduler.linting.session import LintContext, ToolSession, last_lines, run_tool, skipped

LANGUAGE = "C/C++"
IMAGE = "code-analysis/lint-cpp:latest"
SOURCE_SUFFIXES = (".c", ".cpp", ".cc", ".cxx", ".c++")
HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx", ".ipp", ".inl", ".tpp")
STYLE = "{BasedOnStyle: LLVM, ColumnLimit: 100, IndentWidth: 4, AccessModifierOffset: -4, SortIncludes: false}"
BASE_CHECKS = "-*,bugprone-*,cert-*,clang-analyzer-*,performance-*"
MODERN_CHECKS = f"{BASE_CHECKS},modernize-*"
PRE_CXX11 = frozenset({"c++98", "c++03", "gnu++98", "gnu++03"})
FALLBACK_FLAGS = {"c": ["-std=c11"], "cpp": ["-std=c++17"]}
DEPENDENCY_TIMEOUT_SECONDS = 90 * 60
TIDY_TIMEOUT_SECONDS = 90 * 60
VCPKG_TOOLCHAIN = "/opt/vcpkg/scripts/buildsystems/vcpkg.cmake"
XARGS_EXIT_COMMAND_FAILED = 123
_STD = re.compile(r"^(?:-std=|--std=|/std:)(?P<std>.+)$")

# Runs clang-tidy on each listed file in parallel, with fixed compiler flags.
FALLBACK_SCRIPT = """\
list="$1"; config="$2"; shift 2
xargs -0 -P "$(nproc)" -I{} clang-tidy --config="$config" {} -- "$@" < "$list"
"""


def run(ctx: LintContext) -> list[ToolRun]:
    files = source_files(ctx.project, SOURCE_SUFFIXES + HEADER_SUFFIXES)
    if not files:
        reason = "no tracked C/C++ files"
        return [skipped(LANGUAGE, "clang-format", reason), skipped(LANGUAGE, "clang-tidy", reason)]
    # clang-format honours .clang-format-ignore even with an inline style.
    ignore_files = find_files(ctx.mount_root, (".clang-format-ignore",))
    with hidden_files(ctx.mount_root, ignore_files, ctx.step_dir / "hidden" / "cpp", ctx.errors):
        formatted = run_tool(ctx, LANGUAGE, "clang-format", IMAGE, lambda session: _format(session, files))
    tidy = run_tool(ctx, LANGUAGE, "clang-tidy", IMAGE, lambda session: _tidy(session, files))
    return [formatted, tidy]


def _format(session: ToolSession, files: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    file_list = write_file_list((project / path for path in files), session.dir / "files.txt")
    result = session.run(
        ["sh", "-c", 'xargs -0 clang-format -i --style="$2" < "$1"', "sh", str(file_list), STYLE],
        label="clang-format",
    )
    if result.exit_code == 0:
        return ToolOutcome(Status.OK)
    if result.exit_code == XARGS_EXIT_COMMAND_FAILED:
        return ToolOutcome(Status.PARTIAL, notes=(f"clang-format reported errors: {last_lines(result.stderr)}",))
    return ToolOutcome(Status.FAILED, notes=(f"clang-format failed: {last_lines(result.stderr)}",))


# --- clang-tidy ------------------------------------------------------------------


def _tidy(session: ToolSession, files: list[Path]) -> ToolOutcome:
    project = session.ctx.project
    sources = [project / path for path in files if path.suffix.lower() in SOURCE_SUFFIXES]
    if not sources:
        return ToolOutcome(Status.SKIPPED, notes=("only header files, no translation units to analyse",))

    c_files = sum(1 for path in sources if path.suffix == ".c")
    database, notes = _compilation_database(session)
    notes.insert(0, f"source files: {c_files} C, {len(sources) - c_files} C++ (headers are checked through them)")
    entries = _project_entries(database, set(sources)) if database else []
    if database and not entries:
        notes.append("the compilation database lists none of the project's source files")

    if entries:
        outputs, exit_codes, groups = _tidy_with_database(session, entries)
        directories = {str(file): Path(entry["directory"]) for file, entry in entries}
    else:
        notes.append("results limited: clang-tidy ran without a compilation database (default flags)")
        outputs, exit_codes, groups = _tidy_without_database(session, sources, files)
        directories = {}

    report = parse_clang_tidy("\n".join(outputs), project=project, directories=directories)
    notes += [f"{count} file(s) checked with {checks}" for checks, count in groups.items() if count]
    if report.suppressed:
        notes.append(f"{report.suppressed} warnings suppressed by clang-tidy, {report.nolint} of them by NOLINT comments")
    if any(code is None for code in exit_codes):
        return ToolOutcome(Status.FAILED, tuple(report.findings), (*notes, "clang-tidy timed out"))
    if any(code != 0 for code in exit_codes):
        notes.append("clang-tidy reported compiler errors for some files (clang-diagnostic-error findings)")
        return ToolOutcome(Status.PARTIAL, tuple(report.findings), tuple(notes))
    return ToolOutcome(Status.OK, tuple(report.findings), tuple(notes))


def _tidy_with_database(session: ToolSession, entries: list[tuple[Path, dict]]):
    groups: dict[str, list[dict]] = {"base checks": [], "base checks + modernize-*": []}
    for file, entry in entries:
        key = "base checks + modernize-*" if _is_cxx11_or_newer(file, entry) else "base checks"
        groups[key].append(entry)

    outputs: list[str] = []
    exit_codes: list[int | None] = []
    for key, group in groups.items():
        if not group:
            continue
        checks = MODERN_CHECKS if "modernize" in key else BASE_CHECKS
        db_dir = session.dir / ("db-modern" if "modernize" in key else "db-base")
        db_dir.mkdir(parents=True, exist_ok=True)
        (db_dir / "compile_commands.json").write_text(json.dumps(group, indent=1), encoding="utf-8")
        result = session.run(
            ["run-clang-tidy", "-p", str(db_dir), f"-config={{Checks: '{checks}'}}"],
            timeout=TIDY_TIMEOUT_SECONDS,
            label=db_dir.name,
        )
        outputs += [result.stdout, result.stderr]
        exit_codes.append(result.exit_code)
    return outputs, exit_codes, {key: len(group) for key, group in groups.items()}


def _tidy_without_database(session: ToolSession, sources: list[Path], files: list[Path]):
    project = session.ctx.project
    script = session.dir / "tidy-without-database.sh"
    script.write_text(FALLBACK_SCRIPT, encoding="utf-8")
    include_dirs = sorted(
        {project} | {project / path.parent for path in files if path.suffix.lower() in HEADER_SUFFIXES}
    )
    includes = [f"-I{directory}" for directory in include_dirs]
    groups = {
        "c": [path for path in sources if path.suffix == ".c"],
        "cpp": [path for path in sources if path.suffix != ".c"],
    }
    outputs: list[str] = []
    exit_codes: list[int | None] = []
    for kind, group in groups.items():
        if not group:
            continue
        checks = BASE_CHECKS if kind == "c" else MODERN_CHECKS
        file_list = write_file_list(group, session.dir / f"files-{kind}.txt")
        result = session.run(
            ["sh", str(script), str(file_list), f"{{Checks: '{checks}'}}", *FALLBACK_FLAGS[kind], *includes],
            timeout=TIDY_TIMEOUT_SECONDS,
            label=f"clang-tidy-{kind}",
        )
        outputs += [result.stdout, result.stderr]
        exit_codes.append(result.exit_code)
    counts = {
        f"{'base checks' if kind == 'c' else 'base checks + modernize-*'} ({FALLBACK_FLAGS[kind][0]})": len(group)
        for kind, group in groups.items()
    }
    return outputs, exit_codes, counts


def _is_cxx11_or_newer(file: Path, entry: dict) -> bool:
    if file.suffix == ".c":
        return False
    arguments = _arguments(entry)
    if any(arguments[i] == "-x" and arguments[i + 1] == "c" for i in range(len(arguments) - 1)):
        return False
    std = None
    for argument in arguments:
        if match := _STD.match(argument):
            std = match.group("std").lower()
    if std is None:
        return True  # clang's default for C++ is gnu++17
    if "++" not in std:
        return False  # a C standard
    return std not in PRE_CXX11


def _arguments(entry: dict) -> list[str]:
    if isinstance(entry.get("arguments"), list):
        return [str(argument) for argument in entry["arguments"]]
    try:
        return shlex.split(str(entry.get("command", "")))
    except ValueError:
        return str(entry.get("command", "")).split()


def _entry_file(entry: dict) -> Path:
    file = Path(str(entry.get("file", "")))
    if not file.is_absolute():
        file = Path(str(entry.get("directory", ""))) / file
    return Path(_normpath(file))


def _normpath(path: Path) -> str:
    parts: list[str] = []
    for part in path.parts:
        if part == "..":
            if len(parts) > 1:
                parts.pop()
        elif part != ".":
            parts.append(part)
    return str(Path(*parts)) if parts else str(path)


def _project_entries(database: Path, sources: set[Path]) -> list[tuple[Path, dict]]:
    """Entries of the database that compile one of the project's tracked source files."""
    try:
        data = json.loads(database.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    seen: set[Path] = set()
    entries: list[tuple[Path, dict]] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict) or "directory" not in entry:
            continue
        file = _entry_file(entry)
        if file in sources and file not in seen:
            seen.add(file)
            entries.append((file, entry))
    return entries


# --- compilation database ----------------------------------------------------------


def _compilation_database(session: ToolSession) -> tuple[Path | None, list[str]]:
    project = session.ctx.project
    for candidate in _existing_databases(project):
        # Usable only if its paths exist here (not a database from another machine).
        if any(path.is_file() for path in _database_files(candidate)):
            return candidate, [f"compilation database: {candidate.relative_to(project).as_posix()} (from the project)"]

    if not (project / "CMakeLists.txt").is_file():
        return None, ["no compile_commands.json and no CMakeLists.txt in the project folder"]

    build = session.ctx.step_dir / "cpp-build"
    build.mkdir(parents=True, exist_ok=True)
    script, how = _generate_script(session)
    result = session.run(
        ["sh", "-c", script, "sh", str(project), str(build)],
        timeout=DEPENDENCY_TIMEOUT_SECONDS,
        label="compile-database",
    )
    database = build / "compile_commands.json"
    if result.exit_code == 0 and database.is_file():
        return database, [f"compilation database generated with {how} (configure only, no build)"]
    if result.exit_code is None:
        reason = "timed out"
    else:
        reason = f"exit code {result.exit_code}: {last_lines(result.stdout + result.stderr)}"
    return None, [f"could not generate a compilation database with {how} ({reason})"]


def _existing_databases(project: Path) -> list[Path]:
    candidates = [project / "compile_commands.json", project / "build" / "compile_commands.json"]
    candidates += sorted(project.glob("cmake-build-*/compile_commands.json"))
    return [path for path in candidates if path.is_file()]


def _database_files(database: Path) -> list[Path]:
    try:
        data = json.loads(database.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    return [_entry_file(entry) for entry in data if isinstance(entry, dict) and "directory" in entry]


def _generate_script(session: ToolSession) -> tuple[str, str]:
    """Shell script ($1 = project, $2 = build folder) that writes compile_commands.json."""
    manifests = sorted(session.ctx.manifests("C", "C++"), key=lambda path: (len(path.parts), path.as_posix()))
    conan = next((path for path in manifests if path.name in ("conanfile.txt", "conanfile.py")), None)
    vcpkg = next((path for path in manifests if path.name == "vcpkg.json"), None)
    cmake = 'cmake -S "$1" -B "$2" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_EXPORT_COMPILE_COMMANDS=ON'

    if conan is not None:
        text = (session.ctx.project / conan).read_text(encoding="utf-8", errors="replace")
        generators = " ".join(f"-g {name}" for name in ("CMakeDeps", "CMakeToolchain") if name not in text)
        # The packages must outlive this container: clang-tidy runs in a new
        # one and needs their headers at the paths in compile_commands.json.
        script = (
            "set -e\n"
            'export CONAN_HOME="$2/conan-home"\n'
            "conan profile detect --force\n"
            f'conan install "$1/{conan.parent.as_posix()}" --output-folder="$2" --build=missing {generators}\n'
            'toolchain=$(find "$2" -name conan_toolchain.cmake | head -n 1)\n'
            f'{cmake} -DCMAKE_TOOLCHAIN_FILE="$toolchain"\n'
        )
        return script, f"Conan ({conan.as_posix()}) and CMake"
    if vcpkg is not None:
        manifest_dir = f' -DVCPKG_MANIFEST_DIR="$1/{vcpkg.parent.as_posix()}"' if vcpkg.parent != Path(".") else ""
        script = f'set -e\n{cmake} -DCMAKE_TOOLCHAIN_FILE={VCPKG_TOOLCHAIN}{manifest_dir}\n'
        return script, f"vcpkg ({vcpkg.as_posix()}) and CMake"
    return f"set -e\n{cmake}\n", "CMake"

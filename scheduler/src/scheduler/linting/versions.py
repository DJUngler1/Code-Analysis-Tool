"""Language versions of a project, read from its build files.

Ignoring the project's configuration must not drop its language version:
PMD needs the Java version to parse old code and preview features, and Ruff
needs the Python version so that its fixes don't produce syntax that the
project's Python cannot run (docs/linterConventions/README.md).
"""

from __future__ import annotations

import re
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LanguageVersion:
    version: str | None  # e.g. "17", "1.8", "3.8"
    source: str | None = None  # file it was read from, relative to the project
    preview: bool = False  # Java: --enable-preview is set


NO_VERSION = LanguageVersion(None)

# --- Java -------------------------------------------------------------------

_GRADLE_FILES = ("build.gradle.kts", "build.gradle")
_GRADLE_PATTERNS = (
    re.compile(r"JavaLanguageVersion\s*\.\s*of\s*\(\s*(\d+)\s*\)"),
    re.compile(r"options\s*\.\s*release\s*(?:\.\s*set\s*\(|=)\s*(\d+)"),
    re.compile(r"sourceCompatibility\s*=\s*JavaVersion\s*\.\s*VERSION_(\d+(?:_\d+)?)"),
    re.compile(r"sourceCompatibility\s*=\s*['\"]?(\d+(?:\.\d+)?)['\"]?"),
)
_PROPERTY_REFERENCE = re.compile(r"^\$\{([^}]+)\}$")


def java_version(project: Path, build_files: Sequence[Path]) -> LanguageVersion:
    """Java version of the project's top-most Maven or Gradle build file."""
    candidates = sorted(
        {path.parent for path in build_files} | {Path(".")},
        key=lambda directory: (len(directory.parts), directory.as_posix()),
    )
    for directory in candidates:
        pom = project / directory / "pom.xml"
        if pom.is_file():
            found = _maven_version(pom)
            if found.version:
                return LanguageVersion(found.version, (directory / "pom.xml").as_posix(), found.preview)
        for name in _GRADLE_FILES:
            gradle = project / directory / name
            if gradle.is_file():
                found = _gradle_version(gradle.read_text(encoding="utf-8", errors="replace"))
                if found.version:
                    return LanguageVersion(found.version, (directory / name).as_posix(), found.preview)
    return NO_VERSION


def _maven_version(pom: Path) -> LanguageVersion:
    try:
        root = ET.parse(pom).getroot()
    except (ET.ParseError, OSError):
        return NO_VERSION
    _strip_namespaces(root)
    properties = {child.tag: (child.text or "").strip() for child in root.findall("properties/*")}
    plugin_config: dict[str, str] = {}
    for plugin in [*root.iter("plugin")]:
        if (plugin.findtext("artifactId") or "").strip() == "maven-compiler-plugin":
            for key in ("release", "source"):
                value = plugin.findtext(f"configuration/{key}")
                if value and value.strip():
                    plugin_config.setdefault(key, value.strip())

    preview = "--enable-preview" in pom.read_text(encoding="utf-8", errors="replace")
    for value in (
        plugin_config.get("release"),
        properties.get("maven.compiler.release"),
        plugin_config.get("source"),
        properties.get("maven.compiler.source"),
    ):
        resolved = _resolve(value, properties)
        if resolved:
            return LanguageVersion(resolved, preview=preview)
    return LanguageVersion(None, preview=preview)


def _resolve(value: str | None, properties: dict[str, str]) -> str | None:
    """Resolve one level of ${property} references; drop anything not a version."""
    if not value:
        return None
    if match := _PROPERTY_REFERENCE.match(value):
        value = properties.get(match.group(1), "")
    return value if re.fullmatch(r"\d+(?:\.\d+)?", value) else None


def _gradle_version(text: str) -> LanguageVersion:
    preview = "--enable-preview" in text
    for pattern in _GRADLE_PATTERNS:
        if match := pattern.search(text):
            return LanguageVersion(match.group(1).replace("_", "."), preview=preview)
    return LanguageVersion(None, preview=preview)


def _strip_namespaces(root: ET.Element) -> None:
    for element in root.iter():
        if isinstance(element.tag, str) and "}" in element.tag:
            element.tag = element.tag.split("}", 1)[1]


def pmd_language_version(version: LanguageVersion) -> str | None:
    """Value for PMD's --use-version, e.g. "java-17" or "java-27-preview"."""
    if not version.version:
        return None
    return f"java-{version.version}" + ("-preview" if version.preview else "")


# --- Python -----------------------------------------------------------------

_BOUND = re.compile(r"^\s*(>=|~=|==|===|\^|~|>)?\s*(\d+)(?:\.(\d+))?")


def python_version(project: Path, manifests: Sequence[Path]) -> LanguageVersion:
    """Lowest Python version the project supports, from its top-most pyproject.toml."""
    candidates = sorted(
        {path.parent for path in manifests if path.name == "pyproject.toml"} | {Path(".")},
        key=lambda directory: (len(directory.parts), directory.as_posix()),
    )
    for directory in candidates:
        pyproject = project / directory / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except tomllib.TOMLDecodeError:
            continue
        specifier = data.get("project", {}).get("requires-python") or (
            data.get("tool", {}).get("poetry", {}).get("dependencies", {}).get("python")
        )
        if isinstance(specifier, str) and (lowest := lowest_python(specifier)):
            return LanguageVersion(lowest, (directory / "pyproject.toml").as_posix())
    return NO_VERSION


def lowest_python(specifier: str) -> str | None:
    """Lower bound of a version specifier: ">=3.8,<4" -> "3.8", "^3.10" -> "3.10"."""
    bounds: list[tuple[int, int]] = []
    for clause in re.split(r",|\|\|", specifier):
        clause = clause.strip()
        if not clause or clause.startswith(("<", "!=")) or clause == "*":
            continue
        match = _BOUND.match(clause)
        if match and match.group(2) == "3":
            bounds.append((3, int(match.group(3) or 0)))
    if not bounds:
        return None
    major, minor = min(bounds)
    return f"{major}.{minor}"


def ruff_target_version(version: LanguageVersion) -> str | None:
    """Value for Ruff's --target-version, e.g. "py38". Ruff supports py37 onwards."""
    if not version.version:
        return None
    major, _, minor = version.version.partition(".")
    if major != "3" or not minor.isdigit():
        return None
    return f"py3{max(int(minor), 7)}"

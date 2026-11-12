"""Stage 2: the audit of each package manager, with a parser for its JSON output.

Built-in audits (npm, Yarn, pnpm, Bun, .NET) or the official tool of the
ecosystem (cargo-audit by RustSec, pip-audit by PyPA, conan audit with a token).
Maven, Gradle, vcpkg and Conda have no audit of their own.
"""

from __future__ import annotations

import json
import shlex
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from scheduler.dependencies.context import DependencyContext
from scheduler.dependencies.lockfiles import (
    CPP_IMAGE, DOTNET_IMAGE, NODE_IMAGE, PYTHON_IMAGE, RUST_IMAGE, Unit, units,
)
from scheduler.dependencies.model import (
    Coverage, DependencyFinding, DependencyRun, severity_from_cvss, severity_from_label,
)
from scheduler.linting.model import Status
from scheduler.linting.session import last_lines, slug

AUDITS_DIR = "audits"
Parser = Callable[[str, str], list[DependencyFinding]]  # (stdout, manifest) -> findings


@dataclass(frozen=True)
class AuditJob:
    unit: Unit
    tool: str
    image: str
    command: list[str]
    parse: Parser
    ok_exit_codes: tuple[int, ...]
    env: Mapping[str, str] | None = None


def plan_audits(ctx: DependencyContext, coverage: list[Coverage], conan_token: str | None) -> list[AuditJob]:
    states = {(entry.manager, entry.manifest): entry for entry in coverage}
    jobs: list[AuditJob] = []
    for unit in units(ctx):
        entry = states.get((unit.manager, unit.manifest.as_posix()))
        if entry is None or entry.audit is None:
            continue
        job = _job(unit, entry, conan_token)
        if job is not None:
            jobs.append(job)
    return jobs


def _job(unit: Unit, entry: Coverage, conan_token: str | None) -> AuditJob | None:
    manager = unit.manager
    if manager == "npm":
        return AuditJob(unit, "npm audit", NODE_IMAGE, ["npm", "audit", "--json"], parse_npm, (0, 1))
    if manager == "Yarn":
        script = ('case "$(yarn --version)" in 1.*) yarn audit --json ;; '
                  '*) yarn npm audit --all --recursive --json ;; esac')
        return AuditJob(unit, "yarn audit", NODE_IMAGE, ["sh", "-c", script], parse_yarn, tuple(range(32)))
    if manager == "pnpm":
        return AuditJob(unit, "pnpm audit", NODE_IMAGE, ["pnpm", "audit", "--json"], parse_pnpm, (0, 1))
    if manager == "Bun":
        return AuditJob(unit, "bun audit", NODE_IMAGE, ["bun", "audit", "--json"], parse_bun, (0, 1))
    if manager == "Cargo":
        return AuditJob(unit, "cargo audit", RUST_IMAGE, ["cargo", "audit", "--json"], parse_cargo, (0, 1))
    if manager == "NuGet":
        target = shlex.quote(unit.manifest.name)
        script = (f"dotnet restore {target} >&2 && "
                  f"dotnet package list --project {target} --vulnerable --include-transitive --format json")
        return AuditJob(unit, "dotnet package list --vulnerable", DOTNET_IMAGE, ["sh", "-c", script], parse_dotnet,
                        (0,))
    if manager in ("pip", "Poetry", "uv", "PDM", "Pipenv"):
        return AuditJob(unit, "pip-audit", PYTHON_IMAGE, ["sh", "-c", _pip_audit_script(unit)], parse_pip_audit,
                        (0, 1))
    if manager == "Conan" and conan_token:
        script = ('conan audit provider auth conancenter --token="$CONAN_AUDIT_TOKEN" >&2 && '
                  "conan audit scan . --format=json")
        return AuditJob(unit, "conan audit", CPP_IMAGE, ["sh", "-c", script], parse_conan, (0, 1),
                        env={"CONAN_AUDIT_TOKEN": conan_token})
    return None


def _pip_audit_script(unit: Unit) -> str:
    """pip-audit reads requirements files and pyproject.toml; lock files are exported first."""
    audit = "pip-audit -f json --progress-spinner off"
    pinned = f"{audit} -r /tmp/requirements.txt --no-deps --disable-pip"
    exports = {
        "Poetry": "poetry export -f requirements.txt --without-hashes -o /tmp/requirements.txt",
        "uv": "uv export --frozen --format requirements-txt --no-hashes --no-emit-project -o /tmp/requirements.txt",
        "PDM": "pdm export -f requirements --without-hashes -o /tmp/requirements.txt",
        "Pipenv": "pipenv requirements > /tmp/requirements.txt",
    }
    if unit.manager in exports:
        # Subshell: the export's own messages go to stderr, its file stays intact.
        return f"( {exports[unit.manager]} ) >&2 && {pinned}"
    if unit.manifest.name.endswith(".txt"):
        return f"{audit} -r {shlex.quote(unit.manifest.name)}"
    return f"{audit} ."  # pyproject.toml / setup.py: pip-audit resolves the project


def run_audit(ctx: DependencyContext, job: AuditJob, index: int) -> DependencyRun:
    folder = f"{AUDITS_DIR}/{index:02d}-{slug(job.unit.manager)}"
    out_dir = ctx.step_dir / folder
    manifest = job.unit.manifest.as_posix()
    run = DependencyRun(tool=job.tool, status=Status.FAILED, output_dir=folder, manifest=manifest,
                        manager=job.unit.manager)
    versions = ctx.runner.image_versions(job.image)
    run.version = versions.get(job.tool.split()[0]) or versions.get(job.unit.manager.lower())
    started = time.monotonic()
    result = ctx.run(job.image, job.command, folder=job.unit.folder, log_dir=out_dir, label="audit", env=job.env)
    run.commands.append(result)
    run.duration_seconds = round(time.monotonic() - started, 1)
    if result.exit_code not in job.ok_exit_codes:
        run.notes.append(f"{job.tool} failed (exit code {result.exit_code}): "
                         f"{last_lines(result.stderr or result.stdout)}")
        return run
    try:
        findings = job.parse(result.stdout, manifest)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        run.notes.append(f"{job.tool}: unexpected output ({exc}): {last_lines(result.stderr or result.stdout)}")
        return run
    run.findings = [_with_manager(finding, job.unit.manager) for finding in findings]
    run.status = Status.OK
    return run


def _with_manager(finding: DependencyFinding, manager: str) -> DependencyFinding:
    return replace(finding, manager=manager)


# --- parsers ------------------------------------------------------------------------


def _ghsa(url: object) -> str | None:
    text = str(url or "")
    return text.rsplit("/", 1)[-1] if "/advisories/GHSA-" in text else None


def _advisory_id(url: object, fallback: object) -> str:
    return _ghsa(url) or str(fallback)


def _cvss(data: object) -> tuple[float | None, str | None]:
    if not isinstance(data, Mapping):
        return None, None
    score = data.get("score")
    vector = data.get("vectorString") or data.get("vector")
    return (float(score) if isinstance(score, (int, float)) and score > 0 else None), (str(vector) if vector else None)


def _node_finding(tool: str, manifest: str, *, package: str, version: str | None, advisory: Mapping,
                  severity: object, url: object, cves: Iterable[str] = (),
                  fixed: Iterable[str] = ()) -> DependencyFinding:
    score, vector = _cvss(advisory.get("cvss"))
    ghsa = _ghsa(url)
    aliases = {str(cve) for cve in cves} | ({str(advisory.get("github_advisory_id"))}
                                           if advisory.get("github_advisory_id") else set())
    finding_id = ghsa or str(advisory.get("id") or advisory.get("source") or "unknown")
    return DependencyFinding(
        tool=tool, ecosystem="npm", package=package, version=version, id=finding_id,
        aliases=tuple(sorted(aliases - {finding_id})),
        severity=severity_from_label(severity) if severity else severity_from_cvss(score),
        cvss_score=score, cvss_vector=vector, summary=str(advisory.get("title") or "").strip(),
        fixed_versions=tuple(fixed), url=str(url) if url else None, manifest=manifest,
    )


def parse_npm(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`npm audit --json` (report version 2): advisories are the objects in `via`."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for name, entry in data.get("vulnerabilities", {}).items():
        for via in entry.get("via", []):
            if isinstance(via, Mapping):  # strings are the dependencies the advisory comes through
                findings.append(_node_finding("npm audit", manifest, package=str(via.get("name") or name),
                                              version=None, advisory=via, severity=via.get("severity"),
                                              url=via.get("url")))
    return findings


def parse_yarn(stdout: str, manifest: str) -> list[DependencyFinding]:
    """Yarn 1 (`auditAdvisory` lines) or Yarn 2+ (`yarn npm audit`, one object per advisory)."""
    findings: list[DependencyFinding] = []
    seen: set[tuple] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        entry = json.loads(line)
        if entry.get("type") == "auditAdvisory":
            advisory = entry["data"]["advisory"]
            for version in sorted({str(found.get("version")) for found in advisory.get("findings", [])}):
                key = (advisory.get("id"), version)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(_node_finding("yarn audit", manifest, package=str(advisory.get("module_name")),
                                              version=version, advisory=advisory, severity=advisory.get("severity"),
                                              url=advisory.get("url"), cves=advisory.get("cves", [])))
        elif "children" in entry:
            child = entry["children"]
            advisory = {"id": child.get("ID"), "title": child.get("Issue")}
            for version in child.get("Tree Versions", []) or [None]:
                key = (child.get("ID"), version)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(_node_finding("yarn audit", manifest, package=str(entry.get("value")),
                                              version=version, advisory=advisory, severity=child.get("Severity"),
                                              url=child.get("URL")))
    return findings


def parse_pnpm(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`pnpm audit --json`: `advisories` by id, each with the installed versions in `findings`."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for advisory in data.get("advisories", {}).values():
        versions = sorted({str(found.get("version")) for found in advisory.get("findings", [])}) or [None]
        for version in versions:
            findings.append(_node_finding("pnpm audit", manifest, package=str(advisory.get("module_name")),
                                          version=version, advisory=advisory, severity=advisory.get("severity"),
                                          url=advisory.get("url"), cves=advisory.get("cves", [])))
    return findings


def parse_bun(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`bun audit --json`: advisories by package name."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for package, advisories in data.items():
        for advisory in advisories if isinstance(advisories, list) else []:
            findings.append(_node_finding("bun audit", manifest, package=str(package), version=None,
                                          advisory=advisory, severity=advisory.get("severity"),
                                          url=advisory.get("url")))
    return findings


def parse_cargo(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`cargo audit --json`: `vulnerabilities.list`; `warnings` (unmaintained, yanked) are no findings."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for entry in data.get("vulnerabilities", {}).get("list", []):
        advisory, package = entry.get("advisory", {}), entry.get("package", {})
        vector = advisory.get("cvss")
        findings.append(DependencyFinding(
            tool="cargo audit", ecosystem="crates.io", package=str(package.get("name")),
            version=package.get("version"), id=str(advisory.get("id")),
            aliases=tuple(sorted(str(alias) for alias in advisory.get("aliases", []))),
            severity="unknown", cvss_vector=str(vector) if vector else None,
            summary=str(advisory.get("title") or "").strip(),
            fixed_versions=tuple(entry.get("versions", {}).get("patched", [])),
            url=advisory.get("url") or f"https://rustsec.org/advisories/{advisory.get('id')}",
            manifest=manifest,
        ))
    return findings


def parse_dotnet(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`dotnet package list --vulnerable --include-transitive --format json`."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    seen: set[tuple] = set()
    for project in data.get("projects", []):
        for framework in project.get("frameworks", []):
            for package in [*framework.get("topLevelPackages", []), *framework.get("transitivePackages", [])]:
                for vulnerability in package.get("vulnerabilities", []):
                    url = vulnerability.get("advisoryurl")
                    key = (package.get("id"), package.get("resolvedVersion"), url)
                    if key in seen:
                        continue  # the same package in several projects or frameworks
                    seen.add(key)
                    findings.append(DependencyFinding(
                        tool="dotnet package list --vulnerable", ecosystem="NuGet", package=str(package.get("id")),
                        version=package.get("resolvedVersion"), id=_advisory_id(url, url),
                        severity=severity_from_label(vulnerability.get("severity")), url=url, manifest=manifest,
                    ))
    return findings


def parse_pip_audit(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`pip-audit -f json`: `dependencies[].vulns`. pip-audit reports no severity."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for dependency in data.get("dependencies", []):
        for vuln in dependency.get("vulns", []):
            findings.append(DependencyFinding(
                tool="pip-audit", ecosystem="PyPI", package=str(dependency.get("name")),
                version=dependency.get("version"), id=str(vuln.get("id")),
                aliases=tuple(sorted(str(alias) for alias in vuln.get("aliases", []))),
                summary=str(vuln.get("description") or "").strip().split("\n")[0][:300],
                fixed_versions=tuple(vuln.get("fix_versions", [])),
                url=f"https://osv.dev/vulnerability/{vuln.get('id')}", manifest=manifest,
            ))
    return findings


def parse_conan(stdout: str, manifest: str) -> list[DependencyFinding]:
    """`conan audit scan --format=json`, structure taken from Conan's formatter (untested: needs a token)."""
    data = json.loads(stdout)
    findings: list[DependencyFinding] = []
    for reference, info in data.get("data", {}).items():
        package, _, version = str(reference).partition("/")
        for edge in info.get("vulnerabilities", {}).get("edges", []):
            node = edge.get("node", {})
            fixed = [fix.get("version") for fix_edge in node.get("vulnerablePackages", {}).get("edges", [])
                     for fix in fix_edge.get("node", {}).get("fixVersions", [])]
            score = node.get("cvss", {}).get("preferredBaseScore") if isinstance(node.get("cvss"), Mapping) else None
            findings.append(DependencyFinding(
                tool="conan audit", ecosystem="ConanCenter", package=package, version=version.split("#")[0] or None,
                id=str(node.get("name")), severity=severity_from_label(node.get("severity")),
                cvss_score=float(score) if isinstance(score, (int, float)) else None,
                summary=str(node.get("description") or "").strip().split("\n")[0][:300],
                fixed_versions=tuple(str(version) for version in fixed if version),
                url=(node.get("references") or [None])[0], manifest=manifest,
            ))
    return findings

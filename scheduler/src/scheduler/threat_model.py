"""The threat model of the analysed project: asked once, drafted by the reasoning model, confirmed by the user.

Whether a security finding matters depends on facts the finding does not show: which data an attacker
controls, and what is shipped. Step 6 therefore gets a threat model of the project. The tool looks for it
before step 2 (and before step 6 with --evaluate), in this order:

1. <project>/.code-analysis/threat-model.md, committed to the project. It counts as confirmed (step 1 only
   accepts a clean working tree). Meant for CI, where nobody can answer questions.
2. <OUTPUT_DIR>/<project>/threat-model.md, written by the tool, with threat-model.json: status (draft or
   confirmed), time and SHA-256 of the confirmed text, the user's answers, the documentation read and the
   model. A text changed after its confirmation must be confirmed again.

If there is none, [llm.threat_model] create decides how it is made:
- "generate": the user answers three questions. The reasoning model of [llm.report] writes a draft from the
  answers, the facts of step 1 and the project's README, SECURITY.md and docs/ (prompt threat_model.md,
  request and answer in threat-model-request.json). The user reads the draft, corrects it in an editor and
  confirms it in the terminal.
- "manual": the tool writes a template with the three questions as hints; the user writes the threat model
  and confirms it. No model is involved.
Users can also write the file themselves at any time: committed to the project (used as it is), or in the
results folder (confirmed once in the terminal). Without a terminal (CI, piped input) and without a confirmed
threat model the tool stops with a hint.

Each run keeps the threat model it used (<run folder>/threat-model.md and .json); step 6 reads only that
copy (evaluation/threat.py).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from scheduler.config import Settings
from scheduler.evaluation.config import ConfigError, LlmConfig, ReportConfig, load_config
from scheduler.evaluation.items import path_scope
from scheduler.evaluation.ollama import Exchange, OllamaClient, OllamaError
from scheduler.evaluation.prompts import read_prompt, render, sections
from scheduler.evaluation.report import parse_json
from scheduler.evaluation.threat import FILE, INFO, clean

PROJECT_FILE = Path(".code-analysis") / FILE
REQUEST_LOG = "threat-model-request.json"
CONFIRMED, DRAFT = "confirmed", "draft"
QUESTIONS = (
    ("software", "1. What kind of software is this, and how is it used? (library, web server, app, CLI tool, ...)"),
    ("untrusted_inputs", "2. Where does untrusted data come in? (files, network, user input, ...: the trust "
                         "boundaries)"),
    ("shipping", "3. What is shipped, and what is not? (e.g. tests, build tools and examples are not shipped)"),
)
# Fields of the model's answer: key, heading in threat-model.md, list or text.
SECTIONS = (
    ("software", "Software and use", False),
    ("untrusted_inputs", "Untrusted inputs (trust boundaries)", True),
    ("trusted_inputs", "Not attacker-controlled", True),
    ("shipped", "Shipped", True),
    ("not_shipped", "Not shipped", True),
    ("assumptions", "Assumptions to check", True),
)
# Hints of the template ([llm.threat_model] create = "manual"), as HTML comments the models do not see.
TEMPLATE_HINTS = {
    "software": QUESTIONS[0][1],
    "untrusted_inputs": QUESTIONS[1][1] + " Name the kind of data, where it comes from and which part of the "
                                          "project reads it.",
    "trusted_inputs": "Inputs that are not attacker-controlled, e.g. arguments a developer passes through the API "
                      "of a library.",
    "shipped": QUESTIONS[2][1] + " Folders or modules that are shipped.",
    "not_shipped": "Folders or modules that are not shipped, e.g. tests, examples, build tools, CI.",
    "assumptions": "Anything you are not sure about (optional).",
}
SCHEMA = {
    "type": "object",
    "properties": {key: {"type": "array", "items": {"type": "string"}} if is_list else {"type": "string"}
                   for key, _, is_list in SECTIONS},
    "required": [key for key, _, _ in SECTIONS],
}
ROOT_DOCS = re.compile(r"^(readme|security)(\.[a-z]+)?$", re.IGNORECASE)
DOC_FOLDERS = ("docs", "doc")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
NO_ANSWER = "(no answer)"
PROMPT_RESERVE = 1000  # characters kept free in the context window besides the measured prompt


class ThreatModelError(Exception):
    """No confirmed threat model, and none could be made (no terminal, aborted, no answer from the model)."""


@dataclass
class ThreatModel:
    text: str
    source: str  # "project": committed to the project; "results": confirmed in the tool
    path: Path
    confirmed_at: str | None = None
    sha256: str = ""

    def info(self) -> dict:
        return {"source": self.source, "path": str(self.path), "confirmed_at": self.confirmed_at,
                "sha256": self.sha256}


@dataclass
class Lookup:
    state: str  # confirmed, changed (after its confirmation), draft (not confirmed yet), missing
    model: ThreatModel | None = None
    info: dict = field(default_factory=dict)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def find(project: Path | None, folder: Path) -> Lookup:
    """The project's threat model: committed to the project, else in the results folder."""
    if project is not None and (project / PROJECT_FILE).is_file():
        text = (project / PROJECT_FILE).read_text(encoding="utf-8", errors="replace")
        if has_content(text):
            return Lookup(CONFIRMED, ThreatModel(text, "project", project / PROJECT_FILE, None, digest(text)))
    path = folder / FILE
    if not path.is_file():
        return Lookup("missing")
    text = path.read_text(encoding="utf-8", errors="replace")
    info = _read_json(folder / INFO)
    if info.get("status") != CONFIRMED:
        return Lookup(DRAFT, info=info)
    if info.get("sha256") != digest(text):
        return Lookup("changed", info=info)
    return Lookup(CONFIRMED, ThreatModel(text, "results", path, info.get("confirmed_at"), digest(text)), info)


def ensure_threat_model(project: Path | None, settings: Settings, *, folder: Path | None, facts: dict,
                        interactive: bool, client: OllamaClient | None = None,
                        ask: Callable[[str], str] = input, out: Callable[[str], None] = print,
                        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> ThreatModel:
    """The confirmed threat model of the project; asks for one if there is none. Raises ThreatModelError."""
    if folder is None:
        raise ThreatModelError("OUTPUT_DIR is not set (docker/.env): the threat model is kept in the results "
                               "folder of the project.")
    try:
        config = load_config(settings.config_file)
    except ConfigError as exc:
        raise ThreatModelError(str(exc)) from exc
    lookup = find(project, folder)
    if lookup.state == CONFIRMED:
        assert lookup.model is not None
        model = lookup.model
        how = f"confirmed {model.confirmed_at}" if model.confirmed_at else "committed to the project"
        out(f"Threat model: {model.path} ({how}).")
        _warn_length(model.text, config, out)
        return model
    if not interactive:
        raise ThreatModelError(_missing_hint(lookup.state, folder))
    session = _Session(project, folder, facts, config, client or OllamaClient(settings.ollama_base_url), ask, out,
                       now)
    if lookup.state == "changed":
        out(f"The threat model {folder / FILE} was changed after it was confirmed. Read it and confirm it again.")
        info = lookup.info
    elif lookup.state == DRAFT:
        out(f"There is a threat model that was not confirmed yet: {folder / FILE}")
        info = lookup.info
    elif config.threat_model.create == "manual":
        info = session.template()
    else:
        info = session.draft()
    return session.review(info)


def copy_to_run(run_dir: Path, model: ThreatModel) -> None:
    """Keep the threat model a run used in its folder; step 6 reads only this copy."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / FILE).write_text(model.text, encoding="utf-8")
    _write_json(run_dir / INFO, {"status": CONFIRMED, **model.info()})


def project_facts(facts: dict, project: Path | None) -> dict:
    """Facts of step 1 (languages, package managers) and the top-level folders with their scope."""
    result = {key: facts[key] for key in ("languages", "package_managers") if facts.get(key)}
    if project is not None and project.is_dir():
        result["top_level_folders"] = {path.name: path_scope(f"{path.name}/file", set())
                                       for path in sorted(project.iterdir())
                                       if path.is_dir() and not path.name.startswith(".")}
    return result


def collect_docs(project: Path, budget: int) -> tuple[str, list[dict], int]:
    """README, SECURITY.md and the files of docs/ (in this order) up to `budget` characters.

    Returns the text, the files read with their size and the part used, and the number of files left out."""
    files = sorted(path for path in project.iterdir() if path.is_file() and ROOT_DOCS.match(path.name))
    for name in DOC_FOLDERS:
        if (project / name).is_dir():
            files += sorted(path for path in (project / name).rglob("*")
                            if path.is_file() and path.suffix.lower() in DOC_SUFFIXES
                            and not any(part.startswith(".") for part in path.relative_to(project).parts))
    parts: list[str] = []
    read: list[dict] = []
    left = budget
    for path in files:
        if left <= 0:
            break
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        used = text[:left]
        relative = path.relative_to(project).as_posix()
        parts.append(f"===== {relative} =====\n{used}" + ("\n[... shortened]" if len(used) < len(text) else ""))
        read.append({"file": relative, "chars": len(text), "used": len(used)})
        left -= len(used)
    return "\n\n".join(parts), read, len(files) - len(read)


def render_template(name: str, limit: int) -> str:
    """The empty threat model to write by hand ([llm.threat_model] create = "manual")."""
    lines = [f"<!-- Threat model of {name}, written by you. Answer below each heading, concretely (modules, folders, "
             "file formats, protocols), one line per list item. The comments are not passed to the models; the "
             f"security decisions of step 6 see the rest, at most {limit} characters. -->", "",
             f"# Threat model: {name}"]
    for key, heading, is_list in SECTIONS:
        lines += ["", f"## {heading}", "", f"<!-- {TEMPLATE_HINTS[key]} -->"] + (["- "] if is_list else [])
    return "\n".join(lines) + "\n"


def has_content(text: str) -> bool:
    """More than headings and empty list items (an unchanged template has none)."""
    return any(line.strip() not in ("", "-") and not line.lstrip().startswith("#")
               for line in clean(text).splitlines())


def render_markdown(name: str, answer: dict, model: str, documentation: list[dict]) -> str:
    lines = [f"<!-- Draft by {model} from your answers and {len(documentation)} files of documentation. Check every "
             "line and correct it, then confirm it in the terminal. This comment is not passed to the models. -->",
             "", f"# Threat model: {name}"]
    for key, heading, is_list in SECTIONS:
        lines += ["", f"## {heading}", ""]
        if is_list:
            items = [item.strip() for item in answer.get(key) or [] if str(item).strip()]
            lines += [f"- {item}" for item in items] or ["- none named"]
        else:
            lines.append(str(answer.get(key) or "").strip())
    return "\n".join(lines) + "\n"


class _Session:
    """One interactive round: answers, draft, review."""

    def __init__(self, project: Path | None, folder: Path, facts: dict, config: LlmConfig, client: OllamaClient,
                 ask: Callable[[str], str], out: Callable[[str], None], now: Callable[[], datetime]):
        self.project, self.folder, self.facts, self.config = project, folder, facts, config
        self.client, self.ask, self.out, self.now = client, ask, out, now

    def draft(self) -> dict:
        self.out("No confirmed threat model for this project yet. Please answer three questions. End an answer "
                 "with an empty line; an empty answer leaves the question to the documentation.")
        answers = {key: self._answer(question) for key, question in QUESTIONS}
        report = self.config.report
        prompt = _prompt()
        name = self.project.name if self.project else self.folder.name
        facts = json.dumps(self.facts, ensure_ascii=False, indent=1)
        answers_text = "\n".join(f"{question}\n{answers[key]}" for key, question in QUESTIONS)
        without_docs = render(prompt["Request"], ANSWERS=answers_text, PROJECT_FACTS=facts, PROJECT_DOCS="")
        budget = min(self.config.threat_model.docs_kib * 1024,
                     int((report.num_ctx - report.num_predict) * report.chars_per_token)
                     - len(prompt["System"]) - len(without_docs) - PROMPT_RESERVE)
        docs, documentation, left_out = collect_docs(self.project, max(0, budget)) if self.project else ("", [], 0)
        self.out(f"\nWriting a draft with {report.model} from your answers and {len(documentation)} files of "
                 f"documentation{f' ({left_out} more left out: too long)' if left_out else ''} ...")
        user = render(prompt["Request"], ANSWERS=answers_text, PROJECT_FACTS=facts,
                      PROJECT_DOCS=docs or "(no documentation found)")
        answer, exchange = self._ask_model(report, prompt["System"], user)
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / FILE
        if path.is_file():  # a regenerated draft replaces the file; the previous one is kept once
            shutil.copyfile(path, path.with_name(FILE + ".previous"))
        path.write_text(render_markdown(name, answer, report.model, documentation), encoding="utf-8")
        _write_json(self.folder / REQUEST_LOG, {"endpoint": exchange.endpoint, "attempts": exchange.attempts,
                                                "duration_seconds": exchange.duration_seconds,
                                                "request": exchange.request, "response": exchange.response})
        info = {"status": DRAFT, "created": self.now().isoformat(timespec="seconds"), "answers": answers,
                "documentation": documentation, "documentation_left_out": left_out,
                "model": {"name": report.model, "think": report.think, "options": report.options()}}
        _write_json(self.folder / INFO, info)
        return info

    def template(self) -> dict:
        """create = "manual": an empty threat model with the questions as hints, for the user to write."""
        path = self.folder / FILE
        self.folder.mkdir(parents=True, exist_ok=True)
        name = self.project.name if self.project else self.folder.name
        path.write_text(render_template(name, self.config.threat_model.context_chars), encoding="utf-8")
        self.out(f"No confirmed threat model for this project yet. A template is ready for you to fill in: {path}\n"
                 "(It is written by hand because [llm.threat_model] create = \"manual\" in code-analysis.toml.)")
        info = {"status": DRAFT, "created": self.now().isoformat(timespec="seconds"), "written_by": "user"}
        _write_json(self.folder / INFO, info)
        return info

    def review(self, info: dict) -> ThreatModel:
        path = self.folder / FILE
        generate = self.config.threat_model.create == "generate"
        options = "[c]onfirm, [r]egenerate from new answers, [a]bort: " if generate else "[c]onfirm, [a]bort: "
        while True:
            self.out(f"\nRead the threat model and correct it in an editor: {path}\n"
                     "The security decisions of step 6 rely on it. Save the file, then come back here.")
            choice = self._input(options).strip().lower()
            if choice in ("c", "confirm"):
                text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
                if not has_content(text):
                    self.out("The threat model has no content yet (only headings). Write it into the file"
                             + (", or regenerate it." if generate else "."))
                    continue
                _warn_length(text, self.config, self.out)
                info = info | {"status": CONFIRMED, "confirmed_at": self.now().isoformat(timespec="seconds"),
                               "sha256": digest(text)}
                _write_json(self.folder / INFO, info)
                self.out("Threat model confirmed.")
                return ThreatModel(text, "results", path, info["confirmed_at"], info["sha256"])
            if generate and choice in ("r", "regenerate"):
                info = self.draft()
            elif choice in ("a", "abort"):
                raise ThreatModelError(f"aborted. The threat model was not confirmed; it stays in {path}.")
            else:
                self.out("Please answer c, r or a." if generate else "Please answer c or a.")

    def _answer(self, question: str) -> str:
        self.out(f"\n{question}")
        lines: list[str] = []
        while True:
            line = self._input("> ")
            if not line.strip():
                return "\n".join(lines) or NO_ANSWER
            lines.append(line.rstrip())

    def _input(self, prompt: str) -> str:
        try:
            return self.ask(prompt)
        except EOFError as exc:
            raise ThreatModelError("no more input: the threat model was not confirmed.") from exc

    def _ask_model(self, report: ReportConfig, system: str, user: str) -> tuple[dict, Exchange]:
        """The draft as JSON; an answer that is not valid is asked again with another seed, `retries` times."""
        error = "no valid answer"
        try:
            for attempt in range(report.retries + 1):
                exchange = self.client.chat(report.model, system, user, SCHEMA,
                                            options=report.options() | {"seed": report.seed + attempt},
                                            think=report.think, retries=report.retries,
                                            timeout=report.timeout_seconds, keep_alive=report.keep_alive)
                if exchange.response is None:
                    error = exchange.error or "no answer"
                    break  # the request itself failed after its repetitions
                answer = parse_json(str((exchange.response.get("message") or {}).get("content") or ""))
                if _valid(answer):
                    assert answer is not None
                    return answer, exchange
        except OllamaError as exc:
            error = str(exc)
        finally:  # the tools of steps 2 to 5 need the memory
            try:
                self.client.unload(report.model)
            except OllamaError:
                pass
        raise ThreatModelError(f"the reasoning model {report.model} wrote no valid draft ({error}). Check that "
                               "Ollama runs and the model supports structured output (GGUF, not MLX).")


def _valid(answer: dict | None) -> bool:
    if not isinstance(answer, dict) or not str(answer.get("software") or "").strip():
        return False
    return all(isinstance(answer.get(key, []), list) for key, _, is_list in SECTIONS if is_list)


def _prompt() -> dict[str, str]:
    parts = sections(read_prompt("threat_model"))
    missing = [title for title in ("System", "Request") if title not in parts]
    if missing:
        raise ThreatModelError(f"prompt threat_model.md has no section {', '.join(missing)}")
    return parts


def _warn_length(text: str, config: LlmConfig, out: Callable[[str], None]) -> None:
    limit = config.threat_model.context_chars
    if len(clean(text)) > limit:
        out(f"Note: the threat model has {len(clean(text))} characters; the security decisions see only the first "
            f"{limit} ([llm.threat_model] context_chars). Shorten it, or raise the limit.")


def _missing_hint(state: str, folder: Path) -> str:
    reason = {"changed": "was changed after it was confirmed", DRAFT: "is a draft that was not confirmed"}.get(
        state, "does not exist")
    return (f"no confirmed threat model: {folder / FILE} {reason}, and without a terminal nobody can answer the "
            "questions. Run the tool once in a terminal (bin/code-analysis) to create and confirm it, or write it "
            f"yourself and commit it to the project as {PROJECT_FILE}.")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

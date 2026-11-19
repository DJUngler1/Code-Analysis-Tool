"""[llm], [llm.input], [llm.decision], [llm.report] and [llm.report.<area>] of code-analysis.toml.

Every key has a default, so a missing key or section is fine. Unknown keys are
errors: a typo would otherwise be ignored silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from pathlib import Path

from scheduler.tool_config import ConfigFileError, flag, number, read_config, section, text, whole_number

AREAS = ("security", "code_quality", "test_quality")
MAX_QUESTIONS = 64  # limit of Ollama's decision endpoint
MAX_REQUEST_KIB = 64  # local limit of the decision endpoint (text and questions)
# Keys of [llm] itself; base_url is read by scheduler.config (OLLAMA_BASE_URL overrides it).
LLM_KEYS = ("base_url", "report_language", "areas")
MOVED_TO_MODELS = ("timeout_seconds", "retries", "keep_alive")
THREAT_MODEL_CREATE = ("generate", "manual")  # [llm.threat_model] create
# Severities of the findings (normalized by steps 2 to 4), for security_single_severity.
SEVERITIES = ("critical", "high", "error", "medium", "warning", "low", "note", "info")


class ConfigError(Exception):
    """[llm] in code-analysis.toml is invalid."""


@dataclass(frozen=True)
class InputConfig:
    """What the tool prepares for the models and the reports (step 6, part 1)."""

    message_chars: int = 300  # message of a finding, summary of an advisory
    code_chars: int = 400  # code excerpt around a finding
    code_context_lines: int = 2  # lines before and after the finding in the excerpt
    # Comments left out of the code the models see (// and /* */, or # for Python; line numbers stay). For
    # benchmarks whose comments tell which code is safe; off for real projects, where comments are context.
    strip_comments: bool = False
    group_samples: int = 5  # sample locations per group of findings (code quality, security)
    group_samples_with_code: int = 3  # of these, the first ones with a code excerpt
    group_messages: int = 3  # different messages per group
    # Security report: REAL findings of this severity or higher get an entry of their own ("high": high, error,
    # critical); the other findings are grouped by rule (or package) and label.
    security_single_severity: str = "high"
    failed_tests_per_group: int = 5  # test names per group of failed tests
    failure_message_chars: int = 600  # failure message of a group of failed tests
    declaration_chars: int = 300  # declarations (classes, functions) of a weakly tested file
    weak_line_rate: float = 0.5  # files with a lower line coverage are weakly tested
    diff_example_lines: int = 10  # lines of the formatter example in the report
    patch_sample_kib: int = 32  # part of changes.patch the reasoning model sees for the kinds of change


@dataclass(frozen=True)
class DecisionConfig:
    model: str = "pplx-decider-flash:latest"
    timeout_seconds: int = 300
    retries: int = 2
    keep_alive: str = "10m"
    max_questions: int = 8
    max_input_kib: int = 20
    review_confidence: float = 0.6


@dataclass(frozen=True)
class ReportConfig:
    model: str = "qwen3.8:latest"
    timeout_seconds: int = 900
    retries: int = 2
    keep_alive: str = "10m"
    think: bool | str = True  # or a level the model supports, e.g. "low", "medium", "high"
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int | None = None  # None (key not set): not sent, Ollama's or the model's default applies
    min_p: float | None = None  # None: not sent, the model's default applies
    seed: int = 42
    num_ctx: int = 16384
    num_predict: int = 6144
    entries_per_request: int = 10
    chars_per_token: float = 2.5

    def options(self) -> dict:
        """Ollama's generation options for /api/chat."""
        options = {"temperature": self.temperature, "top_p": self.top_p, "seed": self.seed,
                   "num_ctx": self.num_ctx, "num_predict": self.num_predict}
        for name in ("top_k", "min_p"):  # only sent when set
            if getattr(self, name) is not None:
                options[name] = getattr(self, name)
        return options


@dataclass(frozen=True)
class ThreatModelConfig:
    """The project's threat model (scheduler.threat_model), if the project has no confirmed one yet."""

    # "generate": three questions, the reasoning model of [llm.report] drafts it, the user corrects and confirms it;
    # "manual": the tool writes a template, the user writes the threat model and confirms it (no model).
    create: str = "generate"
    docs_kib: int = 24  # README, SECURITY.md and docs/ the reasoning model reads for the draft
    context_chars: int = 2500  # characters of the confirmed threat model the security decisions and reports see


@dataclass(frozen=True)
class LlmConfig:
    report_language: str = "English"
    areas: tuple[str, ...] = AREAS
    input: InputConfig = field(default_factory=InputConfig)
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    report: ReportConfig = field(default_factory=ReportConfig)
    # [llm.report.<area>]: the keys an area sets differently from [llm.report].
    report_overrides: dict[str, dict] = field(default_factory=dict)
    threat_model: ThreatModelConfig = field(default_factory=ThreatModelConfig)

    def report_for(self, area: str) -> ReportConfig:
        """The reasoning model's settings for one area: [llm.report] with the area's own values."""
        return replace(self.report, **self.report_overrides.get(area, {}))


def load_config(path: Path | None) -> LlmConfig:
    """The LLM settings; the defaults when there is no configuration file (tests)."""
    try:
        data = read_config(path)
        llm = section(data, path, "llm")
        _known_keys(llm, LLM_KEYS + ("input", "decision", "report", "threat_model"), path, "llm")
        areas = llm.get("areas", list(AREAS))
        if not isinstance(areas, list) or not areas or any(area not in AREAS for area in areas):
            raise ConfigFileError(f"{path}: llm.areas must list one or more of {', '.join(AREAS)}.")
        report_table = section(data, path, "llm.report")
        report = _report(report_table, path, "llm.report", ReportConfig())
        overrides = {}
        for area in [key for key, value in report_table.items() if isinstance(value, dict)]:
            if area not in AREAS:
                raise ConfigFileError(f"{path}: [llm.report.{area}] is not an area; use one of {', '.join(AREAS)}.")
            table = report_table[area]
            if "model" in table:
                raise ConfigFileError(f"{path}: llm.report.{area}.model is not possible: all areas use the reasoning "
                                      "model of [llm.report].")
            area_config = _report(table, path, f"llm.report.{area}", report)
            if table:  # an empty table changes nothing
                overrides[area] = {key: getattr(area_config, key) for key in table}
        return LlmConfig(
            report_language=text(llm, "report_language", "English", path, "llm"),
            areas=tuple(dict.fromkeys(areas)),
            input=_input(section(data, path, "llm.input"), path),
            decision=_decision(section(data, path, "llm.decision"), path),
            report=report,
            report_overrides=overrides,
            threat_model=_threat_model(section(data, path, "llm.threat_model"), path),
        )
    except ConfigFileError as exc:
        raise ConfigError(str(exc)) from exc


def _threat_model(table: dict, path: Path | None) -> ThreatModelConfig:
    name = "llm.threat_model"
    defaults = ThreatModelConfig()
    _known_keys(table, tuple(item.name for item in fields(ThreatModelConfig)), path, name)
    create = text(table, "create", defaults.create, path, name).lower()
    if create not in THREAT_MODEL_CREATE:
        raise ConfigFileError(f'{path}: {name}.create must be "generate" (the tool drafts the threat model from three '
                              'questions) or "manual" (you write it from a template).')
    return ThreatModelConfig(
        create=create,
        docs_kib=whole_number(table, "docs_kib", defaults.docs_kib, path, name),
        context_chars=whole_number(table, "context_chars", defaults.context_chars, path, name, minimum=200),
    )


def _known_keys(table: dict, allowed: tuple[str, ...], path: Path | None, name: str) -> None:
    for key in table:
        if key in allowed:
            continue
        if name == "llm" and key in MOVED_TO_MODELS:
            raise ConfigFileError(f"{path}: llm.{key} is set per model now: in [llm.decision] and [llm.report].")
        raise ConfigFileError(f"{path}: {name}.{key} is not a setting; known: {', '.join(allowed)}.")


def _model_settings(table: dict, path: Path | None, name: str, defaults: DecisionConfig | ReportConfig) -> dict:
    keep_alive = table.get("keep_alive", defaults.keep_alive)
    if not isinstance(keep_alive, (str, int)) or isinstance(keep_alive, bool):
        raise ConfigFileError(f'{path}: {name}.keep_alive must be a duration such as "10m" or seconds.')
    return {
        "timeout_seconds": whole_number(table, "timeout_seconds", defaults.timeout_seconds, path, name, minimum=1),
        "retries": whole_number(table, "retries", defaults.retries, path, name),
        "keep_alive": str(keep_alive),
    }


def _decision(table: dict, path: Path | None) -> DecisionConfig:
    name = "llm.decision"
    defaults = DecisionConfig()
    _known_keys(table, tuple(item.name for item in fields(DecisionConfig)), path, name)
    max_questions = whole_number(table, "max_questions", defaults.max_questions, path, name, minimum=1)
    if max_questions > MAX_QUESTIONS:
        raise ConfigFileError(f"{path}: {name}.max_questions must be at most {MAX_QUESTIONS}.")
    return DecisionConfig(
        model=text(table, "model", defaults.model, path, name),
        **_model_settings(table, path, name, defaults),
        max_questions=max_questions,
        max_input_kib=whole_number(table, "max_input_kib", defaults.max_input_kib, path, name, minimum=1),
        review_confidence=number(table, "review_confidence", defaults.review_confidence, path, name, maximum=1.0),
    )


def _report(table: dict, path: Path | None, name: str, defaults: ReportConfig) -> ReportConfig:
    """[llm.report], or an area's table on top of it (`defaults` is then [llm.report])."""
    settings = {key: value for key, value in table.items() if not isinstance(value, dict)}
    _known_keys(settings, tuple(item.name for item in fields(ReportConfig)), path, name)
    min_p = settings.get("min_p", defaults.min_p)
    top_k = settings.get("top_k", defaults.top_k)
    num_ctx = whole_number(settings, "num_ctx", defaults.num_ctx, path, name, minimum=2048)
    num_predict = whole_number(settings, "num_predict", defaults.num_predict, path, name, minimum=256)
    if num_predict >= num_ctx:
        raise ConfigFileError(f"{path}: {name}.num_predict must be smaller than num_ctx (the answer must leave room "
                              "for the prompt).")
    return ReportConfig(
        model=text(settings, "model", defaults.model, path, name),
        **_model_settings(settings, path, name, defaults),
        think=_think(settings.get("think", defaults.think), path, name),
        temperature=number(settings, "temperature", defaults.temperature, path, name, maximum=2.0),
        top_p=number(settings, "top_p", defaults.top_p, path, name, maximum=1.0),
        top_k=None if top_k is None else whole_number({"top_k": top_k}, "top_k", 1, path, name, minimum=1),
        min_p=None if min_p is None else number({"min_p": min_p}, "min_p", 0.0, path, name, maximum=1.0),
        seed=whole_number(settings, "seed", defaults.seed, path, name),
        num_ctx=num_ctx,
        num_predict=num_predict,
        entries_per_request=whole_number(settings, "entries_per_request", defaults.entries_per_request, path, name,
                                         minimum=1),
        chars_per_token=number(settings, "chars_per_token", defaults.chars_per_token, path, name, minimum=0.5),
    )


def _think(value: object, path: Path | None, name: str) -> bool | str:
    """true, false, or a level of the model ("low", "medium", ...: the values /api/show lists)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ConfigFileError(f'{path}: {name}.think must be true, false or a level of the model such as "low", '
                          '"medium" or "high" (the values `/api/show` lists for it).')


def _input(table: dict, path: Path | None) -> InputConfig:
    name = "llm.input"
    defaults = InputConfig()
    _known_keys(table, tuple(item.name for item in fields(InputConfig)), path, name)
    values = {}
    for item in fields(InputConfig):
        default = getattr(defaults, item.name)
        if item.name == "security_single_severity":
            value = text(table, item.name, default, path, name).lower()
            if value not in SEVERITIES:
                raise ConfigFileError(f"{path}: {name}.{item.name} must be one of {', '.join(SEVERITIES)}.")
            values[item.name] = value
        elif item.name == "strip_comments":
            values[item.name] = flag(table, item.name, default, path, name)
        elif item.name == "weak_line_rate":
            values[item.name] = number(table, item.name, default, path, name, maximum=1.0)
        elif item.name == "code_context_lines":
            values[item.name] = whole_number(table, item.name, default, path, name)
        else:
            values[item.name] = whole_number(table, item.name, default, path, name, minimum=1)
    if values["group_samples_with_code"] > values["group_samples"]:
        raise ConfigFileError(f"{path}: {name}.group_samples_with_code must be at most group_samples.")
    return InputConfig(**values)

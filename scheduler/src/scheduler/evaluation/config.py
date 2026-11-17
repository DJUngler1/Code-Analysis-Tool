"""[llm], [llm.decision] and [llm.report] of code-analysis.toml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scheduler.tool_config import ConfigFileError, flag, number, read_config, section, text, whole_number

AREAS = ("security", "code_quality", "test_quality")
MAX_QUESTIONS = 64  # limit of Ollama's decision endpoint
MAX_REQUEST_KIB = 64  # local limit of the decision endpoint (text and questions)


class ConfigError(Exception):
    """[llm] in code-analysis.toml is invalid."""


@dataclass(frozen=True)
class DecisionConfig:
    model: str = "pplx-decider-flash:latest"
    max_questions: int = 8
    max_input_kib: int = 20
    review_confidence: float = 0.6


@dataclass(frozen=True)
class ReportConfig:
    model: str = "qwen3.8:latest"
    think: bool = True
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    seed: int = 42
    num_ctx: int = 16384
    num_predict: int = 6144
    entries_per_request: int = 10

    def options(self) -> dict:
        """Ollama's generation options for /api/chat."""
        return {"temperature": self.temperature, "top_p": self.top_p, "top_k": self.top_k, "seed": self.seed,
                "num_ctx": self.num_ctx, "num_predict": self.num_predict}


@dataclass(frozen=True)
class LlmConfig:
    report_language: str = "English"
    areas: tuple[str, ...] = AREAS
    timeout_seconds: int = 900
    retries: int = 2
    keep_alive: str = "10m"
    decision: DecisionConfig = field(default_factory=DecisionConfig)
    report: ReportConfig = field(default_factory=ReportConfig)


def load_config(path: Path | None) -> LlmConfig:
    """The LLM settings; the defaults when there is no configuration file (tests)."""
    try:
        data = read_config(path)
        llm = section(data, path, "llm")
        decision = section(data, path, "llm.decision")
        report = section(data, path, "llm.report")
        areas = llm.get("areas", list(AREAS))
        if not isinstance(areas, list) or not areas or any(area not in AREAS for area in areas):
            raise ConfigFileError(f"{path}: llm.areas must list one or more of {', '.join(AREAS)}.")
        keep_alive = llm.get("keep_alive", "10m")
        if not isinstance(keep_alive, (str, int)) or isinstance(keep_alive, bool):
            raise ConfigFileError(f'{path}: llm.keep_alive must be a duration such as "10m" or seconds.')
        max_questions = whole_number(decision, "max_questions", 8, path, "llm.decision", minimum=1)
        if max_questions > MAX_QUESTIONS:
            raise ConfigFileError(f"{path}: llm.decision.max_questions must be at most {MAX_QUESTIONS}.")
        max_input = whole_number(decision, "max_input_kib", 20, path, "llm.decision", minimum=1)
        return LlmConfig(
            report_language=text(llm, "report_language", "English", path, "llm"),
            areas=tuple(dict.fromkeys(areas)),
            timeout_seconds=whole_number(llm, "timeout_seconds", 900, path, "llm", minimum=1),
            retries=whole_number(llm, "retries", 2, path, "llm"),
            keep_alive=str(keep_alive),
            decision=DecisionConfig(
                model=text(decision, "model", DecisionConfig.model, path, "llm.decision"),
                max_questions=max_questions,
                max_input_kib=max_input,
                review_confidence=number(decision, "review_confidence", 0.6, path, "llm.decision", maximum=1.0),
            ),
            report=ReportConfig(
                model=text(report, "model", ReportConfig.model, path, "llm.report"),
                think=flag(report, "think", True, path, "llm.report"),
                temperature=number(report, "temperature", 0.6, path, "llm.report", maximum=2.0),
                top_p=number(report, "top_p", 0.95, path, "llm.report", maximum=1.0),
                top_k=whole_number(report, "top_k", 20, path, "llm.report", minimum=1),
                seed=whole_number(report, "seed", 42, path, "llm.report"),
                num_ctx=whole_number(report, "num_ctx", 16384, path, "llm.report", minimum=2048),
                num_predict=whole_number(report, "num_predict", 6144, path, "llm.report", minimum=256),
                entries_per_request=whole_number(report, "entries_per_request", 10, path, "llm.report", minimum=1),
            ),
        )
    except ConfigFileError as exc:
        raise ConfigError(str(exc)) from exc

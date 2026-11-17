"""Ollama's HTTP API for step 6.

- decide(): the decision endpoint POST /v1/systemone (System One models such as
  pplx-decider-flash). A shared `state` and up to 64 named questions; each
  `choice` question gets the most likely option, the probability of each
  option and a confidence. No free text, no generation options.
- chat(): POST /api/chat with `format` (a JSON schema the answer must follow)
  and `think` (reasoning models such as qwen3.8).
- installed(), loaded(), unload(): /api/tags, /api/ps and unloading a model,
  so the decision model and the reasoning model are never loaded together.

After `max_failures` failed requests in a row (Ollama unreachable, or a server
error such as a crashed model runner), the next failure raises
OllamaUnavailable: step 6 stops instead of sending more requests to a server
that is down or out of memory. Requests Ollama rejects (4xx) do not count.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

RETRY_PAUSE_SECONDS = 5.0
MAX_FAILURES_IN_A_ROW = 3


class OllamaError(Exception):
    """Ollama is unreachable, or a request failed."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class OllamaUnavailable(OllamaError):
    """Several requests in a row failed: Ollama is down or cannot run the model."""


@dataclass
class Exchange:
    """One request and its answer, as written to the requests/ folder."""

    endpoint: str
    request: dict
    response: dict | None
    duration_seconds: float
    error: str | None = None
    attempts: int = 1


Post = Callable[[str, dict, float], dict]


class OllamaClient:
    def __init__(self, base_url: str, *, timeout: float = 900.0, retries: int = 2, keep_alive: str = "10m",
                 post: Post | None = None, pause: float = RETRY_PAUSE_SECONDS,
                 max_failures: int = MAX_FAILURES_IN_A_ROW):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.keep_alive = keep_alive
        self._post = post or self._http_post
        self._pause = pause
        self.max_failures = max_failures
        self._failures_in_a_row = 0

    # --- requests ---

    def decide(self, model: str, state: str, questions: dict[str, dict], *, retries: int | None = None) -> Exchange:
        body = {"model": model, "state": state, "questions": questions, "keep_alive": self.keep_alive}
        return self._exchange("/v1/systemone", body, self.retries if retries is None else retries)

    def chat(self, model: str, system: str, user: str, schema: dict, *, options: dict, think: bool) -> Exchange:
        body = {"model": model, "stream": False, "think": think, "format": schema, "options": options,
                "keep_alive": self.keep_alive,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        return self._exchange("/api/chat", body, self.retries)

    def _exchange(self, endpoint: str, body: dict, retries: int) -> Exchange:
        started = time.monotonic()
        error = None
        rejected = False
        for attempt in range(1, retries + 2):
            try:
                response = self._post(endpoint, body, self.timeout)
                self._failures_in_a_row = 0
                return Exchange(endpoint, body, response, round(time.monotonic() - started, 1), attempts=attempt)
            except OllamaError as exc:
                error = str(exc)
                # A request Ollama rejects (4xx) fails the same way again.
                rejected = exc.status is not None and 400 <= exc.status < 500
                if rejected:
                    break
                if attempt <= retries:
                    time.sleep(self._pause)
        if not rejected:
            self._failures_in_a_row += 1
            if self._failures_in_a_row >= self.max_failures:
                raise OllamaUnavailable(f"Ollama stopped answering: {self._failures_in_a_row} requests in a row "
                                        f"failed (last error: {error}). Check that Ollama runs and has enough "
                                        "memory for the model.")
        return Exchange(endpoint, body, None, round(time.monotonic() - started, 1), error, attempt)

    # --- models ---

    def installed(self) -> dict[str, str]:
        """Installed models: name -> digest."""
        data = self._get("/api/tags")
        return {model.get("name", "?"): model.get("digest", "") for model in data.get("models", [])
                if isinstance(model, dict)}

    def loaded(self) -> list[str]:
        data = self._get("/api/ps")
        return [model.get("name", "?") for model in data.get("models", []) if isinstance(model, dict)]

    def unload(self, model: str) -> bool:
        """Unload a model now (keep_alive 0); True if it is no longer loaded."""
        try:
            self._post("/api/generate", {"model": model, "keep_alive": 0}, 60.0)
        except OllamaError:
            pass  # e.g. a decision model that /api/generate does not serve; checked below
        try:
            for _ in range(10):
                if model not in self.loaded():
                    return True
                time.sleep(0.5)
        except OllamaError:
            return False
        return False

    # --- HTTP ---

    def _get(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as response:
                data = json.load(response)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OllamaError(f"Ollama is not reachable at {self.base_url}: {_reason(exc)}") from exc
        return data if isinstance(data, dict) else {}

    def _http_post(self, path: str, body: dict, timeout: float) -> dict:
        request = urllib.request.Request(f"{self.base_url}{path}", json.dumps(body).encode(),
                                         {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            try:
                detail = json.loads(detail).get("error", detail)
            except (ValueError, AttributeError):
                pass
            raise OllamaError(f"{path}: HTTP {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise OllamaError(f"{path}: {_reason(exc)}") from exc
        if not isinstance(data, dict):
            raise OllamaError(f"{path}: the answer is not a JSON object")
        return data


def model_key(name: str) -> str:
    """Ollama's name of a model: "qwen3.8" and "qwen3.8:latest" are the same model."""
    name = name.strip()
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _reason(exc: Exception) -> str:
    reason = getattr(exc, "reason", None) or exc
    return str(reason) or type(exc).__name__

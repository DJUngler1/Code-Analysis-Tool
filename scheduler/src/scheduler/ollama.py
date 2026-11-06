"""Reachability check for the Ollama installation on the host."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass(frozen=True)
class OllamaStatus:
    base_url: str
    reachable: bool
    models: tuple[str, ...] = ()
    error: str | None = None


def check_ollama(base_url: str, timeout: float = 3.0) -> OllamaStatus:
    """Ask Ollama for its installed models (GET /api/tags)."""
    try:
        with urllib.request.urlopen(f"{base_url}/api/tags", timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.URLError as exc:
        return OllamaStatus(base_url, reachable=False, error=str(exc.reason))
    except (OSError, ValueError) as exc:
        return OllamaStatus(base_url, reachable=False, error=str(exc) or type(exc).__name__)

    models = data.get("models", []) if isinstance(data, dict) else []
    names = tuple(m.get("name", "?") for m in models if isinstance(m, dict))
    return OllamaStatus(base_url, reachable=True, models=names)

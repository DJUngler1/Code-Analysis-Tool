"""A small client for SonarQube's Web API (standard library only)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Protocol

TIMEOUT_SECONDS = 60


class SonarError(Exception):
    """The server is not reachable or answered with an error."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class SonarApi(Protocol):
    """What sonarqube.py needs from the server; tests provide a fake."""

    def get(self, path: str, params: Mapping[str, object] | None = None) -> dict: ...

    def post(self, path: str, params: Mapping[str, object] | None = None) -> dict: ...


class HttpSonarApi:
    """Calls the Web API, authenticated as a user (login, password) or with a token."""

    def __init__(self, base_url: str, *, login: str | None = None, password: str = "",
                 token: str | None = None, timeout: float = TIMEOUT_SECONDS) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._headers = {"Accept": "application/json"}
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
        elif login:
            credentials = base64.b64encode(f"{login}:{password}".encode()).decode()
            self._headers["Authorization"] = f"Basic {credentials}"

    def get(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        query = f"?{urllib.parse.urlencode(_strings(params))}" if params else ""
        return self._send(urllib.request.Request(f"{self.base_url}/{path}{query}", headers=self._headers))

    def post(self, path: str, params: Mapping[str, object] | None = None) -> dict:
        body = urllib.parse.urlencode(_strings(params or {})).encode()
        headers = {**self._headers, "Content-Type": "application/x-www-form-urlencoded"}
        return self._send(urllib.request.Request(f"{self.base_url}/{path}", data=body, headers=headers, method="POST"))

    def _send(self, request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 – fixed http(s) URL
                text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SonarError(f"{request.get_method()} {request.full_url.split('?')[0]}: HTTP {exc.code} "
                             f"{_error_message(detail)}", exc.code) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise SonarError(f"SonarQube is not reachable at {self.base_url}: {exc}") from exc
        if not text.strip():
            return {}
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SonarError(f"{request.full_url.split('?')[0]}: no JSON in the answer") from exc
        return data if isinstance(data, dict) else {"items": data}


def _strings(params: Mapping[str, object]) -> dict[str, str]:
    return {key: str(value) for key, value in params.items() if value is not None}


def _error_message(text: str) -> str:
    try:
        errors = json.loads(text).get("errors", [])
        return "; ".join(str(error.get("msg", "")) for error in errors) or text[:200]
    except (json.JSONDecodeError, AttributeError):
        return text[:200]

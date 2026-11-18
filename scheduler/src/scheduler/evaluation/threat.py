"""Step 6: the threat model a run used, from <run folder>/threat-model.md (written by scheduler.threat_model).

Only the security area uses it: the decision prompt and the report prompt get it as {{THREAT_MODEL}}.
The text is shortened to [llm.threat_model] context_chars, because the decision model reads the shared input
once per question. HTML comments (hints for the user in the draft) are left out.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FILE = "threat-model.md"
INFO = "threat-model.json"
NO_THREAT_MODEL = "No threat model was provided for this run."
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def clean(text: str) -> str:
    """The text the models see: without HTML comments, trimmed."""
    return _COMMENT.sub("", text).strip()


def run_threat_model(run_dir: Path, limit: int) -> tuple[str | None, dict | None]:
    """The run's threat model for the prompts (None if there is none), and what summary.json records about it."""
    path = run_dir / FILE
    if not path.is_file():
        return None, None
    text = clean(path.read_text(encoding="utf-8", errors="replace"))
    if not text:
        return None, None
    try:
        info = json.loads((run_dir / INFO).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = {}
    shown = text if len(text) <= limit else text[:limit].rstrip() + " …"
    return shown, {key: info.get(key) for key in ("source", "path", "confirmed_at", "sha256")} | {
        "chars": len(text), "limit": limit, "shortened": len(text) > limit}

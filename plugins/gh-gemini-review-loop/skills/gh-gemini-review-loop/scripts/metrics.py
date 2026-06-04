"""Local, identity-free run metrics for the Gemini review loop.

Pure module: no network, no imports from fetch_gemini_threads. Owns the
runs.jsonl schema, append/load, run-summary formatting, and aggregation.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

RECORD_SCHEMA_VERSION = 1
DEFAULT_WINDOW = 10

JUDGE_VERDICTS = (
    "valid_actionable",
    "false_positive",
    "needs_human",
    "explanation_only",
    "duplicate",
    "already_addressed",
)
JUDGE_ACTIONS = ("fix", "reply", "ignore", "escalate")
VALID_OUTCOMES = (
    "clean",
    "capped",
    "human",
    "regression",
    "no_progress",
    "verification_failed",
)
VALID_VERIFICATION = ("passed", "failed", "skipped")

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def runs_log_path() -> Path:
    """Path to runs.jsonl, beside state.json; override via GGRL_STATE_DIR."""
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "runs.jsonl"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime(_TS_FMT)


def top_dir(path: str) -> str:
    """First path segment of a repo-relative path, or '(unknown)' if empty."""
    if not path:
        return "(unknown)"
    parts = Path(path).parts
    return parts[0] if parts else "(unknown)"


def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, _sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"

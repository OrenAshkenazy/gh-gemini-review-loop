"""Centralized color handling for human-readable Gemini loop output."""

from __future__ import annotations

import os

PURPLE = "\033[95m"
RESET = "\033[0m"


def colors_enabled(*, no_color: bool = False) -> bool:
    """Return whether human loop output should be colorized."""
    if no_color:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("GGRL_NO_COLOR") is not None:
        return False
    return True


def color_loop(text: str, *, enabled: bool = True) -> str:
    """Wrap a human-readable loop block in purple, preserving its text."""
    if not enabled:
        return text
    return f"{PURPLE}{text}{RESET}"

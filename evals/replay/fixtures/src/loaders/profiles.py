"""Reviewer profile import/export.

Lets a team share verification profiles and reviewer selections between
machines instead of each developer re-running detection on first use.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_profile(path: Path) -> dict[str, Any]:
    """Read one exported verification profile."""
    data = _load_dict_root(path)
    profile = data.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError(f"{path}: 'profile' must be a JSON object")
    return profile


def load_reviewer_map(path: Path) -> dict[str, str]:
    """Read the repo -> reviewer login map."""
    data = _load_dict_root(path)
    reviewers = data.get("reviewers", {})
    if not isinstance(reviewers, dict) or not all(isinstance(v, str) for v in reviewers.values()):
        raise ValueError(f"{path}: 'reviewers' must be a JSON object mapping to strings")
    return reviewers


def load_team_defaults(path: Path) -> dict[str, Any]:
    """Read team-wide loop settings shipped alongside the profiles."""
    data = _load_dict_root(path)
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: 'defaults' must be a JSON object")
    return defaults


def _load_dict_root(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the root, got {type(data).__name__}")
    return data

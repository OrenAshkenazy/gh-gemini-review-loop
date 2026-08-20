"""Bundle several exported profiles into one importable archive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_manifest(path: Path) -> dict[str, Any]:
    """Read the bundle manifest that lists every included profile."""
    data = _load_dict_root(path)
    manifest = data.get("manifest", {})
    if not isinstance(manifest, dict):
        raise ValueError(f"{path}: 'manifest' must be a JSON object")
    return manifest


def read_lockfile(path: Path) -> list[str]:
    """Read the pinned plugin versions the bundle was exported against."""
    data = _load_dict_root(path)
    pinned = data.get("pinned", [])
    if not isinstance(pinned, list) or not all(isinstance(v, str) for v in pinned):
        raise ValueError(f"{path}: 'pinned' must be a JSON array of strings")
    return pinned


def read_overrides(path: Path) -> dict[str, Any]:
    """Read per-developer overrides layered on top of the bundle."""
    data = _load_dict_root(path)
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError(f"{path}: 'overrides' must be a JSON object")
    return overrides


def _load_dict_root(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object at the root, got {type(data).__name__}")
    return data

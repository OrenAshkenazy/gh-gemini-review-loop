#!/usr/bin/env python3
"""Deterministic verification-stack detection for gh-gemini-review-loop.

Pure function of the filesystem: inspects marker files in a repo root and
emits a transient JSON candidate profile. Does NOT read or write preferences;
persistence and prose-reconciliation happen in the agent/judge layers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _check(name: str, command: str, required: bool) -> dict[str, Any]:
    return {"name": name, "command": command, "required": required}


def _detect_python(root: Path) -> dict[str, Any]:
    reasons: list[str] = []
    pyproject = root / "pyproject.toml"
    has_pyproject = pyproject.is_file()
    if has_pyproject:
        reasons.append("pyproject.toml")
    if (root / "setup.py").is_file():
        reasons.append("setup.py")
    if (root / "tests").is_dir():
        reasons.append("tests/")
    checks = [_check("tests", "pytest", True)]
    # is_file() can still be followed by an OSError on read (permissions, broken
    # symlink); degrade to no optional-tool detection rather than crash, matching
    # the guarded read in _detect_node.
    deps_text = ""
    if has_pyproject:
        try:
            deps_text = pyproject.read_text(encoding="utf-8")
        except OSError:
            deps_text = ""
    if "ruff" in deps_text:
        checks.append(_check("lint", "ruff check .", True))
    if "mypy" in deps_text:
        checks.append(_check("typecheck", "mypy .", False))
    return {
        "stack": "python",
        "confidence": "high" if has_pyproject else "medium",
        "reasons": reasons,
        "candidate_checks": checks,
    }


def _detect_node(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    raw_scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    # `scripts` can be any JSON type in a hand-edited/corrupt package.json; a
    # non-dict would make `script_key in scripts` raise TypeError below.
    scripts = raw_scripts if isinstance(raw_scripts, dict) else {}
    mapping = [
        ("test", "tests", "npm test", True),
        ("lint", "lint", "npm run lint", True),
        ("typecheck", "typecheck", "npm run typecheck", False),
    ]
    checks = [
        _check(name, cmd, required)
        for script_key, name, cmd, required in mapping
        if script_key in scripts
    ]
    return {
        "stack": "node",
        "confidence": "high",
        "reasons": ["package.json"],
        "candidate_checks": checks,
    }


def _detect_rust(root: Path) -> dict[str, Any]:
    return {
        "stack": "rust",
        "confidence": "high",
        "reasons": ["Cargo.toml"],
        "candidate_checks": [
            _check("tests", "cargo test", True),
            _check("lint", "cargo clippy", False),
        ],
    }


def _detect_go(root: Path) -> dict[str, Any]:
    return {
        "stack": "go",
        "confidence": "high",
        "reasons": ["go.mod"],
        "candidate_checks": [
            _check("tests", "go test ./...", True),
            _check("vet", "go vet ./...", False),
        ],
    }


def detect(repo_root: Path | str) -> dict[str, Any]:
    """Return {stack, confidence, reasons, candidate_checks} for ``repo_root``.

    Strong, unambiguous stack markers (pyproject.toml, setup.py, package.json,
    Cargo.toml, go.mod) are checked first. A bare ``tests/`` directory is a weak
    signal — common across many languages — so it is only used as a Python
    fallback when no strong marker is present. Unknown stacks return a
    low-confidence empty candidate so the caller falls back to ad-hoc verification.
    """
    root = Path(repo_root)
    # Strong markers first. Use is_file() so a directory that happens to share a
    # marker name (e.g. a dir literally named pyproject.toml) is not mistaken for
    # the file and does not crash a later read_text().
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        return _detect_python(root)
    if (root / "package.json").is_file():
        return _detect_node(root)
    if (root / "Cargo.toml").is_file():
        return _detect_rust(root)
    if (root / "go.mod").is_file():
        return _detect_go(root)
    # Weak fallback: a bare tests/ directory suggests Python.
    if (root / "tests").is_dir():
        return _detect_python(root)
    return {
        "stack": "unknown",
        "confidence": "low",
        "reasons": [],
        "candidate_checks": [],
    }


def main(argv: list[str]) -> int:
    root = argv[1] if len(argv) > 1 else "."
    print(json.dumps(detect(root), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

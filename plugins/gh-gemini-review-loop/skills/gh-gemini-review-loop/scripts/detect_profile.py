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
    if pyproject.exists():
        reasons.append("pyproject.toml")
    if (root / "setup.py").exists():
        reasons.append("setup.py")
    if (root / "tests").is_dir():
        reasons.append("tests/")
    checks = [_check("tests", "pytest", True)]
    deps_text = pyproject.read_text(encoding="utf-8") if pyproject.exists() else ""
    if "ruff" in deps_text:
        checks.append(_check("lint", "ruff check .", True))
    if "mypy" in deps_text:
        checks.append(_check("typecheck", "mypy .", False))
    return {
        "stack": "python",
        "confidence": "high" if pyproject.exists() else "medium",
        "reasons": reasons,
        "candidate_checks": checks,
    }


def _detect_node(root: Path) -> dict[str, Any]:
    try:
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
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

    Detection order is fixed: python, node, rust, go. The first matching marker
    wins. Unknown stacks return a low-confidence empty candidate so the caller
    falls back to ad-hoc verification.
    """
    root = Path(repo_root)
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (
        root / "tests"
    ).is_dir():
        return _detect_python(root)
    if (root / "package.json").exists():
        return _detect_node(root)
    if (root / "Cargo.toml").exists():
        return _detect_rust(root)
    if (root / "go.mod").exists():
        return _detect_go(root)
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

#!/usr/bin/env python3
"""Deterministic verification-stack detection for gh-gemini-review-loop.

Pure function of the filesystem: inspects marker files in a repo root and
emits a transient JSON candidate profile. Does NOT read or write preferences;
persistence and prose-reconciliation happen in the agent/judge layers.
"""
from __future__ import annotations

import json
import re
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
            # errors="replace" + catching ValueError so invalid UTF-8 bytes
            # (UnicodeDecodeError is a ValueError, not OSError) degrade rather
            # than crash the detection loop.
            deps_text = pyproject.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            deps_text = ""
    # Word-boundary match so a dependency like "gruff" does not false-positive
    # as "ruff".
    if re.search(r"\bruff\b", deps_text):
        checks.append(_check("lint", "ruff check .", True))
    if re.search(r"\bmypy\b", deps_text):
        checks.append(_check("typecheck", "mypy .", False))
    return {
        "stack": "python",
        "confidence": "high" if has_pyproject else "medium",
        "reasons": reasons,
        "candidate_checks": checks,
    }


def _detect_node(root: Path) -> dict[str, Any]:
    try:
        # errors="replace" guards invalid UTF-8 (UnicodeDecodeError is a
        # ValueError, not OSError/JSONDecodeError); ValueError also covers
        # json.JSONDecodeError (its subclass) for malformed content.
        text = (root / "package.json").read_text(encoding="utf-8", errors="replace")
        data = json.loads(text)
    except (OSError, ValueError):
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


def _required(check: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``check`` forced to required=True, preserving cwd.

    v1 has a single gating tier: every check in a saved profile is required.
    Detection may mark a check optional (e.g. mypy); when it becomes a gate we
    normalize it to required so persistence and run_profile gate on it. A
    per-check ``working_directory`` (monorepo paths) is carried through.
    """
    out: dict[str, Any] = {
        "name": check["name"],
        "command": check["command"],
        "required": True,
    }
    cwd = check.get("working_directory")
    if isinstance(cwd, str) and cwd:
        out["working_directory"] = cwd
    return out


def _commands_label(checks: list[dict[str, Any]]) -> str:
    return " + ".join(c["command"] for c in checks)


def build_presets(candidate_checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build the explicit ordered preset menu from detector candidate checks.

    Pure function. Returns a list of preset dicts, each shaped::

        {"label": str, "checks": list[check], "source": str | None,
         "customize": bool}

    Rules:
    - Empty candidates (unknown stack) -> ``[]``; the caller shows no menu and
      falls back to ad-hoc verification.
    - "All detected" -> every candidate check, each required=True,
      ``source="confirmed"``. Always present.
    - A narrower option appears only for multi-check repos (omitted when it
      would duplicate "All detected"): "Tests only" when a check named ``tests``
      exists, else "First check only" (the first candidate). ``source="customized"``.
    - "Skip - use ad-hoc verification" -> ``source="skipped"``, no checks.
    - "Customize manually" -> escape option (``customize=True``, ``source=None``,
      no checks); the caller hands off to the free-form NL customize path.

    Every option is emitted here. Nothing relies on the prompt tool auto-adding
    an "Other"/escape option.
    """
    if not candidate_checks:
        return []
    all_checks = [_required(c) for c in candidate_checks]
    presets: list[dict[str, Any]] = [
        {
            "label": f"All detected — {_commands_label(all_checks)}",
            "checks": all_checks,
            "source": "confirmed",
            "customize": False,
        }
    ]
    if len(candidate_checks) > 1:
        tests = next((c for c in candidate_checks if c["name"] == "tests"), None)
        narrow = _required(tests if tests is not None else candidate_checks[0])
        label_prefix = "Tests only" if tests is not None else "First check only"
        presets.append(
            {
                "label": f"{label_prefix} — {narrow['command']}",
                "checks": [narrow],
                "source": "customized",
                "customize": False,
            }
        )
    presets.append(
        {
            "label": "Skip — use ad-hoc verification",
            "checks": [],
            "source": "skipped",
            "customize": False,
        }
    )
    presets.append(
        {
            "label": "Customize manually",
            "checks": [],
            "source": None,
            "customize": True,
        }
    )
    return presets


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
    result = detect(root)
    result["presets"] = build_presets(result["candidate_checks"])
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

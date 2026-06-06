#!/usr/bin/env python3
"""Deterministic verification-stack detection for gh-gemini-review-loop.

Pure function of the filesystem: inspects marker files in a repo root and
emits a transient JSON candidate profile. Does NOT read or write preferences;
persistence and prose-reconciliation happen in the agent/judge layers.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


# Recipe names that indicate a verification step. Anchored, case-insensitive:
# exact "test"/"check"/"lint"/"typecheck"/"verify", or a hyphenated test
# variant ("test-backend", "client-tests").
_VERIFY_RECIPE_RE = re.compile(
    r"^(test|check|lint|typecheck|verify)$"
    r"|^test-[\w-]+$"
    r"|^[\w-]+-tests?$",
    re.IGNORECASE,
)

# A recipe header line: name, optional space-separated parameters, then a single
# ':' that is NOT ':=' (which would be a just assignment). Dependencies appear
# after the colon and are intentionally not captured.
_RECIPE_HEADER_RE = re.compile(
    r"^(?P<name>[A-Za-z_][\w-]*)(?P<params>(?:[ \t]+[^:#\n]+?)?)[ \t]*:(?!=)"
)

_JUSTFILE_NAMES = ("justfile", "Justfile", ".justfile")


def _recipe_name_matches(name: str) -> bool:
    return bool(_VERIFY_RECIPE_RE.match(name))


def _recipe_is_runnable(params: str) -> bool:
    """True iff every parameter is safe to omit when calling ``just <recipe>``.

    just parameter forms:
      ``name``            required positional   -> NOT runnable
      ``name="default"``  defaulted             -> runnable
      ``+name``           required variadic     -> NOT runnable
      ``*name``           optional variadic     -> runnable

    Splitting on whitespace can mis-handle a default containing spaces
    (``p="a b"``); the worst case is a conservative false-skip, never a broken
    emitted check.
    """
    for token in params.split():
        if token.startswith("*"):
            continue
        if token.startswith("+"):
            return False
        if "=" in token:
            continue
        return False
    return True


def parse_justfile_recipes(root: Path | str) -> list[dict[str, Any]]:
    """Return emittable verification checks parsed from a repo-root justfile.

    A recipe is emitted only when its name matches a verification pattern AND it
    is zero-arg-runnable (parameter guard). Returns ``[]`` when no justfile
    exists, none match, or all matches require arguments.
    """
    root = Path(root)
    text = ""
    for fname in _JUSTFILE_NAMES:
        path = root / fname
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                text = ""
            break
    checks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        # Recipe headers start in column 0; recipe bodies are indented.
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        match = _RECIPE_HEADER_RE.match(line)
        if not match:
            continue
        name = match.group("name")
        if name in seen or not _recipe_name_matches(name):
            continue
        if not _recipe_is_runnable(match.group("params")):
            continue
        seen.add(name)
        checks.append({
            "name": name,
            "command": f"just {name}",
            "working_directory": ".",
            "required": True,
        })
    return checks


_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})

# Marker filename -> (runner command, check-name hint). First marker found
# walking up from a test dir wins.
_MARKER_RUNNERS: list[tuple[str, str]] = [
    ("package.json", "npm test"),
    ("pyproject.toml", "pytest"),
    ("setup.py", "pytest"),
    ("Cargo.toml", "cargo test"),
    ("go.mod", "go test ./..."),
]


def _tracked_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
            ["git", "ls-files"],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return [ln for ln in proc.stdout.splitlines() if ln]


def _nearest_marker_dir(root: Path, start: Path) -> tuple[str, str] | None:
    """Walk up from ``start`` (inclusive) to ``root``; return (cwd, command).

    ``cwd`` is the marker directory relative to ``root`` ('.' for root itself).
    """
    current = start
    while True:
        for marker, command in _MARKER_RUNNERS:
            if (current / marker).is_file():
                rel = current.relative_to(root).as_posix()
                return (rel if rel != "." else ".", command)
        if current == root:
            return None
        current = current.parent


def discover_git_tree_checks(root: Path | str) -> list[dict[str, Any]]:
    """Discover monorepo test dirs from tracked files; map to runner + cwd.

    One check per nearest-marker directory (deduped). Test dirs with no marker
    up-tree are skipped. Returns ``[]`` outside a git repo.
    """
    root = Path(root)
    # Collect candidate test directories from tracked file paths.
    test_dirs: list[Path] = []
    seen_dirs: set[Path] = set()
    for rel in _tracked_files(root):
        parts = Path(rel).parts
        for i, part in enumerate(parts):
            if part in _TEST_DIR_NAMES:
                d = root / Path(*parts[: i + 1])
                if d not in seen_dirs:
                    seen_dirs.add(d)
                    test_dirs.append(d)
    checks: list[dict[str, Any]] = []
    seen_cwd: set[str] = set()
    for test_dir in test_dirs:
        found = _nearest_marker_dir(root, test_dir)
        if found is None:
            continue
        cwd, command = found
        if cwd in seen_cwd:
            continue
        seen_cwd.add(cwd)
        name = cwd.replace("/", "-") if cwd != "." else "root"
        checks.append({
            "name": name,
            "command": command,
            "working_directory": cwd,
            "required": True,
        })
    return checks


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

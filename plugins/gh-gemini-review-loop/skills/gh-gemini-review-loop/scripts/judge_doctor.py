#!/usr/bin/env python3
"""Diagnostic CLI for judge eval setup.

Run with no arguments to print a step-by-step report of every check the
judge needs to pass. Each failed check prints the exact command to fix it
on this machine — no generic advice, no broken copy-paste.

Exit codes:
  0  — all checks passed; judge eval is ready
  1  — one or more checks failed
  2  — unexpected internal error (bug in the doctor itself)

Designed to be safe to run anywhere: no network call unless ``--probe`` is
passed AND the prior checks pass. Without ``--probe`` the doctor is purely
read-only.
"""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge import (  # noqa: E402
    DEFAULT_MODEL,
    JudgeClient,
    JudgeError,
    looks_like_placeholder_key,
)


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _color(text: str, code: str) -> str:
    """Apply ANSI color only when stdout is a TTY (keeps log files clean).

    Uses ``getattr`` because some environments (GUI IDE consoles, custom
    test runners, background daemons) replace ``sys.stdout`` with a stream
    that has no ``isatty`` method — directly calling it would raise
    ``AttributeError`` and crash the doctor before it could report anything.
    """
    isatty = getattr(sys.stdout, "isatty", None)
    if not isatty or not isatty():
        return text
    return f"{code}{text}{RESET}"


def _ok(label: str, detail: str = "") -> None:
    mark = _color("✓", GREEN)
    suffix = f"  {_color(detail, DIM)}" if detail else ""
    print(f"  {mark} {label}{suffix}")


def _warn(label: str, fix: str) -> None:
    mark = _color("!", YELLOW)
    print(f"  {mark} {label}")
    print(f"    {_color('fix:', DIM)} {fix}")


def _fail(label: str, fix: str) -> None:
    mark = _color("✗", RED)
    print(f"  {mark} {label}")
    print(f"    {_color('fix:', DIM)} {fix}")


def check_python() -> bool:
    """Verify Python 3.10+ and report the interpreter being used."""
    print(_color("\n[1/5] Python interpreter", BOLD))
    py = sys.executable or "python3"
    major, minor = sys.version_info[:2]
    version = f"{major}.{minor}.{sys.version_info.micro}"
    if (major, minor) < (3, 10):
        _fail(
            f"Python {version} at {py}",
            "Need Python 3.10+. Try a newer interpreter (e.g. python3.11) "
            "and re-run this doctor with: python3.11 "
            f"{Path(__file__).name}",
        )
        return False
    _ok(f"Python {version}", py)
    return True


def check_openai_sdk() -> bool:
    """Verify the openai SDK (v1.0.0+) is importable from THIS Python."""
    print(_color("\n[2/5] openai SDK", BOLD))
    # shlex.quote so the printed command survives spaces in the interpreter
    # path (common on macOS: '/Users/me/My Project/.venv/bin/python').
    py = shlex.quote(sys.executable or "python3")
    try:
        import openai  # noqa: PLC0415
    except ImportError:
        _fail(
            "openai SDK not installed for this Python",
            f"{py} -m pip install -U openai\n"
            "    (if pip blocks with 'externally-managed-environment':\n"
            f"       {py} -m pip install --break-system-packages -U openai\n"
            "     or use pipx / a venv)",
        )
        return False
    try:
        from openai import OpenAI  # noqa: F401, PLC0415
    except ImportError:
        _fail(
            f"openai SDK too old (need v1.0.0+, found {getattr(openai, '__version__', 'unknown')})",
            f"{py} -m pip install -U openai",
        )
        return False
    _ok(f"openai v{getattr(openai, '__version__', 'unknown')}")
    return True


def check_api_key() -> tuple[bool, str | None]:
    """Verify ``OPENAI_API_KEY`` is set and doesn't look like a placeholder.

    Returns (ok, key) so the optional probe step can reuse the key without
    re-reading the env.
    """
    print(_color("\n[3/5] OPENAI_API_KEY", BOLD))
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        keychain_hint = ""
        if sys.platform == "darwin":
            keychain_hint = (
                "\n    If stored in macOS Keychain, add to ~/.zshenv:\n"
                "       export OPENAI_API_KEY=$(security find-generic-password "
                "-a \"$USER\" -s \"openai-api-key\" -w 2>/dev/null)"
            )
        _fail(
            "OPENAI_API_KEY is not set in this environment",
            "Export it from your shell init (not just ~/.zshrc — "
            "subprocesses don't source that). Use ~/.zshenv on zsh." + keychain_hint,
        )
        return False, None
    if looks_like_placeholder_key(key):
        preview = key[:12] + "..." if len(key) > 12 else key
        _fail(
            f"OPENAI_API_KEY looks like a placeholder ({preview!r})",
            "Check ~/.claude/settings.json for a stale "
            '"env": {"OPENAI_API_KEY": "REPLACE_WITH_YOUR_KEY"} block '
            "and delete it. The real key goes in ~/.zshenv (or Keychain).",
        )
        return False, key
    _ok(f"key present and well-formed ({key[:7]}...{key[-4:]})")
    return True, key


def check_settings_json() -> bool:
    """Warn if ~/.claude/settings.json injects a placeholder OPENAI_API_KEY.

    This is the highest-leverage check — the Claude Code env block silently
    overrides the shell-exported value, which is the failure mode we hit
    that wastes the most time to diagnose.
    """
    print(_color("\n[4/5] Claude Code settings.json", BOLD))
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        _ok("no ~/.claude/settings.json (nothing to override env)")
        return True
    try:
        import json  # noqa: PLC0415

        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(
            f"could not parse {settings}: {exc}",
            "Fix or remove the file so it doesn't silently break env injection.",
        )
        return True
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        _ok("no 'env' block in settings.json")
        return True
    injected = env.get("OPENAI_API_KEY")
    if injected is None or injected == "":
        _ok("settings.json does not inject OPENAI_API_KEY")
        return True
    # Explicit type check: looks_like_placeholder_key returns False for
    # non-strings (defensively), which would let an obviously-broken
    # settings.json (boolean true, integer 0) slip through here as
    # "non-placeholder". Catch it before the placeholder heuristic runs.
    if not isinstance(injected, str):
        _fail(
            f"settings.json injects non-string OPENAI_API_KEY "
            f"(type={type(injected).__name__})",
            "OPENAI_API_KEY must be a string. Edit ~/.claude/settings.json "
            "and either delete the line or quote the value as a string.",
        )
        return False
    if looks_like_placeholder_key(injected):
        preview = injected[:12] + "..." if len(injected) > 12 else injected
        _fail(
            f"settings.json injects placeholder OPENAI_API_KEY ({preview!r})",
            "This overrides any shell-exported value. Edit ~/.claude/settings.json "
            "and either delete the OPENAI_API_KEY line from the 'env' block, "
            "or replace the placeholder with your real key. Then restart "
            "Claude Code so the new env takes effect.",
        )
        return False
    _ok("settings.json injects a non-placeholder key")
    return True


def check_gh_cli() -> bool:
    """Verify the gh CLI is installed and authenticated (loop needs it)."""
    print(_color("\n[5/5] gh CLI", BOLD))
    if not shutil.which("gh"):
        _warn(
            "gh CLI not found on PATH",
            "Install: https://cli.github.com/  (not strictly required for the "
            "judge itself, but the surrounding loop needs it)",
        )
        return True  # not fatal for judge; warn-only
    try:
        result = subprocess.run(  # noqa: S603 — fixed args, no shell
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"gh auth status failed: {exc}", "Run: gh auth login")
        return True
    if result.returncode != 0:
        _warn(
            "gh CLI is not authenticated",
            "Run: gh auth login",
        )
        return True
    _ok("gh CLI installed and authenticated")
    return True


def probe_openai(model: str = DEFAULT_MODEL) -> bool:
    """Make one minimal API call to confirm the key actually works.

    Only invoked when ``--probe`` is passed AND all prior checks passed. This
    is the only step that touches the network or costs money (a single
    completion is well under $0.001).
    """
    print(_color(f"\n[probe] live API call to {model}", BOLD))
    client = JudgeClient(model=model)
    ready, reason = client.is_ready()
    if not ready:
        _fail("judge client refused to start", reason or "unknown")
        return False
    finding = {
        "path": "doctor.py",
        "line": 1,
        "severity": "low",
        "body": "Doctor probe — please respond with valid JSON.",
        "diff_hunk": "",
    }
    try:
        result = client.judge(finding)
    except JudgeError as exc:
        _fail("OpenAI call failed", str(exc))
        return False
    if result.status != "ok":
        _fail(
            f"judge returned status={result.status}",
            result.skip_reason or "see raw response",
        )
        return False
    _ok(f"got verdict={result.verdict!r}, confidence={result.confidence:.2f}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="judge_doctor",
        description="Diagnose judge-eval setup (read-only by default).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Make one live API call to verify the key works (costs ~$0.0001).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Model to use for --probe (default: {DEFAULT_MODEL}).",
    )
    args = parser.parse_args(argv)

    print(_color("judge_doctor — gh-gemini-review-loop", BOLD))

    checks = [
        check_python(),
        check_openai_sdk(),
    ]
    key_ok, _key = check_api_key()
    checks.append(key_ok)
    checks.append(check_settings_json())
    checks.append(check_gh_cli())

    all_ok = all(checks)
    print()
    if all_ok:
        print(_color("All checks passed.", GREEN))
        if args.probe:
            probe_ok = probe_openai(model=args.model)
            if not probe_ok:
                print(_color("\nProbe failed.", RED))
                return 1
            print(_color("\nProbe passed — judge eval is fully working.", GREEN))
        else:
            print(_color("Re-run with --probe to verify the key with a live API call.", DIM))
        return 0

    print(_color("One or more checks failed. Fix the items above, then re-run.", RED))
    return 1


if __name__ == "__main__":
    sys.exit(main())

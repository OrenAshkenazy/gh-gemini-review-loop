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
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from judge import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    JudgeClient,
    JudgeError,
    looks_like_placeholder_key,
)
from key_resolver import dotenv_path, resolve_api_key  # noqa: E402


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


def check_network_reachability() -> bool:
    """Verify we can reach the OpenAI API host via TCP.

    Replaces the legacy SDK-install check: the judge now uses stdlib urllib,
    so the failure mode that matters is "can this machine open a TLS
    connection to api.openai.com:443", not "is the openai package importable".
    A pure TCP probe (no API key, no request body) catches firewalls,
    captive portals, and DNS issues without spending a token.
    """
    print(_color("\n[2/5] network reachability", BOLD))
    # Probe whatever endpoint the judge will actually call: OPENAI_BASE_URL
    # if set (self-hosted gateway), otherwise the default. Otherwise users
    # behind firewalls that block api.openai.com but allow their gateway
    # would see this check fail incorrectly.
    base_url = os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    parsed = urlparse(base_url)
    host = parsed.hostname or "api.openai.com"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        _fail(
            f"cannot reach {host}:{port} ({exc})",
            "Check network / VPN / corporate proxy. If you use a custom "
            "gateway (Ollama, LiteLLM, enterprise proxy), set OPENAI_BASE_URL "
            "to its URL.",
        )
        return False
    _ok(f"reached {host}:{port}")
    return True


def check_api_key() -> tuple[bool, str | None]:
    """Verify a key is available via the tiered resolver and well-formed.

    Returns (ok, key) so the optional probe step can reuse the key without
    re-reading the source.
    """
    print(_color("\n[3/5] OPENAI_API_KEY (tiered lookup)", BOLD))
    key, source = resolve_api_key()
    if not key:
        _fail(
            "no OpenAI key in any source",
            "Store one with: python3 "
            f"{SCRIPT_DIR / 'key_resolver.py'} --set\n"
            f"    Resolver checks, in order: dotfile {dotenv_path()}, "
            "macOS Keychain, Linux secret-tool.\n"
            "    Note: OPENAI_API_KEY env var is intentionally ignored — "
            "use --set to store your key.",
        )
        return False, None
    if looks_like_placeholder_key(key):
        preview = key[:12] + "..." if len(key) > 12 else key
        _fail(
            f"key from source={source!r} looks like a placeholder ({preview!r})",
            "Replace it: python3 "
            f"{SCRIPT_DIR / 'key_resolver.py'} --clear && python3 "
            f"{SCRIPT_DIR / 'key_resolver.py'} --set",
        )
        return False, key
    _ok(f"key present and well-formed ({key[:7]}...{key[-4:]})", f"source={source}")
    return True, key


def check_settings_json() -> bool:
    """Note if ~/.claude/settings.json injects OPENAI_API_KEY.

    The resolver no longer reads OPENAI_API_KEY from the environment, so an
    env-block injection is harmless for key resolution. We still warn when a
    placeholder is present so the user knows it won't do anything useful.
    """
    print(_color("\n[4/5] Claude Code settings.json", BOLD))
    settings = Path.home() / ".claude" / "settings.json"
    if not settings.exists():
        _ok("no ~/.claude/settings.json")
        return True
    try:
        import json  # noqa: PLC0415

        data = json.loads(settings.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _warn(
            f"could not parse {settings}: {exc}",
            "Fix or remove the file.",
        )
        return True
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        _ok("no 'env' block in settings.json")
        return True
    injected = env.get("OPENAI_API_KEY")
    if injected is None or injected == "":
        _ok("settings.json does not set OPENAI_API_KEY")
        return True
    if not isinstance(injected, str):
        _warn(
            f"settings.json sets non-string OPENAI_API_KEY "
            f"(type={type(injected).__name__}) — has no effect on the resolver",
            "The resolver reads the dotfile, not env vars. Remove the line or fix the type.",
        )
        return True
    if looks_like_placeholder_key(injected):
        preview = injected[:12] + "..." if len(injected) > 12 else injected
        _warn(
            f"settings.json sets placeholder OPENAI_API_KEY ({preview!r}) — has no effect on the resolver",
            "The resolver reads the dotfile, not env vars. "
            "Remove the line or run: python3 key_resolver.py --set",
        )
        return True
    _warn(
        "settings.json sets OPENAI_API_KEY — has no effect on the resolver",
        "The resolver reads the dotfile, not env vars. You can remove this line.",
    )
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
        check_network_reachability(),
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

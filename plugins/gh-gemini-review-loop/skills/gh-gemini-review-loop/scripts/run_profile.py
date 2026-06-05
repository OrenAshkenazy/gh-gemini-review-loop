#!/usr/bin/env python3
"""Execute a verification profile's checks and compute the required-gate.

Each check ``command`` is split with ``shlex`` and run WITHOUT a shell, with
``cwd`` set to the profile's ``working_directory`` and a wall-clock timeout.
The gate fails iff any ``required`` check fails or times out; non-required
failures are recorded but non-gating.
"""
from __future__ import annotations

import dataclasses
import json
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


@dataclasses.dataclass
class CheckResult:
    name: str
    command: str
    required: bool
    status: str  # "passed" | "failed" | "timeout"
    returncode: int | None
    duration_s: float


@dataclasses.dataclass
class ProfileRunResult:
    verification: str  # "passed" | "failed"
    checks: list[CheckResult]
    failed_required: list[str]

    def to_details(self) -> dict[str, Any]:
        """JSON-serializable shape for --verification-details."""
        return {
            "verification": self.verification,
            "failed_required": self.failed_required,
            "checks": [dataclasses.asdict(c) for c in self.checks],
        }


def _run_one(check: Any, cwd: Path, timeout: int) -> CheckResult:
    start = time.monotonic()
    # A profile can be hand-edited or corrupted; never trust the shape.
    if not isinstance(check, dict):
        return CheckResult("<invalid>", repr(check), True, "failed", None,
                           time.monotonic() - start)
    name = str(check.get("name", "<unnamed>"))
    command = check.get("command")
    required = bool(check.get("required", True))
    if not isinstance(command, str):
        return CheckResult(name, str(command), required, "failed", None,
                           time.monotonic() - start)
    try:
        argv = shlex.split(command)
    except ValueError:
        # Malformed/unclosed quotes in a hand-edited command.
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start)
    if not argv:
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start)
    try:
        proc = subprocess.run(  # noqa: S603 - command is user-confirmed, no shell
            argv,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(name, command, required, "timeout", None,
                           time.monotonic() - start)
    except (FileNotFoundError, OSError):
        return CheckResult(name, command, required, "failed", None,
                           time.monotonic() - start)
    status = "passed" if proc.returncode == 0 else "failed"
    return CheckResult(name, command, required, status, proc.returncode,
                       time.monotonic() - start)


def run_profile(profile: Any, repo_root: Path | str) -> ProfileRunResult:
    """Run all checks in ``profile`` rooted at ``repo_root``; compute the gate.

    The profile may have been hand-edited in preferences.json, so every field
    is validated/coerced before use rather than trusted.
    """
    root = Path(repo_root)
    if not isinstance(profile, dict):
        profile = {}
    working_directory = profile.get("working_directory", ".")
    if not isinstance(working_directory, str):
        working_directory = "."
    cwd = root / working_directory
    try:
        timeout = int(profile.get("timeout_seconds", 300))
    except (TypeError, ValueError):
        timeout = 300
    if timeout <= 0:
        # subprocess.run treats 0/negative as an immediate/invalid timeout.
        timeout = 300
    checks = profile.get("checks", [])
    if not isinstance(checks, list):
        checks = []
    results = [_run_one(c, cwd, timeout) for c in checks]
    failed_required = [
        c.name for c in results if c.required and c.status != "passed"
    ]
    verification = "failed" if failed_required else "passed"
    return ProfileRunResult(verification, results, failed_required)


def main(argv: list[str]) -> int:
    """CLI: run_profile.py <owner/repo> <repo_root>.

    Loads the saved profile for the repo, runs it, prints to_details() JSON,
    and exits 0 if verification passed / 1 if it failed. A missing, skipped,
    or empty profile prints a 'skipped' result and exits 0 (the loop falls
    back to ad-hoc verification).
    """
    if len(argv) < 3:
        print("usage: run_profile.py <owner/repo> <repo_root>", file=sys.stderr)
        return 2
    repo, repo_root = argv[1], argv[2]
    try:
        from judge import get_profile  # noqa: PLC0415
    except ImportError:
        print(json.dumps({"verification": "skipped",
                          "reason": "judge module unavailable"}))
        return 0
    profile = get_profile(repo)
    if (
        not isinstance(profile, dict)
        or profile.get("source") == "skipped"
        or not isinstance(profile.get("checks"), list)
        or not profile.get("checks")
    ):
        print(json.dumps({"verification": "skipped",
                          "reason": "no runnable profile"}))
        return 0
    result = run_profile(profile, repo_root)
    print(json.dumps(result.to_details(), indent=2))
    return 0 if result.verification == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

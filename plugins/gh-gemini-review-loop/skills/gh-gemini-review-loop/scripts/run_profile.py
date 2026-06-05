#!/usr/bin/env python3
"""Execute a verification profile's checks and compute the required-gate.

Each check ``command`` is split with ``shlex`` and run WITHOUT a shell, with
``cwd`` set to the profile's ``working_directory`` and a wall-clock timeout.
The gate fails iff any ``required`` check fails or times out; non-required
failures are recorded but non-gating.
"""
from __future__ import annotations

import dataclasses
import shlex
import subprocess
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


def _run_one(check: dict[str, Any], cwd: Path, timeout: int) -> CheckResult:
    name = check["name"]
    command = check["command"]
    required = bool(check.get("required", True))
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 - command is user-confirmed, no shell
            shlex.split(command),
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


def run_profile(profile: dict[str, Any], repo_root: Path | str) -> ProfileRunResult:
    """Run all checks in ``profile`` rooted at ``repo_root``; compute the gate."""
    root = Path(repo_root)
    cwd = root / profile.get("working_directory", ".")
    timeout = int(profile.get("timeout_seconds", 300))
    results = [_run_one(c, cwd, timeout) for c in profile.get("checks", [])]
    failed_required = [
        c.name for c in results if c.required and c.status != "passed"
    ]
    verification = "failed" if failed_required else "passed"
    return ProfileRunResult(verification, results, failed_required)

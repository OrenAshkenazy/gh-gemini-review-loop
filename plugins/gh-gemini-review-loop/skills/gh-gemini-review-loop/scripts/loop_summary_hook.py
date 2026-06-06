#!/usr/bin/env python3
"""Stop-hook backstop for Gemini review-loop visibility.

The skill instructs the agent to emit a ``[loop] Summary`` after every cycle and
at loop end. That instruction is not always honoured under context pressure, so
this hook guarantees visibility mechanically: when a turn ends, if a Gemini loop
advanced (a new fetch bumped ``update_seq``) without the agent emitting a
summary (``last_summary_seq`` lags), it emits the authoritative summary now by
shelling out to ``fetch_gemini_threads.py --cycle-summary``.

It is network-gated by local state: outside an active loop it is a fast no-op
that never touches git or GitHub. ``--cycle-summary`` is read-only — it stamps
``last_summary_seq`` so the hook never repeats a summary the agent already
showed, and it never appends a metrics record or clears the accumulator (the
agent still owns the single terminal ``--record-run``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fetch_gemini_threads import (  # noqa: E402
    any_active_run,
    find_active_run,
    resolve_current_repo,
    summary_is_stale,
)


def select_backstop_pr(repo_full: str) -> int | None:
    """Return the PR number that needs a backstop summary, or None.

    A PR qualifies when it has an active run for ``repo_full`` that advanced
    since the last emitted summary. Pure decision: reads local state only.
    """
    active = find_active_run(repo_full)
    if active is None:
        return None
    number, run = active
    return number if summary_is_stale(run) else None


def build_hook_output(summary: str) -> str | None:
    """Wrap the summary as a Stop-hook JSON payload, or None if there's nothing.

    A Stop hook's bare stdout is written to the debug log only — Claude Code
    does not show it in the chat. The `systemMessage` JSON field is the
    documented way to surface a message to the user, so the summary rides there.
    """
    text = summary.strip()
    if not text:
        return None
    return json.dumps({"systemMessage": text})


def main() -> int:
    # Drain the hook payload on stdin; a Stop event carries nothing we need.
    try:
        sys.stdin.read()
    except (OSError, ValueError):  # never let hook I/O break the turn
        pass

    # Fast path: if no loop is active anywhere, skip the git/repo resolution
    # entirely so the hook is free on every unrelated Stop.
    try:
        if not any_active_run():
            return 0
        repo = resolve_current_repo()
    except (RuntimeError, OSError):  # not a gh repo, no remote, etc.
        return 0

    number = select_backstop_pr(repo)
    if number is None:
        return 0

    pr_url = f"https://github.com/{repo}/pull/{number}"
    cmd = [
        sys.executable,
        os.path.join(HERE, "fetch_gemini_threads.py"),
        "--pr", pr_url,
        "--cycle-summary",
    ]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
    except (OSError, ValueError, subprocess.SubprocessError):  # backstop must never block Stop
        return 0
    payload = build_hook_output(proc.stdout or "")
    if payload:
        # Emitted as JSON so Claude Code surfaces the summary to the user via
        # systemMessage; bare stdout from a Stop hook is debug-log-only.
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

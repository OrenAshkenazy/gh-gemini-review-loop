#!/usr/bin/env python3
"""PreToolUse gate: enforce --cycle-summary before git push during the loop.

The skill requires --cycle-summary at the end of every non-terminal cycle.
This gate makes that requirement mechanical: if a Gemini review loop is
active for the current repo and the run has a stale summary
(update_seq > last_summary_seq), git push is blocked until the agent
runs --cycle-summary.

Why block at git push rather than at re-review comment posting?
git push is the natural commit point that separates cycles. Blocking here
gives the agent a clear, one-step fix: run --cycle-summary, then push.

Fails OPEN: any error (not a repo, bad JSON, import failure) allows the push.
A correctness aid must never wedge normal git operations.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fetch_gemini_threads import (  # noqa: E402
    any_active_run,
    find_active_run,
    resolve_current_repo,
    summary_is_stale,
)

_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")

BLOCK_TEMPLATE = (
    "[gh-gemini-review-loop] A Gemini review loop is active for {repo} and the "
    "per-cycle summary has not been emitted yet (update_seq={update_seq} > "
    "last_summary_seq={last_summary_seq}). Run --cycle-summary BEFORE pushing:\n\n"
    "  python3 \"$CLAUDE_PLUGIN_ROOT/skills/gh-gemini-review-loop/scripts/"
    "fetch_gemini_threads.py\" \\\n"
    "      --pr {pr_url} \\\n"
    "      --cycle-summary \\\n"
    "      --fixed-count <n> \\\n"
    "      --verification <passed|failed|skipped>\n\n"
    "Print the full output to the user, then push."
)


def stale_summary_for_push(repo: str) -> dict[str, Any] | None:
    """Return block info dict if a git push should be blocked, else None.

    A push is blocked when the active loop for ``repo`` has advanced
    (``update_seq > 0``) without a summary being emitted since
    (``last_summary_seq`` lags). Returns a dict with ``pr_number``,
    ``update_seq``, and ``last_summary_seq`` for building the error message.
    """
    result = find_active_run(repo)
    if result is None:
        return None
    pr_number, run = result
    if not summary_is_stale(run):
        return None
    return {
        "pr_number": pr_number,
        "update_seq": run.get("update_seq", "?"),
        "last_summary_seq": run.get("last_summary_seq", 0),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
    except (json.JSONDecodeError, ValueError, OSError):
        return 0

    # Only intercept Bash tool calls that contain git push.
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not _GIT_PUSH_RE.search(command):
        return 0

    try:
        if not any_active_run():
            return 0
        repo = resolve_current_repo()
        info = stale_summary_for_push(repo)
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
        return 0  # not a gh repo, no remote, or corrupt state -> allow

    if info is None:
        return 0

    pr_url = f"https://github.com/{repo}/pull/{info['pr_number']}"
    print(
        BLOCK_TEMPLATE.format(
            repo=repo,
            update_seq=info["update_seq"],
            last_summary_seq=info["last_summary_seq"],
            pr_url=pr_url,
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

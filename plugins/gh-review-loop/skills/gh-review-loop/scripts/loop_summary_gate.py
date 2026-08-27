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

The error message is intentionally just the fix instruction (#86): the
receipt content itself is delivered once, via the sticky PR comment that
--cycle-summary writes — embedding a state snapshot here was a third copy
of the same data.

Fails OPEN: any error (not a repo, bad JSON, import failure) allows the push.
A correctness aid must never wedge normal git operations.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from loop_state import (  # noqa: E402 — slim, stdlib-only (see #83)
    any_active_run,
    find_active_run,
    reap_stale_sentinel,
    resolve_current_repo,
    summary_is_stale,
)
from hook_runtime import (  # noqa: E402
    load_hook_payload,
    python_script_command,
    tool_command,
    tool_name,
)

_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b")

BLOCK_TEMPLATE = (
    "[gh-review-loop] A review loop is active for {repo} and the "
    "per-cycle summary has not been emitted yet (update_seq={update_seq} > "
    "last_summary_seq={last_summary_seq}).\n\n"
    "Run --cycle-summary (it delivers the receipt to the PR comment; relay "
    "the printed [loop] pointer line), then push:\n\n"
    "  {fetch_cmd} \\\n"
    "      --pr {pr_url} \\\n"
    "      --cycle-summary \\\n"
    "      --fixed-count <n> \\\n"
    "      --verification <passed|failed|skipped>"
)


def stale_summary_for_push(repo: str) -> dict[str, Any] | None:
    """Return block info dict if a git push should be blocked, else None.

    A push is blocked when the active loop for ``repo`` has advanced
    (``update_seq > 0``) without a summary being emitted since
    (``last_summary_seq`` lags). Returns a dict with the run fields the
    one-line fix instruction needs.
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
        raw_payload = sys.stdin.read() if sys.stdin is not None else ""
        payload = load_hook_payload(raw_payload)
    except (OSError, ValueError):
        return 0

    # Only intercept Bash tool calls that contain git push.
    if tool_name(payload) != "Bash":
        return 0
    command = tool_command(payload)
    if not _GIT_PUSH_RE.search(command):
        return 0

    try:
        # The shell guard only checks the sentinel exists (#103); a stale one
        # means a crashed/abandoned loop — reap it and stand down.
        if reap_stale_sentinel():
            return 0
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
            fetch_cmd=python_script_command("fetch_gemini_threads.py"),
        ),
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())

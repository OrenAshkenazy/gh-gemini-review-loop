#!/usr/bin/env python3
"""PreToolUse gate: enforce verification-profile-before-fixes during the loop.

The skill requires that, on the first run for a repo, the verification profile
is set up (detect -> preset menu -> save) *before* any fix is applied — so the
verification strategy is fixed before edits. That ordering was previously prose
the agent could skip. This gate makes it mechanical.

It blocks Edit/Write/MultiEdit only in the precise violation window: a Gemini
loop is active for the current repo AND no profile decision has been saved yet.
Saving any profile — including a deliberate Skip — satisfies the gate, so the
agent unblocks itself simply by making the decision the skill already asks for.

Fails OPEN: any error (not a repo, judge unimportable, malformed payload) allows
the edit. A visibility/ordering aid must never wedge unrelated editing.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from fetch_gemini_threads import (  # noqa: E402
    any_active_run,
    find_active_run,
    resolve_current_repo,
)


def profile_required_for_repo(repo_full: str) -> bool:
    """True when an edit should be blocked pending a profile decision.

    Requires an active loop for the repo and no saved profile. Fails open
    (returns False) if the judge module can't be imported.
    """
    if find_active_run(repo_full) is None:
        return False
    try:
        import judge  # noqa: PLC0415
    except ImportError:
        return False
    return judge.get_profile(repo_full) is None


BLOCK_MESSAGE = (
    "[gh-gemini-review-loop] A Gemini review loop is active for {repo} and no "
    "verification profile is saved yet. Set up the profile BEFORE editing: run "
    "detect_profile.py, present the preset menu, and persist the choice with "
    "judge.save_profile(...). Choosing 'Skip' also satisfies this. See the "
    "skill's 'Verification Profile' section. This keeps the verify strategy "
    "fixed before any fix is applied."
)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            return 0
    except (json.JSONDecodeError, ValueError, OSError):
        return 0  # malformed/unreadable payload -> fail open

    tool = payload.get("tool_name", "")
    if tool not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return 0

    try:
        if not any_active_run():
            return 0
        repo = resolve_current_repo()
        if not profile_required_for_repo(repo):
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError):
        # not a gh repo, no remote, or corrupt/hand-edited state.json -> allow
        return 0

    # Exit code 2 blocks the tool call; stderr is surfaced to the agent.
    print(BLOCK_MESSAGE.format(repo=repo), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

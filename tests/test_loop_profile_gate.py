"""Unit tests for the profile-before-fixes PreToolUse gate decision.

During an active Gemini loop the skill must fix the verification strategy
(detect -> preset menu -> save) before editing any code. This gate enforces
that ordering mechanically: it blocks Edit/Write only in the precise window
where a loop is active for the repo and no profile decision has been saved yet.
Choosing any profile (including Skip) satisfies it.
"""

import io
import json
import os
import time

import judge
from fetch_gemini_threads import save_sticky_state
from loop_profile_gate import main as profile_gate_main
from loop_profile_gate import profile_required_for_repo
from loop_state import SENTINEL_TTL_SECONDS, sentinel_path, touch_sentinel


class _UnreadableStdin:
    def read(self):
        raise ValueError("invalid input")


def _active(repo, number=1):
    save_sticky_state({
        f"{repo}#{number}": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}},
    })


class TestProfileRequiredForRepo:
    def test_main_fails_open_when_stdin_read_raises_value_error(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", _UnreadableStdin())
        assert profile_gate_main() == 0

    def test_main_fails_open_when_stdin_is_none(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", None)
        assert profile_gate_main() == 0

    def test_false_when_no_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        # No loop in flight: editing is unrelated to a review loop, never block.
        assert profile_required_for_repo("o/r") is False

    def test_true_when_active_loop_and_no_profile(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active("o/r")
        assert profile_required_for_repo("o/r") is True

    def test_false_when_active_loop_and_profile_confirmed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active("o/r")
        judge.save_profile(
            "o/r", source="confirmed",
            checks=[{"name": "tests", "command": "pytest", "required": True}],
        )
        assert profile_required_for_repo("o/r") is False

    def test_false_when_active_loop_and_profile_skipped(self, tmp_path, monkeypatch):
        # A deliberate Skip is still a decision — it must satisfy the gate.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active("o/r")
        judge.save_profile("o/r", source="skipped", checks=[])
        assert profile_required_for_repo("o/r") is False

    def test_false_for_other_repo_with_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active("other/repo")
        assert profile_required_for_repo("o/r") is False


class TestStaleSentinelReap:
    def test_stale_sentinel_allows_edit_and_is_reaped(self, tmp_path, monkeypatch):
        # #103: the shell guard is existence-only, so the gate must disarm a
        # crashed loop itself — and stand down before any repo resolution
        # (resolve_current_repo would trip the hermetic-gh guard here).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active("o/r")
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"tool_name": "Edit"}))
        )

        assert profile_gate_main() == 0
        assert not sentinel_path().exists()

    def test_stale_sentinel_reaped_on_non_matching_tool(self, tmp_path, monkeypatch):
        # Mirror of the summary gate's reap-before-filter ordering (PR #110):
        # even a payload that doesn't match the edit tools must disarm a
        # stale loop.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        monkeypatch.setattr(
            "sys.stdin", io.StringIO(json.dumps({"tool_name": "SomeOtherTool"}))
        )

        assert profile_gate_main() == 0
        assert not sentinel_path().exists()

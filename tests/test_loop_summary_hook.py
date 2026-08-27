"""Unit tests for the Stop-hook backstop decision logic.

The hook itself shells out to fetch_gemini_threads.py; these tests cover the
pure decision — given local state and a repo, which PR (if any) needs a
backstop summary — without any network or subprocess.
"""

import io
import json
import os
import time

from fetch_gemini_threads import save_sticky_state
from loop_state import SENTINEL_TTL_SECONDS, sentinel_path, touch_sentinel
from loop_summary_hook import build_hook_output, main as summary_hook_main
from loop_summary_hook import select_backstop_pr


class TestStaleSentinelReap:
    def test_stale_sentinel_skips_backstop_and_is_reaped(
        self, tmp_path, monkeypatch
    ):
        # #103: the shell guard is existence-only, so the Stop hook must
        # disarm a crashed loop itself — and stand down before any repo
        # resolution (resolve_current_repo would trip the hermetic-gh guard).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#7": {"run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2, "last_summary_seq": 1,
            }},
        })
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

        assert summary_hook_main() == 0
        assert not sentinel_path().exists()


class TestSelectBackstopPr:
    def test_none_when_no_active_run(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert select_backstop_pr("o/r") is None

    def test_none_when_active_but_already_summarized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#7": {"run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2, "last_summary_seq": 2,
            }},
        })
        assert select_backstop_pr("o/r") is None

    def test_returns_pr_when_advanced_without_summary(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#7": {"run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2, "last_summary_seq": 1,
            }},
        })
        assert select_backstop_pr("o/r") == 7

    def test_ignores_active_loop_in_other_repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "other/repo#1": {"run": {
                "started_at": "2026-06-06T10:00:00Z", "update_seq": 2,
            }},
        })
        assert select_backstop_pr("o/r") is None


class TestBuildHookOutput:
    """A Stop hook's bare stdout is NOT shown to the user (debug log only).
    The summary must be returned as the `systemMessage` JSON field, which
    Claude Code surfaces in the chat."""

    def test_none_for_empty_summary(self):
        assert build_hook_output("") is None
        assert build_hook_output("   \n  ") is None

    def test_wraps_summary_in_system_message_json(self):
        summary = "[loop] Summary\nFindings fetched: 6\nFixed: 6"
        out = build_hook_output(summary)
        parsed = json.loads(out)
        assert parsed["systemMessage"] == summary

    def test_output_is_valid_json_single_line(self):
        # Claude Code parses stdout as JSON on exit 0; must be one parseable doc.
        out = build_hook_output("[loop] Summary\nx: 1")
        assert "\n" not in out.rstrip("\n")  # newlines only inside the JSON string
        json.loads(out)  # does not raise

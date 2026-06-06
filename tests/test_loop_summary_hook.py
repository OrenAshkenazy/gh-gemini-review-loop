"""Unit tests for the Stop-hook backstop decision logic.

The hook itself shells out to fetch_gemini_threads.py; these tests cover the
pure decision — given local state and a repo, which PR (if any) needs a
backstop summary — without any network or subprocess.
"""

from fetch_gemini_threads import save_sticky_state
from loop_summary_hook import select_backstop_pr


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

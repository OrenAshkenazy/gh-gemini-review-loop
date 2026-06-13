"""Unit tests for the summary-before-push PreToolUse gate decision.

During an active Gemini loop the agent must emit --cycle-summary before
pushing new commits. This gate enforces that mechanically: git push is
blocked while a loop is active for the repo and summary_is_stale() is True
(update_seq > last_summary_seq). Running --cycle-summary stamps
last_summary_seq and clears the gate.
"""

from fetch_gemini_threads import save_sticky_state
from loop_summary_gate import format_run_snapshot
from loop_summary_gate import main as summary_gate_main
from loop_summary_gate import stale_summary_for_push


class _UnreadableStdin:
    def read(self):
        raise ValueError("invalid input")


def _active_stale(
    repo: str,
    number: int = 1,
    *,
    update_seq: int = 2,
    last_summary_seq: int = 0,
) -> None:
    """Write an active run with update_seq > last_summary_seq (stale summary)."""
    run: dict = {
        "started_at": "2026-06-06T10:00:00Z",
        "update_seq": update_seq,
    }
    if last_summary_seq:
        run["last_summary_seq"] = last_summary_seq
    save_sticky_state({f"{repo}#{number}": {"run": run}})


def _active_current(repo: str, number: int = 1) -> None:
    """Write an active run where summary is already current."""
    save_sticky_state({
        f"{repo}#{number}": {
            "run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2,
                "last_summary_seq": 2,
            },
        },
    })


class TestStaleSummaryForPush:
    def test_main_fails_open_when_stdin_read_raises_value_error(self, monkeypatch):
        monkeypatch.setattr("sys.stdin", _UnreadableStdin())
        assert summary_gate_main() == 0

    def test_none_when_no_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert stale_summary_for_push("o/r") is None

    def test_none_when_update_seq_zero(self, tmp_path, monkeypatch):
        # A run with started_at but no update_seq has no fetched data yet —
        # it's legacy/pre-feature state, not a stale mid-loop summary.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"started_at": "t"}}})
        assert stale_summary_for_push("o/r") is None

    def test_none_when_summary_current(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_current("o/r")
        assert stale_summary_for_push("o/r") is None

    def test_none_for_other_repo_active_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("other/repo")
        assert stale_summary_for_push("o/r") is None

    def test_returns_info_when_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=7, update_seq=3, last_summary_seq=1)
        info = stale_summary_for_push("o/r")
        assert info is not None
        assert info["pr_number"] == 7
        assert info["update_seq"] == 3
        assert info["last_summary_seq"] == 1

    def test_returns_info_when_never_summarized(self, tmp_path, monkeypatch):
        # last_summary_seq absent (first cycle, never called --cycle-summary).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=42, update_seq=1, last_summary_seq=0)
        info = stale_summary_for_push("o/r")
        assert info is not None
        assert info["pr_number"] == 42
        assert info["last_summary_seq"] == 0

    def test_clears_after_summary_stamped(self, tmp_path, monkeypatch):
        # After the agent calls --cycle-summary, last_summary_seq catches up
        # and the gate must clear.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        _active_stale("o/r", number=3, update_seq=2)
        # Simulate stamp_summary_emitted patching last_summary_seq to match.
        save_sticky_state({
            "o/r#3": {"run": {
                "started_at": "2026-06-06T10:00:00Z",
                "update_seq": 2,
                "last_summary_seq": 2,
            }},
        })
        assert stale_summary_for_push("o/r") is None

    def test_info_includes_run_fields(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#5": {"run": {
                "started_at": "2026-06-07T08:00:00Z",
                "update_seq": 3,
                "finding_ids": ["id1", "id2"],
                "finding_paths": ["src/foo.py", "src/bar.py"],
            }},
        })
        info = stale_summary_for_push("o/r")
        assert info is not None
        assert info["finding_ids"] == ["id1", "id2"]
        assert info["finding_paths"] == ["src/foo.py", "src/bar.py"]
        assert info["started_at"] == "2026-06-07T08:00:00Z"


class TestFormatRunSnapshot:
    def test_includes_key_fields(self):
        info = {
            "update_seq": 2,
            "last_summary_seq": 0,
            "finding_ids": ["a", "b", "c"],
            "finding_paths": ["src/x.py"],
            "started_at": "2026-06-07T08:00:00Z",
        }
        out = format_run_snapshot(info)
        assert "2026-06-07" in out
        assert "3" in out   # thread count
        assert "src/x.py" in out
        assert "snapshot" in out.lower()

    def test_truncates_long_path_list(self):
        info = {
            "update_seq": 1,
            "last_summary_seq": 0,
            "finding_ids": [],
            "finding_paths": ["a.py", "b.py", "c.py", "d.py"],
            "started_at": "2026-06-07T08:00:00Z",
        }
        out = format_run_snapshot(info)
        assert "…" in out  # truncation marker for > 3 paths

    def test_handles_empty_run(self):
        info = {
            "update_seq": "?",
            "last_summary_seq": 0,
            "finding_ids": [],
            "finding_paths": [],
            "started_at": "",
        }
        out = format_run_snapshot(info)
        assert "none recorded" in out
        assert "?" in out  # started_at fallback

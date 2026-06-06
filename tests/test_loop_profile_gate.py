"""Unit tests for the profile-before-fixes PreToolUse gate decision.

During an active Gemini loop the skill must fix the verification strategy
(detect -> preset menu -> save) before editing any code. This gate enforces
that ordering mechanically: it blocks Edit/Write only in the precise window
where a loop is active for the repo and no profile decision has been saved yet.
Choosing any profile (including Skip) satisfies it.
"""

import judge
from fetch_gemini_threads import save_sticky_state
from loop_profile_gate import profile_required_for_repo


def _active(repo, number=1):
    save_sticky_state({
        f"{repo}#{number}": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}},
    })


class TestProfileRequiredForRepo:
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

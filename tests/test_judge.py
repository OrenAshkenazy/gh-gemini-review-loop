"""Unit tests for the end-user judge (plugins/.../scripts/judge.py).

All tests are hermetic — they never call OpenAI. The judge's network call
is injected via the ``call_fn`` constructor arg.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "gh-gemini-review-loop" / "skills" / "gh-gemini-review-loop" / "scripts"
)
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from judge import (  # noqa: E402
    DEFAULT_MODEL,
    PREFS_SCHEMA_VERSION,
    VALID_VERDICTS,
    JudgeClient,
    JudgeError,
    build_user_prompt,
    load_preferences,
    mark_tip_shown,
    prefs_path,
    save_preferences,
    should_judge_run,
)


def fake_ok(verdict="valid_actionable", **overrides):
    payload = {
        "verdict": verdict,
        "confidence": overrides.get("confidence", 0.9),
        "severity_override": overrides.get("severity_override", "medium"),
        "recommended_action": overrides.get("recommended_action", "fix"),
        "reason": overrides.get("reason", "looks real"),
    }
    return lambda _msgs: {"content": json.dumps(payload)}


# ---------------------------------------------------------------------------
# Preferences file (script as source of truth; agent only writes once)
# ---------------------------------------------------------------------------


class TestPreferences:
    def test_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert prefs_path() == tmp_path / "preferences.json"

    def test_missing_file_returns_default_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        prefs = load_preferences()
        assert prefs["judge_mode"] == "off"
        assert prefs["judge_model"] == DEFAULT_MODEL
        assert prefs["schema_version"] == PREFS_SCHEMA_VERSION

    def test_corrupt_file_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text("{not json")
        prefs = load_preferences()
        assert prefs["judge_mode"] == "off"

    def test_unknown_mode_in_file_falls_back_to_off(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps({"schema_version": 1, "judge_mode": "wat", "judge_model": "x"})
        )
        prefs = load_preferences()
        assert prefs["judge_mode"] == "off"

    def test_non_dict_file_falls_back_to_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(json.dumps(["not", "a", "dict"]))
        assert load_preferences()["judge_mode"] == "off"

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        saved = save_preferences("on_cycle", judge_model="gpt-4o")
        assert saved["judge_mode"] == "on_cycle"
        assert saved["judge_model"] == "gpt-4o"
        assert saved["judge_tip_shown"] is False
        loaded = load_preferences()
        assert loaded["judge_mode"] == "on_cycle"
        assert loaded["judge_model"] == "gpt-4o"
        assert loaded["set_at"]  # non-empty timestamp
        assert loaded["judge_tip_shown"] is False

    def test_save_preserves_tip_shown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_preferences("off")
        mark_tip_shown()
        # Saving a new mode must not clobber judge_tip_shown=True.
        save_preferences("on_complete")
        assert load_preferences()["judge_tip_shown"] is True

    def test_mark_tip_shown(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_preferences("off")
        assert load_preferences()["judge_tip_shown"] is False
        mark_tip_shown()
        assert load_preferences()["judge_tip_shown"] is True
        # Calling again is idempotent.
        mark_tip_shown()
        assert load_preferences()["judge_tip_shown"] is True

    def test_default_prefs_tip_shown_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        prefs = load_preferences()  # no file → defaults
        assert prefs["judge_tip_shown"] is False

    def test_save_rejects_invalid_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        with pytest.raises(ValueError):
            save_preferences("maybe")

    def test_save_creates_parent_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "deep" / "nested"
        monkeypatch.setenv("GGRL_STATE_DIR", str(nested))
        save_preferences("on_complete")
        assert (nested / "preferences.json").exists()

    def test_unknown_schema_version_keeps_valid_mode(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps(
                {
                    "schema_version": 999,
                    "judge_mode": "on_cycle",
                    "judge_model": "gpt-x",
                    "set_at": "x",
                }
            )
        )
        prefs = load_preferences()
        # Forward-compat: unknown schema_version doesn't drop a valid mode.
        assert prefs["judge_mode"] == "on_cycle"


# ---------------------------------------------------------------------------
# should_judge_run — dispatch logic (the single source of truth)
# ---------------------------------------------------------------------------


class TestShouldJudgeRun:
    @pytest.mark.parametrize("phase", ["cycle", "complete", None])
    def test_off_never_runs(self, phase):
        assert should_judge_run(mode="off", phase=phase) is False

    @pytest.mark.parametrize("phase", ["cycle", "complete", None])
    def test_once_always_runs(self, phase):
        assert should_judge_run(mode="once", phase=phase) is True

    def test_on_cycle_only_runs_on_cycle_phase(self):
        assert should_judge_run(mode="on_cycle", phase="cycle") is True
        assert should_judge_run(mode="on_cycle", phase="complete") is False
        assert should_judge_run(mode="on_cycle", phase=None) is False

    def test_on_complete_only_runs_on_complete_phase(self):
        assert should_judge_run(mode="on_complete", phase="complete") is True
        assert should_judge_run(mode="on_complete", phase="cycle") is False
        assert should_judge_run(mode="on_complete", phase=None) is False

    def test_unknown_mode_never_runs(self):
        # Defense in depth: shouldn't happen (load_preferences filters), but
        # if it did, the safe default is to not run.
        assert should_judge_run(mode="bogus", phase="cycle") is False


# ---------------------------------------------------------------------------
# JudgeClient — graceful skip + parse paths
# ---------------------------------------------------------------------------


class TestJudgeClientReadiness:
    def test_skips_when_no_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = JudgeClient(api_key=None)
        ready, reason = client.is_ready()
        assert ready is False
        assert "OPENAI_API_KEY" in reason

    def test_judge_returns_skipped_when_not_ready(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = JudgeClient(api_key=None)
        result = client.judge({"body": "x", "diff_hunk": ""})
        assert result.status == "skipped"
        assert result.skip_reason is not None
        # Skipped results have stable defaults so downstream code doesn't NPE
        assert result.verdict in VALID_VERDICTS
        assert 0.0 <= result.confidence <= 1.0

    def test_ready_with_call_fn(self):
        client = JudgeClient(call_fn=fake_ok())
        ready, reason = client.is_ready()
        assert ready is True
        assert reason is None


class TestJudgeClientParse:
    def test_happy_path(self):
        client = JudgeClient(call_fn=fake_ok("valid_actionable"))
        r = client.judge({"body": "x"})
        assert r.status == "ok"
        assert r.verdict == "valid_actionable"
        assert r.confidence == 0.9
        assert r.severity_override == "medium"
        assert r.recommended_action == "fix"

    def test_invalid_verdict_raises(self):
        client = JudgeClient(
            call_fn=lambda _m: {
                "content": json.dumps(
                    {
                        "verdict": "maybe",
                        "confidence": 0.5,
                        "severity_override": "medium",
                        "recommended_action": "fix",
                        "reason": "x",
                    }
                )
            }
        )
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_invalid_json_raises(self):
        client = JudgeClient(call_fn=lambda _m: {"content": "not json"})
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_non_dict_payload_raises(self):
        client = JudgeClient(call_fn=lambda _m: {"content": json.dumps(["x"])})
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_invalid_severity_override_coerces_to_none(self):
        client = JudgeClient(
            call_fn=lambda _m: {
                "content": json.dumps(
                    {
                        "verdict": "valid_actionable",
                        "confidence": 0.7,
                        "severity_override": "wat",
                        "recommended_action": "fix",
                        "reason": "x",
                    }
                )
            }
        )
        r = client.judge({"body": "x"})
        assert r.severity_override == "none"

    def test_invalid_recommended_action_coerces_to_ignore(self):
        client = JudgeClient(
            call_fn=lambda _m: {
                "content": json.dumps(
                    {
                        "verdict": "valid_actionable",
                        "confidence": 0.7,
                        "severity_override": "high",
                        "recommended_action": "destroy",
                        "reason": "x",
                    }
                )
            }
        )
        r = client.judge({"body": "x"})
        assert r.recommended_action == "ignore"

    def test_confidence_clamping(self):
        client = JudgeClient(
            call_fn=lambda _m: {
                "content": json.dumps(
                    {
                        "verdict": "valid_actionable",
                        "confidence": 5.5,
                        "severity_override": "medium",
                        "recommended_action": "fix",
                        "reason": "x",
                    }
                )
            }
        )
        r = client.judge({"body": "x"})
        assert r.confidence == 1.0

    def test_falsy_reason_preserved_via_explicit_none_check(self):
        # 0 should survive as "0", not get collapsed to ""
        client = JudgeClient(
            call_fn=lambda _m: {
                "content": json.dumps(
                    {
                        "verdict": "valid_actionable",
                        "confidence": 0.5,
                        "severity_override": "medium",
                        "recommended_action": "fix",
                        "reason": 0,
                    }
                )
            }
        )
        r = client.judge({"body": "x"})
        assert r.reason == "0"


# ---------------------------------------------------------------------------
# build_user_prompt
# ---------------------------------------------------------------------------


class TestBuildUserPrompt:
    def test_omits_diff_block_when_empty(self):
        out = build_user_prompt({"path": "x.py", "line": 1, "body": "hi"})
        assert "diff hunk" not in out.lower()

    def test_includes_diff_block_when_present(self):
        out = build_user_prompt(
            {"path": "x.py", "line": 1, "body": "hi", "diff_hunk": "+foo"}
        )
        assert "+foo" in out

    def test_handles_missing_fields(self):
        out = build_user_prompt({})
        assert "(unknown path):?" in out


# ---------------------------------------------------------------------------
# Judge invariant: JudgeClient has no mutation methods
# ---------------------------------------------------------------------------


class TestJudgeInvariant:
    def test_no_mutation_methods(self):
        """The judge must be read-only. It cannot resolve, comment, or push."""
        client = JudgeClient(call_fn=fake_ok())
        forbidden = ["resolve", "comment", "push", "post", "mutate", "write"]
        for name in dir(client):
            for f in forbidden:
                # Allow private internals (_openai_call writes the API request,
                # but does no GitHub mutation).
                if name.startswith("_") or name in ("request_timeout",):
                    continue
                assert f not in name.lower(), (
                    f"JudgeClient.{name} looks like a mutation; the judge must be read-only."
                )

    def test_judge_module_does_not_import_gh_or_graphql(self):
        """The judge module must NOT import anything that could mutate GitHub state."""
        judge_module_path = PLUGIN_SCRIPTS / "judge.py"
        # Explicit utf-8: judge.py has em-dashes / arrows / non-ASCII
        # characters in docstrings and would UnicodeDecodeError on Windows
        # CP1252 or older CI locales without this.
        src = judge_module_path.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "gh api", "resolveReviewThread", "addPullRequestReview"):
            assert forbidden not in src, (
                f"judge.py imports/uses {forbidden!r} — judge must be read-only."
            )

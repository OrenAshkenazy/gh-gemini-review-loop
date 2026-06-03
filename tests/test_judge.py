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
    DEFAULT_MAX_REREVIEW_REQUESTS,
    DEFAULT_MODEL,
    PREFS_SCHEMA_VERSION,
    VALID_VERDICTS,
    JudgeClient,
    JudgeError,
    build_user_prompt,
    load_preferences,
    looks_like_placeholder_key,
    mark_tip_shown,
    prefs_path,
    save_preferences,
    should_judge_run,
)


@pytest.fixture(autouse=True)
def _isolate_key_resolver(monkeypatch, tmp_path):
    """Stub the OS keystore readers so JudgeClient(api_key=None) tests are
    host-independent. Otherwise a developer Mac with a real Keychain entry
    silently fills in the key and flips the readiness assertions.
    """
    import key_resolver  # noqa: PLC0415

    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(key_resolver._READERS, "macos_keychain", lambda: None)
    monkeypatch.setitem(key_resolver._READERS, "linux_secret_service", lambda: None)


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
        assert prefs["max_rereview_requests"] == DEFAULT_MAX_REREVIEW_REQUESTS
        assert prefs["schema_version"] == PREFS_SCHEMA_VERSION

    def test_missing_file_is_written_on_first_load(self, tmp_path, monkeypatch):
        """preferences.json must be created on first load so users can discover it."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not (tmp_path / "preferences.json").exists()

        load_preferences()

        assert (tmp_path / "preferences.json").exists()
        saved = json.loads((tmp_path / "preferences.json").read_text())
        assert saved["judge_mode"] == "off"
        assert saved["max_rereview_requests"] == DEFAULT_MAX_REREVIEW_REQUESTS

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
        assert saved["max_rereview_requests"] == DEFAULT_MAX_REREVIEW_REQUESTS
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

    def test_loads_saved_max_rereview_requests(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps({"schema_version": 1, "max_rereview_requests": 5})
        )
        assert load_preferences()["max_rereview_requests"] == 5

    @pytest.mark.parametrize("value", [-1, True, "five", None])
    def test_invalid_max_rereview_requests_falls_back(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps({"schema_version": 1, "max_rereview_requests": value})
        )
        assert load_preferences()["max_rereview_requests"] == DEFAULT_MAX_REREVIEW_REQUESTS

    def test_string_max_rereview_requests_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps({"schema_version": 1, "max_rereview_requests": " 6 "})
        )
        assert load_preferences()["max_rereview_requests"] == 6

    def test_save_preserves_max_rereview_requests(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            json.dumps({"schema_version": 1, "max_rereview_requests": 4})
        )
        save_preferences("on_complete")
        assert load_preferences()["max_rereview_requests"] == 4


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

    def test_skip_reason_mentions_doctor_when_key_missing(self, monkeypatch):
        # Users hit this most often; the error must surface the doctor so they
        # don't have to guess what to install or where the key should live.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        client = JudgeClient(api_key=None)
        _, reason = client.is_ready()
        assert "judge_doctor" in reason

    @pytest.mark.parametrize(
        "placeholder",
        [
            "REPLACE_WITH_YOUR_KEY",
            "sk-YOUR_KEY_HERE",
            "PASTE_KEY_HERE",
            "<your-openai-api-key>",
            "TODO",
            "xxx",
            "short",  # under 40 chars
            "not-starting-with-sk-but-long-enough-to-pass-length-check-aaaa",
        ],
    )
    def test_placeholder_keys_blocked(self, placeholder):
        # Real keys never trip this; placeholders do. Catches the
        # settings.json injection failure mode before the SDK ever runs.
        client = JudgeClient(api_key=placeholder)
        ready, reason = client.is_ready()
        assert ready is False
        assert "placeholder" in reason.lower()

    def test_non_string_api_key_blocked_with_typed_reason(self):
        # settings.json env-injection can pass a bool/int through unchanged.
        # The readiness check must reject it with a clear "not a string"
        # reason instead of letting a downstream string op AttributeError.
        client = JudgeClient(api_key=True)
        ready, reason = client.is_ready()
        assert ready is False
        assert "not a string" in reason
        assert "bool" in reason

    def test_real_looking_key_not_blocked_by_placeholder_check(self):
        # A plausible-shape key (sk- + 48 chars) must NOT match the placeholder
        # heuristic — otherwise valid keys would be falsely rejected.
        fake_real = "sk-" + "a" * 48
        client = JudgeClient(api_key=fake_real)
        # Won't fully succeed (no SDK / network), but reason must not be the
        # placeholder one — that's the regression we're guarding against.
        _, reason = client.is_ready()
        if reason is not None:
            assert "placeholder" not in reason.lower()


class TestLooksLikePlaceholderKey:
    @pytest.mark.parametrize(
        "key,expected",
        [
            (None, False),  # missing key is "not a placeholder" — caller handles
            ("", False),
            (True, False),  # non-string (bool) — settings.json could inject this
            (12345, False),  # non-string (int) — must not crash key.upper()
            ([], False),  # other non-string — defensive
            ("REPLACE_WITH_YOUR_KEY", True),
            ("replace_with_your_key", True),  # case-insensitive
            ("sk-YOUR_KEY_HERE_PADDED_TO_BE_LONG_AAAAAAAA", True),
            ("short", True),  # under min length
            ("not-sk-prefix-but-long-enough-to-pass-length-aaaa", True),
            ("sk-" + "a" * 48, False),  # plausible real key shape
            ("sk-svcacct-" + "b" * 40, False),  # service-account key shape
        ],
    )
    def test_detects(self, key, expected):
        assert looks_like_placeholder_key(key) is expected

    def test_openai_base_url_bypasses_shape_checks(self, monkeypatch):
        # Users pointing at Ollama / LiteLLM / LM Studio / enterprise gateways
        # legitimately use short or non-sk- keys. OPENAI_BASE_URL is the
        # signal that the SDK is talking to a non-OpenAI endpoint, so shape
        # checks must not fire there. The explicit placeholder-marker check
        # still does — a literal REPLACE_WITH_YOUR_KEY is wrong everywhere.
        monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:11434/v1")
        assert looks_like_placeholder_key("ollama") is False
        assert looks_like_placeholder_key("not-an-sk-key") is False
        assert looks_like_placeholder_key("any-short") is False
        # But explicit placeholders still rejected
        assert looks_like_placeholder_key("REPLACE_WITH_YOUR_KEY") is True

    def test_no_openai_base_url_keeps_shape_checks(self, monkeypatch):
        # Regression guard for the inverse: when OPENAI_BASE_URL is NOT set,
        # the bypass must NOT trigger and short/non-sk keys are still caught.
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
        assert looks_like_placeholder_key("short") is True
        assert looks_like_placeholder_key("not-an-sk-key-but-padded-out-aaaaaaaaaaaa") is True


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
# _openai_call — urllib HTTP path (no SDK)
# ---------------------------------------------------------------------------


class TestUrllibCall:
    """Verify the stdlib urllib path: request shape, error handling, base URL."""

    def test_post_shape_matches_chat_completions(self, monkeypatch):
        captured = {}

        class _FakeResp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return self._body

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def _fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data)
            captured["timeout"] = timeout
            return _FakeResp(
                json.dumps(
                    {
                        "model": "gpt-4o-mini",
                        "choices": [
                            {"message": {"content": json.dumps({
                                "verdict": "valid_actionable",
                                "confidence": 0.8,
                                "severity_override": "high",
                                "recommended_action": "fix",
                                "reason": "real",
                            })}}
                        ],
                    }
                ).encode("utf-8")
            )

        from judge import JudgeClient as JC  # noqa: PLC0415

        monkeypatch.setattr(
            "judge._urlrequest.urlopen", _fake_urlopen
        )
        client = JC(api_key="sk-" + "a" * 48)
        r = client.judge({"body": "x"})
        assert r.status == "ok"
        assert r.verdict == "valid_actionable"
        # Request shape — what the SDK previously did, now done by us.
        assert captured["url"].endswith("/chat/completions")
        assert captured["method"] == "POST"
        assert captured["headers"]["Authorization"].lower().startswith("bearer ")
        assert captured["body"]["model"] == "gpt-4o-mini"
        assert captured["body"]["response_format"] == {"type": "json_object"}
        assert captured["body"]["messages"][0]["role"] == "system"

    def test_http_error_raises_judge_error_with_body(self, monkeypatch):
        import io
        from urllib.error import HTTPError

        def _fake_urlopen(req, timeout):  # noqa: ARG001
            raise HTTPError(
                req.full_url, 401, "Unauthorized", hdrs={},
                fp=io.BytesIO(b'{"error":{"message":"Incorrect API key"}}'),
            )

        from judge import JudgeClient as JC, JudgeError  # noqa: PLC0415

        monkeypatch.setattr("judge._urlrequest.urlopen", _fake_urlopen)
        client = JC(api_key="sk-" + "a" * 48)
        with pytest.raises(JudgeError) as excinfo:
            client.judge({"body": "x"})
        # The API's error body should surface verbatim so users see WHY,
        # not a generic message.
        assert "401" in str(excinfo.value)
        assert "Incorrect API key" in str(excinfo.value)

    def test_http_error_body_truncated_when_huge(self, monkeypatch):
        # Corporate proxies / Cloudflare can return multi-KB HTML on
        # 502/403/523. The judge must truncate so the actionable header
        # ("HTTP 502") isn't buried under 4 KB of `<html><head>...`.
        import io
        from urllib.error import HTTPError

        big_html = "<html>" + ("x" * 5000) + "</html>"

        def _fake_urlopen(req, timeout):  # noqa: ARG001
            raise HTTPError(
                req.full_url, 502, "Bad Gateway", hdrs={},
                fp=io.BytesIO(big_html.encode("utf-8")),
            )

        from judge import JudgeClient as JC, JudgeError  # noqa: PLC0415

        monkeypatch.setattr("judge._urlrequest.urlopen", _fake_urlopen)
        client = JC(api_key="sk-" + "a" * 48)
        with pytest.raises(JudgeError) as excinfo:
            client.judge({"body": "x"})
        msg = str(excinfo.value)
        # Status code and truncation marker both present; full HTML is not.
        assert "502" in msg
        assert "truncated" in msg
        assert len(msg) < 600  # i.e., not 5 KB

    def test_url_error_raises_judge_error(self, monkeypatch):
        from urllib.error import URLError

        def _fake_urlopen(*_a, **_kw):
            raise URLError("Name or service not known")

        from judge import JudgeClient as JC, JudgeError  # noqa: PLC0415

        monkeypatch.setattr("judge._urlrequest.urlopen", _fake_urlopen)
        client = JC(api_key="sk-" + "a" * 48)
        with pytest.raises(JudgeError) as excinfo:
            client.judge({"body": "x"})
        assert "network error" in str(excinfo.value).lower()

    def test_invalid_utf8_decoded_with_replace(self, monkeypatch):
        # A misbehaving proxy / gateway can splice in invalid UTF-8 bytes.
        # We must not let a UnicodeDecodeError escape outside the
        # HTTPError/URLError catches as an unhandled exception — that would
        # crash the loop. errors="replace" degrades to a structured
        # JudgeError via the JSON-parse path instead.
        class _FakeResp:
            def read(self):
                # Invalid utf-8 byte 0x80 in the middle of the payload.
                return b'{"choices":[{"message":{"content":"\x80not-json"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        from judge import JudgeClient as JC, JudgeError  # noqa: PLC0415

        monkeypatch.setattr("judge._urlrequest.urlopen", lambda *_a, **_kw: _FakeResp())
        client = JC(api_key="sk-" + "a" * 48)
        # Doesn't matter how this fails — it must NOT be UnicodeDecodeError.
        with pytest.raises(JudgeError):
            client.judge({"body": "x"})

    def test_base_url_override_respected(self, monkeypatch):
        captured = {}

        class _FakeResp:
            def read(self):
                return json.dumps({
                    "model": "x",
                    "choices": [{"message": {"content": json.dumps({
                        "verdict": "valid_actionable", "confidence": 0.5,
                        "severity_override": "low", "recommended_action": "reply",
                        "reason": "ok",
                    })}}],
                }).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def _fake_urlopen(req, timeout):  # noqa: ARG001
            captured["url"] = req.full_url
            return _FakeResp()

        from judge import JudgeClient as JC  # noqa: PLC0415

        monkeypatch.setattr("judge._urlrequest.urlopen", _fake_urlopen)
        client = JC(api_key="sk-" + "a" * 48, base_url="http://localhost:11434/v1")
        client.judge({"body": "x"})
        assert captured["url"].startswith("http://localhost:11434/v1/")


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

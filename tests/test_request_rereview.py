"""Tests for the script-owned re-review request helper."""

from __future__ import annotations

import json
import subprocess

import pytest

import request_rereview
import review_vendors


CREATED_AT = "2026-06-09T07:17:16Z"


def _successful_post(**overrides):
    payload = {"created_at": CREATED_AT, **overrides}
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
    )


def test_post_rereview_uses_argv_list_and_extracts_created_at():
    captured = {}

    def runner(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _successful_post()

    result = request_rereview.post_rereview(
        "OrenAshkenazy/AegisLocal",
        11,
        request_rereview.DEFAULT_PHRASE,
        runner=runner,
    )

    assert result["created_at"] == CREATED_AT
    assert isinstance(captured["argv"], list)
    assert captured["kwargs"].get("shell") is not True
    assert captured["argv"] == [
        "gh",
        "api",
        "repos/OrenAshkenazy/AegisLocal/issues/11/comments",
        "--method",
        "POST",
        "--raw-field",
        f"body={request_rereview.DEFAULT_PHRASE}",
    ]


def test_json_stdout_parses_as_json_and_has_no_ansi(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview,
        "post_rereview",
        lambda repo, pr, phrase, **kwargs: {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        },
    )

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed == {
        "created_at": CREATED_AT,
        "repo": "OrenAshkenazy/AegisLocal",
        "pr": 11,
        "phrase": request_rereview.DEFAULT_PHRASE,
    }
    assert "\033[" not in captured.out
    assert captured.err == ""


def test_human_mode_includes_timestamp(monkeypatch, capsys):
    monkeypatch.setattr(
        request_rereview,
        "post_rereview",
        lambda repo, pr, phrase, **kwargs: {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        },
    )

    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == f"[loop] Re-review requested at {CREATED_AT}"


def test_invalid_repo_format_fails(capsys):
    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal/extra", "--pr", "11"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "OWNER/REPO" in captured.err


def test_invalid_pr_fails(capsys):
    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "0"])

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "positive integer" in captured.err


def test_gh_failure_exits_nonzero_and_writes_clear_stderr(capsys):
    def runner(argv, **kwargs):  # noqa: ARG001
        return subprocess.CompletedProcess(
            args=argv,
            returncode=1,
            stdout="",
            stderr="permission denied",
        )

    original = request_rereview.post_rereview

    def failing_post(repo, pr, phrase, **kwargs):
        return original(repo, pr, phrase, runner=runner)

    request_rereview.post_rereview = failing_post
    try:
        rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])
    finally:
        request_rereview.post_rereview = original

    captured = capsys.readouterr()
    assert rc != 0
    assert captured.out == ""
    assert "gh api failed" in captured.err
    assert "permission denied" in captured.err


def test_default_phrase_is_used_when_omitted(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main(["--repo", "OrenAshkenazy/AegisLocal", "--pr", "11"])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == request_rereview.DEFAULT_PHRASE


def test_reviewer_mention_builds_default_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-mention",
        "coderabbitai",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_review_trigger_mention_alias_builds_default_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--review-trigger-mention",
        "@coderabbitai",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_missing_safe_trigger_returns_json_stop_without_posting(monkeypatch, capsys):
    def fail_post(repo, pr, phrase):  # noqa: ARG001
        raise AssertionError("post_rereview should not be called")

    monkeypatch.setattr(request_rereview, "post_rereview", fail_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-login",
        "unknown-bot",
        "--no-safe-trigger",
        "--json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert captured.err == ""
    assert payload == {
        "status": "no_safe_trigger",
        "posted": False,
        "reviewer": "unknown-bot",
        "message": (
            "No safe re-review trigger known for `unknown-bot`; "
            "pass --review-trigger-mention to enable re-review requests."
        ),
    }


def test_unknown_reviewer_login_without_trigger_does_not_post(monkeypatch, capsys):
    def fail_post(repo, pr, phrase):  # noqa: ARG001
        raise AssertionError("post_rereview should not be called")

    monkeypatch.setattr(request_rereview, "post_rereview", fail_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--reviewer-login",
        "unknown-bot",
        "--json",
    ])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == 0
    assert payload["status"] == "no_safe_trigger"
    assert payload["posted"] is False
    assert payload["reviewer"] == "unknown-bot"


def test_persisted_review_trigger_is_used_when_mention_is_omitted(
    tmp_path, monkeypatch, capsys
):
    captured = {}
    state = {
        "OrenAshkenazy/AegisLocal#11": {
            "reviewer": {
                "login": "coderabbitai",
                "display_name": "CodeRabbit",
                "review_trigger": "@coderabbitai",
                "source": "confirmed",
                "selected_at": "2026-06-28T12:00:00Z",
            }
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--json",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@coderabbitai please review the latest changes."


def test_persisted_codex_reviewer_without_trigger_posts_the_exact_codex_phrase(
    tmp_path, monkeypatch, capsys
):
    """Reviewer records written before Codex was known carry review_trigger: null."""
    captured = {}
    state = {
        "OrenAshkenazy/SignalScout#159": {
            "reviewer": {
                "login": "chatgpt-codex-connector",
                "display_name": "Chatgpt Codex Connector",
                "review_trigger": None,
                "source": "confirmed",
                "selected_at": "2026-08-04T11:24:51Z",
            }
        }
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/SignalScout",
        "--pr",
        "159",
        "--json",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_codex_reviewer_login_posts_the_exact_codex_phrase(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/SignalScout",
        "--pr",
        "159",
        "--reviewer-login",
        "chatgpt-codex-connector",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_codex_mention_does_not_get_the_gemini_sentence_form(monkeypatch, capsys):
    captured = {}

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/SignalScout",
        "--pr",
        "159",
        "--review-trigger-mention",
        "@codex",
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == "@codex review"


def test_custom_phrase_is_preserved_exactly(monkeypatch, capsys):
    captured = {}
    phrase = "@gemini-code-assist please review the latest changes. Extra context: keep JSON clean."

    def fake_post(repo, pr, phrase, **kwargs):
        captured["phrase"] = phrase
        return {
            "created_at": CREATED_AT,
            "repo": repo,
            "pr": pr,
            "phrase": phrase,
        }

    monkeypatch.setattr(request_rereview, "post_rereview", fake_post)

    rc = request_rereview.main([
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--pr",
        "11",
        "--phrase",
        phrase,
    ])

    capsys.readouterr()
    assert rc == 0
    assert captured["phrase"] == phrase


def test_dry_run_does_not_call_gh_and_reports_the_exact_body():
    """The only write to someone else's PR must be previewable."""
    calls = []

    def runner(*args, **kwargs):  # pragma: no cover - must never run
        calls.append(args)
        raise AssertionError("dry run must not invoke gh")

    payload = request_rereview.post_rereview(
        "acme/widget", 159, "@codex review", runner=runner, dry_run=True
    )

    assert calls == []
    assert payload["posted"] is False
    assert payload["dry_run"] is True
    assert payload["phrase"] == "@codex review"
    assert payload["created_at"] is None


def test_dry_run_still_validates_its_arguments():
    with pytest.raises(ValueError):
        request_rereview.post_rereview("not-a-repo", 1, "@codex review", dry_run=True)
    with pytest.raises(ValueError):
        request_rereview.post_rereview("acme/widget", 0, "@codex review", dry_run=True)


def test_default_reviewer_is_installable_on_every_tier():
    """The default applies to every user, so it cannot need a paid tier.

    Gemini still reviews normally for enterprise tenants; it is disqualified as
    a default only because a consumer-tier user can no longer install the app.
    """
    assert not review_vendors.is_consumer_tier_retired(
        request_rereview.DEFAULT_REVIEWER_LOGIN
    )
    assert review_vendors.is_consumer_tier_retired("gemini-code-assist")


def test_a_tier_restricted_vendor_is_still_fully_supported():
    gemini = review_vendors.vendor_for("gemini-code-assist")
    assert gemini is not None
    assert gemini.mention == "@gemini-code-assist"
    assert gemini.rereview_phrase
    assert gemini.auto_reviews is True
    assert "enterprise" in review_vendors.tier_note_for("gemini-code-assist")
    assert review_vendors.tier_note_for("@codex") == ""


class TestCapEnforcement:
    """The cap is the loop's only guarantee it cannot spam a PR."""

    @staticmethod
    def _comments(*bodies_by_login):
        nodes = [{"user": {"login": login}, "body": body}
                 for login, body in bodies_by_login]
        return subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=json.dumps(nodes), stderr=""
        )

    def _runner(self, comments):
        def runner(cmd, **kwargs):
            if cmd[:3] == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="agent\n", stderr=""
                )
            return comments
        return runner

    def test_counts_only_the_agents_own_pings(self):
        runner = self._runner(self._comments(
            ("agent", "@codex review"),
            ("a-human", "@codex review"),
            ("agent", "unrelated comment"),
        ))
        used = request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=runner
        )
        assert used == 1

    def test_returns_none_when_the_count_cannot_be_established(self):
        def failing(cmd, **kwargs):
            return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

        assert request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", "agent", runner=failing
        ) is None
        assert request_rereview.count_agent_pings(
            "acme/widget", 159, "@codex", None
        ) is None

    def test_mention_matches_as_a_whole_word(self):
        pattern = request_rereview._trigger_re("@codex")
        assert pattern.search("@codex review")
        assert pattern.search("please @codex review the latest changes.")
        assert not pattern.search("@codex-reviewer please look")

    def test_main_refuses_to_post_once_the_cap_is_used(self, monkeypatch, capsys):
        posted = []
        monkeypatch.setattr(request_rereview, "post_rereview",
                            lambda *a, **k: posted.append(a))
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_pings",
                            lambda *a, **k: 3)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)

        rc = request_rereview.main(
            ["--repo", "acme/widget", "--pr", "159", "--json"]
        )

        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert posted == []
        assert payload["status"] == "capped"
        assert payload["posted"] is False
        assert payload["rereviews_used"] == 3
        assert payload["rereview_limit"] == 3

    def test_main_posts_while_under_the_cap(self, monkeypatch, capsys):
        posted = []

        def fake_post(repo, pr, phrase, **kwargs):
            posted.append(phrase)
            return {"created_at": CREATED_AT, "repo": repo, "pr": pr, "phrase": phrase}

        monkeypatch.setattr(request_rereview, "post_rereview", fake_post)
        monkeypatch.setattr(request_rereview, "gh_login", lambda *a, **k: "agent")
        monkeypatch.setattr(request_rereview, "count_agent_pings", lambda *a, **k: 2)
        monkeypatch.setattr(request_rereview, "effective_cap", lambda *a: 3)

        assert request_rereview.main(["--repo", "acme/widget", "--pr", "159"]) == 0
        capsys.readouterr()
        assert posted == ["@codex review"]

    def test_a_broken_prefs_file_does_not_lift_the_cap(self, monkeypatch):
        monkeypatch.setattr(request_rereview.judge, "load_preferences",
                            lambda: (_ for _ in ()).throw(OSError("unreadable")))
        assert request_rereview.effective_cap(None) == 3

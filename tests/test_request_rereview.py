"""Tests for the script-owned Gemini re-review request helper."""

from __future__ import annotations

import json
import subprocess

import request_rereview


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
        lambda repo, pr, phrase: {
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
        lambda repo, pr, phrase: {
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

    def failing_post(repo, pr, phrase):
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

    def fake_post(repo, pr, phrase):
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

    def fake_post(repo, pr, phrase):
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

    def fake_post(repo, pr, phrase):
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

    def fake_post(repo, pr, phrase):
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


def test_custom_phrase_is_preserved_exactly(monkeypatch, capsys):
    captured = {}
    phrase = "@gemini-code-assist please review the latest changes. Extra context: keep JSON clean."

    def fake_post(repo, pr, phrase):
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

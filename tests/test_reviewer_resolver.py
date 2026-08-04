"""Tests for reviewer selection/discovery helpers."""

from __future__ import annotations

import reviewer_resolver


def _thread(*comments):
    return {
        "comments": {
            "nodes": list(comments),
        },
    }


def _comment(login, typename="Bot", body="review note"):
    return {
        "author": {
            "login": login,
            "__typename": typename,
        },
        "body": body,
    }


def _pr(*threads):
    return {
        "reviewThreads": {
            "nodes": list(threads),
        },
    }


def test_discovers_distinct_bot_comment_authors_in_first_thread_order():
    pull_request = _pr(
        _thread(_comment("coderabbitai"), _comment("alice", "User")),
        _thread(_comment("gemini-code-assist")),
        _thread(_comment("coderabbitai")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == [
        "coderabbitai",
        "gemini-code-assist",
    ]
    assert candidates[1].display_name == "Gemini Code Assist"
    assert candidates[1].review_trigger == "@gemini-code-assist"


def test_sorts_same_thread_candidates_by_login():
    pull_request = _pr(
        _thread(_comment("zeta-bot"), _comment("alpha-bot")),
        _thread(_comment("middle-bot")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == [
        "alpha-bot",
        "zeta-bot",
        "middle-bot",
    ]


def test_excludes_humans_and_self_login():
    pull_request = _pr(
        _thread(_comment("codex-agent"), _comment("alice", "User")),
        _thread(_comment("qodo-merge-pro")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login="codex-agent",
    )

    assert [candidate.login for candidate in candidates] == ["qodo-merge-pro"]


def test_accepts_bot_suffix_when_typename_is_absent():
    pull_request = _pr(
        _thread({"author": {"login": "github-actions[bot]"}, "body": "review"}),
        _thread({"author": {"login": "plain-human"}, "body": "review"}),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login=None,
    )

    assert [candidate.login for candidate in candidates] == ["github-actions[bot]"]
    assert candidates[0].review_trigger is None


def test_accepts_bot_suffix_even_when_typename_is_user():
    pull_request = _pr(
        _thread(_comment("renovate[bot]", "User")),
    )

    candidates = reviewer_resolver.discover_candidates(
        pull_request,
        self_login=None,
    )

    assert [candidate.login for candidate in candidates] == ["renovate[bot]"]


def test_make_reviewer_record_fills_known_defaults_and_timestamp():
    record = reviewer_resolver.make_reviewer_record(
        "gemini-code-assist",
        source="confirmed",
        selected_at="2026-06-28T12:00:00Z",
    )

    assert record == {
        "login": "gemini-code-assist",
        "display_name": "Gemini Code Assist",
        "review_trigger": "@gemini-code-assist",
        "auto_reviews": True,
        "source": "confirmed",
        "selected_at": "2026-06-28T12:00:00Z",
    }


def test_discovered_codex_gets_its_vendor_name_and_trigger():
    pull_request = _pr(_thread(_comment("chatgpt-codex-connector")))

    candidates = reviewer_resolver.discover_candidates(pull_request, self_login=None)

    assert candidates[0].display_name == "Codex"
    assert candidates[0].review_trigger == "@codex"


def test_trigger_for_knows_codex():
    assert reviewer_resolver.trigger_for("chatgpt-codex-connector") == "@codex"


def test_phrase_for_returns_the_exact_vendor_phrase():
    assert reviewer_resolver.phrase_for("chatgpt-codex-connector") == "@codex review"
    assert (
        reviewer_resolver.phrase_for("gemini-code-assist")
        == "@gemini-code-assist please review the latest changes."
    )


def test_phrase_for_unknown_reviewer_is_none():
    assert reviewer_resolver.phrase_for("coderabbitai") is None


def test_make_reviewer_record_marks_codex_as_not_self_starting():
    record = reviewer_resolver.make_reviewer_record(
        "chatgpt-codex-connector",
        source="confirmed",
        selected_at="2026-08-04T12:00:00Z",
    )

    assert record["auto_reviews"] is False
    assert record["display_name"] == "Codex"
    assert record["review_trigger"] == "@codex"


def test_unknown_reviewers_are_assumed_to_review_on_their_own():
    record = reviewer_resolver.make_reviewer_record(
        "coderabbitai",
        source="confirmed",
        selected_at="2026-08-04T12:00:00Z",
    )

    assert record["auto_reviews"] is True

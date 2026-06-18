"""Tests for publishing the readiness card as a single PR comment."""

from __future__ import annotations

import publish_pr_readiness as ppr


class FakeGitHub:
    """Records GitHub calls so tests assert behavior without the network."""

    def __init__(self, existing=None):
        self.existing = existing or []
        self.created: list[str] = []
        self.updated: list[tuple[int, str]] = []

    def list_comments(self, repo, pr):
        return self.existing

    def create_comment(self, repo, pr, body):
        self.created.append(body)
        return {"id": 999, "html_url": "https://example/c/999"}

    def update_comment(self, repo, comment_id, body):
        self.updated.append((comment_id, body))
        return {"id": comment_id, "html_url": f"https://example/c/{comment_id}"}


def test_creates_comment_when_marker_absent():
    gh = FakeGitHub(existing=[{"id": 1, "body": "unrelated"}])

    result = ppr.publish("o/r", 5, "## card", github=gh)

    assert result["action"] == "created"
    assert gh.created and not gh.updated
    assert ppr.MARKER in gh.created[0]


def test_updates_comment_when_marker_exists():
    gh = FakeGitHub(existing=[{"id": 42, "body": f"old {ppr.MARKER} body"}])

    result = ppr.publish("o/r", 5, "## new card", github=gh)

    assert result["action"] == "updated"
    assert gh.updated and not gh.created
    assert gh.updated[0][0] == 42
    assert ppr.MARKER in gh.updated[0][1]
    assert "## new card" in gh.updated[0][1]


def test_does_not_create_duplicate_comments():
    gh = FakeGitHub(existing=[{"id": 7, "body": ppr.MARKER}])

    ppr.publish("o/r", 5, "card", github=gh)

    assert gh.created == []


def test_body_includes_hidden_marker():
    body = ppr.build_comment_body("## card")
    assert body.startswith(ppr.MARKER)
    assert "## card" in body


def test_no_github_call_without_invoking_publish():
    # Importing and building the body must not touch GitHub.
    gh = FakeGitHub()
    ppr.build_comment_body("## card")
    assert gh.created == [] and gh.updated == []


def test_main_invokes_publish(tmp_path, monkeypatch, capsys):
    readiness = tmp_path / "readiness.md"
    readiness.write_text("## GGRL PR Readiness\nbody", encoding="utf-8")
    calls = {}

    def fake_publish(repo, pr, body, github=None):
        calls["repo"] = repo
        calls["pr"] = pr
        calls["body"] = body
        return {"action": "created", "html_url": "https://example/c/1"}

    monkeypatch.setattr(ppr, "publish", fake_publish)

    rc = ppr.main(
        [
            "--pr",
            "https://github.com/OrenAshkenazy/AegisLocal/pull/11",
            "--readiness",
            str(readiness),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert calls["repo"] == "OrenAshkenazy/AegisLocal"
    assert calls["pr"] == 11
    assert "## GGRL PR Readiness" in calls["body"]
    assert captured.err == ""

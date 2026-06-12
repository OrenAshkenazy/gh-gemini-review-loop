"""Tests for centralized loop output color handling."""

from __future__ import annotations

import json
import sys

import loop_color

from fetch_gemini_threads import PullRequest


def test_color_loop_wraps_text_when_enabled():
    assert (
        loop_color.color_loop("[loop] Summary", enabled=True)
        == f"{loop_color.PURPLE}[loop] Summary{loop_color.RESET}"
    )


def test_color_loop_returns_plain_when_disabled():
    assert loop_color.color_loop("[loop] Summary", enabled=False) == "[loop] Summary"


def test_colors_enabled_false_when_no_color_env_set(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    assert loop_color.colors_enabled() is False


def test_colors_enabled_false_when_ggrl_no_color_env_set(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("GGRL_NO_COLOR", "1")
    assert loop_color.colors_enabled() is False


def test_colors_enabled_false_when_cli_no_color():
    assert loop_color.colors_enabled(no_color=True) is False


def test_loop_prefix_remains_present_after_coloring():
    assert "[loop]" in loop_color.color_loop("[loop] Summary", enabled=True)


def test_fetch_json_output_has_no_ansi_when_color_enabled(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    import fetch_gemini_threads as fgt

    pr = PullRequest(owner="o", repo="r", number=7)
    thread = {
        "id": "t1",
        "path": "a.py",
        "line": 10,
        "comments": {
            "nodes": [{"author": {"login": "gemini-code-assist"}, "body": "issue"}]
        },
    }
    monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
    monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"reviewThreads": {"nodes": [thread]}})
    monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: [])
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "agent")
    monkeypatch.setattr(fgt, "resolve_outdated_threads", lambda *a, **k: 0)
    monkeypatch.setattr(fgt, "resolve_addressed_by_reply", lambda *a, **k: 0)
    monkeypatch.setattr(fgt, "outdated_unresolved_threads", lambda *a, **k: [])
    monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])

    monkeypatch.setattr(sys, "argv", [
        "fetch_gemini_threads.py",
        "--format",
        "json",
        "--judge-mode",
        "off",
    ])

    rc = fgt.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "\033[" not in out
    json.loads(out)


def test_fetch_formatter_no_color_disables_human_color(monkeypatch, capsys):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("GGRL_NO_COLOR", raising=False)
    import fetch_gemini_threads as fgt

    profile = {
        "source": "confirmed",
        "working_directory": ".",
        "checks": [{"name": "root", "command": "uv run pytest", "required": True}],
    }
    monkeypatch.setattr(fgt, "load_profile_for_repo", lambda repo: profile)
    monkeypatch.setattr(sys, "argv", [
        "fetch_gemini_threads.py",
        "--profile-intro",
        "--repo",
        "OrenAshkenazy/AegisLocal",
        "--no-color",
    ])

    rc = fgt.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert out.startswith("[loop] Repo-aware verification profile")
    assert "\033[" not in out

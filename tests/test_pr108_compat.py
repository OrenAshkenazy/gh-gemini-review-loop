"""Regression coverage for PR #108 compatibility review findings."""

from __future__ import annotations

import json

import judge
import metrics


def test_pattern_history_matches_legacy_repo_casing(tmp_path):
    path = tmp_path / "runs.jsonl"
    record = {
        "schema_version": metrics.RECORD_SCHEMA_VERSION,
        "repo": "OREN/GH-Review-Loop",
        "pr": 108,
        "patterns": {
            "signatures": ["shape:guard"],
            "swept": ["shape:guard"],
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    history = metrics.pattern_history_for_pr(
        "oren/gh-review-loop", 108, path=path
    )

    assert history == {
        "seen": {"shape:guard"},
        "swept": {"shape:guard"},
    }


def test_get_profile_falls_back_to_legacy_repo_casing(monkeypatch):
    profile = {"source": "confirmed", "checks": []}
    monkeypatch.setattr(
        judge,
        "load_preferences",
        lambda: {
            "profiles": {
                "OREN/GH-Review-Loop": profile,
            }
        },
    )

    assert judge.get_profile("oren/gh-review-loop") == profile

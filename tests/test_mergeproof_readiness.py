"""Tests for the readiness phase orchestrator."""

from __future__ import annotations

import base64
import json

import mergeproof_readiness as mr


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""

INFRA = {
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
}

LOOP_SUMMARY = {
    "pr_url": "https://github.com/acme/app/pull/7",
    "fixed_count": 7,
    "false_positives_skipped": 1,
    "verification": "passed",
    "verification_command": "uv run pytest",
    "rereview": "completed",
    "cycles_used": 2,
    "cycles_total": 3,
}


class FakeGH:
    def __init__(self, changed, has_config=True, infra=None):
        self.changed = changed
        self.has_config = has_config
        self.infra = infra or INFRA

    def __call__(self, args):
        url = next((arg for arg in args if isinstance(arg, str) and arg.startswith("repos/")), args[-1])
        if "/pulls/7/files" in url:
            return [{"filename": path} for path in self.changed]
        if "/pulls/7" in url:
            return {"base": {"ref": "main", "sha": "base"}}
        if "/contents/mergeproof.yaml" in url:
            if not self.has_config:
                raise RuntimeError("404")
            return {"encoding": "base64", "content": _b64(CONFIG_YAML)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": "isha"}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {"path": p, "type": "blob", "size": len(t)}
                    for p, t in self.infra.items()
                ],
            }
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra[path])}
        raise RuntimeError(f"unexpected {url}")


def test_terminal_phase_renders_when_config_exists():
    gh = FakeGH(changed=["core/api/routes.py"])
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"
    assert "## MergeProof PR Readiness" in result["markdown"]
    assert "<!-- mergeproof-pr-readiness -->" in result["markdown"]


def test_missing_config_skips_without_failing(capsys):
    gh = FakeGH(changed=["core/api/routes.py"], has_config=False)
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "skipped"
    err = capsys.readouterr().err
    assert "[mergeproof] readiness skipped" in err
    assert "Reason: mergeproof.yaml not found" in err


def test_verification_failed_still_renders_with_failed_status():
    gh = FakeGH(changed=["core/api/routes.py"])
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    result = mr.run_readiness("acme/app", 7, summary, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"


def test_load_latest_run_summary_from_recorded_runs_jsonl(tmp_path):
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        json.dumps(
            {
                "repo": "acme/app",
                "pr": 7,
                "fixed_count": 4,
                "verification": "failed",
                "verification_details": {"checks": [{"command": "uv run pytest"}]},
                "outcome": "verification_failed",
                "judge": {"verdicts": {"false_positive": 2}},
                "cycles_used": 1,
                "cycle_cap": 3,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = mr.load_latest_run_summary(runs, "acme/app", 7)
    assert summary["verification"] == "failed"
    assert summary["verification_command"] == "uv run pytest"
    assert summary["false_positives_skipped"] == 2
    assert summary["pr_url"] == "https://github.com/acme/app/pull/7"


def test_readiness_runs_from_recorded_terminal_state():
    gh = FakeGH(changed=["core/api/routes.py"])
    terminal_summary = {
        "pr_url": "https://github.com/acme/app/pull/7",
        "fixed_count": 4,
        "verification": "failed",
        "verification_command": "uv run pytest",
    }
    result = mr.run_readiness("acme/app", 7, terminal_summary, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"

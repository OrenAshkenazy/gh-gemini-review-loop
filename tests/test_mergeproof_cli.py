"""Tests for the MergeProof command surface."""

from __future__ import annotations

import json

import mergeproof


def test_run_writes_json_markdown_and_html_outputs(tmp_path, monkeypatch):
    loop = tmp_path / "loop.json"
    loop.write_text(json.dumps({"verification": "passed"}), encoding="utf-8")
    json_out = tmp_path / "out" / "readiness.json"
    markdown_out = tmp_path / "out" / "readiness.md"
    html_out = tmp_path / "out" / "readiness.html"

    def fake_run_readiness(repo, number, loop_summary, **kwargs):
        assert repo == "O/R"
        assert number == 1
        assert loop_summary["verification"] == "passed"
        assert kwargs["stage_infra"] is True
        assert kwargs["create_infra_pr"] is True
        return {
            "status": "rendered",
            "readiness": {
                "status": "READY",
                "status_label": "READY",
                "reason": "ok",
                "evidence": {},
                "architecture": {},
                "production_risks": [],
                "human_decision": {},
                "next_options": [],
            },
            "markdown": "## GGRL PR Readiness\n",
        }

    monkeypatch.setattr(mergeproof, "run_readiness", fake_run_readiness)

    rc = mergeproof.main(
        [
            "run",
            "--pr",
            "O/R#1",
            "--loop-summary",
            str(loop),
            "--stage-infra",
            "--create-infra-pr",
            "--json-output",
            str(json_out),
            "--markdown-output",
            str(markdown_out),
            "--html-output",
            str(html_out),
        ]
    )

    assert rc == 0
    assert json.loads(json_out.read_text(encoding="utf-8"))["status"] == "READY"
    assert "GGRL PR Readiness" in markdown_out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html_out.read_text(encoding="utf-8")

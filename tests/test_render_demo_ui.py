"""Tests for the static HTML demo report renderer."""

from __future__ import annotations

import json

import render_demo_ui as rdu


READINESS = {
    "status": "HUMAN_DECISION_REQUIRED",
    "status_label": "HUMAN DECISION REQUIRED",
    "reason": "Tests passed, but this PR touches production-facing surfaces.",
    "pr_url": "https://github.com/OrenAshkenazy/AegisLocal/pull/11",
    "evidence": {
        "findings_fixed": 7,
        "false_positives_skipped": 1,
        "verification": "passed",
        "verification_command": "uv run pytest",
        "rereview": "completed",
        "cycles_used": 2,
        "cycles_total": 3,
    },
    "architecture": {
        "service_name": "aegislocal-api",
        "owners": ["platform"],
        "runtime": "kubernetes",
        "exposure": "public",
        "ingress": ["alb", "kong"],
        "datastores": ["postgresql", "redis"],
        "queues": ["sqs:scan-events"],
    },
    "production_risks": [
        {
            "severity": "high",
            "surface": "public_api",
            "reason": "PR touches API route code in a public-facing service",
            "files": ["core/api/routes.py"],
            "human_decision_required": True,
        }
    ],
    "risk_summary": {"highest_severity": "high", "human_decision_required": True, "risk_count": 1},
    "provenance": {
        "sources": [
            {
                "repo": "acme/infra",
                "resolved_sha": "abc1234def",
                "files": ["envs/prod/deploy.yaml", "modules/sqs/main.tf"],
            }
        ],
        "fetched_at": "2026-06-14T00:00:00Z",
        "file_count": 2,
    },
    "safety": {
        "config_changed": False,
        "tree_truncated": False,
        "skipped": [],
        "failed_sources": [],
        "secrets_redacted": True,
    },
    "human_decision": {
        "required": True,
        "review_points": ["API behavior, contract, and error handling"],
    },
    "next_options": [
        "Approve the production risk and merge",
        "Ask AI to adjust the implementation",
        "Split risky behavior into a follow-up PR",
    ],
}


def test_render_returns_html_document():
    html = rdu.render_html(READINESS)
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "GGRL — Production-Aware PR Readiness" in html


def test_includes_status_banner():
    html = rdu.render_html(READINESS)
    assert "HUMAN DECISION REQUIRED" in html


def test_includes_architecture_strip():
    html = rdu.render_html(READINESS)
    assert "aegislocal-api" in html
    assert "scan-events" in html
    assert "→" in html  # architecture flow arrows


def test_includes_production_context_provenance():
    html = rdu.render_html(READINESS)
    assert "Production context pack" in html
    assert "acme/infra" in html
    assert "abc1234" in html
    assert "2 files" in html


def test_includes_pack_safety_status():
    html = rdu.render_html(READINESS)
    assert "Pack safety" in html
    assert "No config, fetch, or source warnings recorded." in html


def test_config_changed_status_has_theme_and_warning():
    data = json.loads(json.dumps(READINESS))
    data["status"] = "CONFIG_CHANGED_REVIEW_REQUIRED"
    data["status_label"] = "CONFIG CHANGED - REVIEW REQUIRED"
    data["safety"]["config_changed"] = True
    html = rdu.render_html(data)
    assert "#ea580c" in html
    assert "Base-branch config was used" in html


def test_includes_fetch_warnings():
    data = json.loads(json.dumps(READINESS))
    data["safety"]["tree_truncated"] = True
    data["safety"]["skipped"] = [{"path": "big.bin", "reason": "binary"}]
    data["safety"]["failed_sources"] = [{"repo": "acme/infra"}]
    html = rdu.render_html(data)
    assert "Tree truncated" in html
    assert "1 skipped (binary)" in html
    assert "acme/infra" in html


def test_includes_risk_table():
    html = rdu.render_html(READINESS)
    assert "core/api/routes.py" in html
    assert "Public API" in html


def test_includes_decision_panel():
    html = rdu.render_html(READINESS)
    assert "Approve the production risk and merge" in html


def test_has_embedded_css():
    html = rdu.render_html(READINESS)
    assert "<style>" in html


def test_no_external_network_assets():
    html = rdu.render_html(READINESS)
    assert "http://" not in html.replace("https://github.com", "")
    assert "<script" not in html.lower()


def test_escapes_html_in_values():
    data = json.loads(json.dumps(READINESS))
    data["architecture"]["service_name"] = "<script>x</script>"
    html = rdu.render_html(data)
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_main_writes_file(tmp_path, capsys):
    readiness = tmp_path / "readiness.json"
    readiness.write_text(json.dumps(READINESS), encoding="utf-8")
    out = tmp_path / "report.html"

    rc = rdu.main(["--readiness", str(readiness), "--output", str(out)])

    captured = capsys.readouterr()
    assert rc == 0
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "HUMAN DECISION REQUIRED" in content
    assert captured.err == ""

"""Tests for the production-aware PR readiness card renderer."""

from __future__ import annotations

import json

import render_pr_readiness as rpr
from render_pr_readiness import build_readiness, main as render_main, render_markdown


LOOP_SUMMARY = {
    "fixed_count": 7,
    "false_positives_skipped": 1,
    "verification": "passed",
    "verification_command": "uv run pytest",
    "rereview": "completed",
    "cycles_used": 2,
    "cycles_total": 3,
    "pr_url": "https://github.com/OrenAshkenazy/AegisLocal/pull/11",
}

ARCH = {
    "service_name": "aegislocal-api",
    "owners": ["platform"],
    "runtime": "kubernetes",
    "exposure": "public",
    "ingress": ["alb", "kong"],
    "datastores": ["postgresql", "redis"],
    "queues": ["sqs:scan-events"],
}

RISKS_HIGH = {
    "production_risks": [
        {
            "severity": "high",
            "surface": "public_api",
            "reason": "PR touches API route code in a public-facing service",
            "files": ["core/api/routes.py"],
            "human_decision_required": True,
        }
    ],
    "summary": {"highest_severity": "high", "human_decision_required": True, "risk_count": 1},
}

RISKS_NONE = {
    "production_risks": [],
    "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0},
}

PACK = {
    "service": "aegislocal-api",
    "facts": ARCH,
    "provenance": {
        "sources": [
            {
                "repo": "acme/infra",
                "resolved_sha": "abc1234",
                "files": ["envs/prod/deploy.yaml"],
            }
        ],
        "fetched_at": "2026-06-14T00:00:00Z",
        "file_count": 1,
    },
    "safety": {"config_changed": False, "secrets_redacted": True},
}


def test_status_human_decision_required_when_risks_exist():
    data = rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH)
    assert data["status"] == "HUMAN_DECISION_REQUIRED"


def test_status_ready_when_no_risks_and_verification_passed():
    data = rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_NONE)
    assert data["status"] == "READY"


def test_status_verification_failed_takes_precedence():
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    data = rpr.build_readiness(summary, ARCH, RISKS_HIGH)
    assert data["status"] == "VERIFICATION_FAILED"


def test_status_human_decision_when_semantic_risk_even_without_prod_risk():
    summary = {**LOOP_SUMMARY, "semantic_risk": True}
    data = rpr.build_readiness(summary, ARCH, RISKS_NONE)
    assert data["status"] == "HUMAN_DECISION_REQUIRED"


def test_status_pending_confirmation():
    summary = {**LOOP_SUMMARY, "pending_confirmation": True}
    data = rpr.build_readiness(summary, ARCH, RISKS_NONE)
    assert data["status"] == "PENDING_CONFIRMATION"


def test_markdown_renders_human_decision_required():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert "## MergeProof PR Readiness" in md
    assert "HUMAN DECISION REQUIRED" in md


def test_markdown_renders_production_context():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert "Production architecture context" in md
    assert "aegislocal-api" in md
    assert "PostgreSQL" in md or "postgresql" in md


def test_markdown_renders_risk_table():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert "Production risks" in md
    assert "core/api/routes.py" in md
    assert "| Severity | Surface | Evidence |" in md


def test_markdown_renders_human_decision_section():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert "Human decision required" in md
    assert "Recommended next options" in md


def test_markdown_renders_merge_evidence():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert "Merge evidence" in md
    assert "uv run pytest" in md
    assert "2/3" in md


def test_output_is_deterministic():
    a = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    b = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, ARCH, RISKS_HIGH))
    assert a == b


def test_main_markdown_stdout(tmp_path, capsys):
    ls = tmp_path / "loop.json"
    ls.write_text(json.dumps(LOOP_SUMMARY), encoding="utf-8")
    arch = tmp_path / "arch.json"
    arch.write_text(json.dumps(ARCH), encoding="utf-8")
    risks = tmp_path / "risks.json"
    risks.write_text(json.dumps(RISKS_HIGH), encoding="utf-8")

    rc = rpr.main(
        [
            "--loop-summary",
            str(ls),
            "--architecture-context",
            str(arch),
            "--production-risks",
            str(risks),
            "--markdown",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "## MergeProof PR Readiness" in captured.out
    assert captured.err == ""


def test_main_json_stdout_has_no_ansi(tmp_path, capsys):
    ls = tmp_path / "loop.json"
    ls.write_text(json.dumps(LOOP_SUMMARY), encoding="utf-8")
    arch = tmp_path / "arch.json"
    arch.write_text(json.dumps(ARCH), encoding="utf-8")
    risks = tmp_path / "risks.json"
    risks.write_text(json.dumps(RISKS_HIGH), encoding="utf-8")

    rc = rpr.main(
        [
            "--loop-summary",
            str(ls),
            "--architecture-context",
            str(arch),
            "--production-risks",
            str(risks),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "\033[" not in captured.out
    payload = json.loads(captured.out)
    assert payload["status"] == "HUMAN_DECISION_REQUIRED"
    assert captured.err == ""


def test_pack_input_is_unwrapped_for_architecture():
    data = rpr.build_readiness(LOOP_SUMMARY, PACK, RISKS_HIGH)
    assert data["architecture"]["service_name"] == "aegislocal-api"
    assert data["status"] == "HUMAN_DECISION_REQUIRED"
    assert data["safety"]["secrets_redacted"] is True


def test_pack_service_fills_unknown_fact_service_name():
    pack = {**PACK, "facts": {**ARCH, "service_name": "unknown"}, "service": "familia-ai"}
    data = rpr.build_readiness(LOOP_SUMMARY, pack, RISKS_NONE)
    assert data["architecture"]["service_name"] == "familia-ai"


def test_config_changed_sets_review_required_status():
    pack = {**PACK, "safety": {"config_changed": True}}
    data = rpr.build_readiness(LOOP_SUMMARY, pack, RISKS_NONE)
    assert data["status"] == "CONFIG_CHANGED_REVIEW_REQUIRED"


def test_verification_failed_outranks_config_changed():
    pack = {**PACK, "safety": {"config_changed": True}}
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    data = rpr.build_readiness(summary, pack, RISKS_HIGH)
    assert data["status"] == "VERIFICATION_FAILED"


def test_markdown_shows_provenance_line():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, PACK, RISKS_HIGH))
    assert "acme/infra@abc1234" in md


_PASS_LOOP = {"pr_url": "https://github.com/o/r/pull/1", "verification": "passed", "fixed_count": 3}
_ARCH = {"service_name": "payments-api", "exposure": "public", "queues": ["redis:arq"]}
_NO_RISK = {"production_risks": [], "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0}}


def test_blocked_obligation_forces_human_decision():
    obligations = [{"type": "worker_deployment", "outcome": "blocked", "evidence_files": ["a"], "inputs": {}, "pack": None, "human_gate_pending": []}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "HUMAN_DECISION_REQUIRED"
    assert r["obligations"] == obligations


def test_human_gated_obligation_forces_human_decision():
    obligations = [{"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["a"], "inputs": {}, "pack": {"approver": "@x"}, "human_gate_pending": ["secret value provisioning"]}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "HUMAN_DECISION_REQUIRED"


def test_all_matched_obligations_do_not_block_ready():
    obligations = [{"type": "worker_deployment", "outcome": "matched", "evidence_files": ["a"], "inputs": {}, "pack": {"approver": "@x"}, "human_gate_pending": []}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "READY"
    assert r["obligations"] == obligations


def test_obligations_default_empty_when_omitted():
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK)
    assert r["obligations"] == []


def test_markdown_renders_obligation_action_panel():
    obligations = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
         "pack": {"generates": ["worker_deployment", "helm_worker_values"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None},
         "human_gate_pending": []},
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["app/secrets/stripe_webhook.py"],
         "inputs": {"secret_name": "stripe_webhook", "service": "payments-api"},
         "pack": {"generates": ["external_secret"], "checks": ["policy"], "approver": "@platform-secrets", "human_gate": "secret value provisioning"},
         "human_gate_pending": ["secret value provisioning", "input: env_var"]},
    ]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    md = render_markdown(r)

    assert "### Production obligations" in md
    assert "worker_deployment" in md
    assert "@platform-runtime" in md
    assert "Needs a human" in md
    assert "secret value provisioning" in md


def test_markdown_omits_obligation_panel_when_none():
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK)
    assert "### Production obligations" not in render_markdown(r)


def _write_json(p, obj):
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_cli_accepts_obligations_file(tmp_path, capsys):
    loop = _write_json(tmp_path / "loop.json", _PASS_LOOP)
    arch = _write_json(tmp_path / "arch.json", _ARCH)
    risks = _write_json(tmp_path / "risks.json", _NO_RISK)
    obs = _write_json(tmp_path / "obs.json", {"obligations": [
        {"type": "worker_deployment", "outcome": "blocked", "evidence_files": ["a"], "inputs": {}, "pack": None, "human_gate_pending": []}
    ]})

    rc = render_main(["--loop-summary", loop, "--architecture-context", arch,
                      "--production-risks", risks, "--obligations", obs, "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["status"] == "HUMAN_DECISION_REQUIRED"
    assert out["obligations"][0]["outcome"] == "blocked"


def test_markdown_action_shows_infra_pr_link_when_staged():
    obligations = [{
        "type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
        "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
        "pack": {"generates": ["worker_deployment"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None},
        "human_gate_pending": [],
        "infra_pr": {"repo": "acme/platform-infra", "branch": "mergeproof/worker_deployment-refund_worker",
                      "create_url": "https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1",
                      "pushed": False, "generated_files": ["envs/prod/payments-api/workers/refund_worker.yaml"]},
    }]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    md = render_markdown(r)
    assert "Open infra PR" in md
    assert "compare/main...mergeproof/worker_deployment-refund_worker" in md

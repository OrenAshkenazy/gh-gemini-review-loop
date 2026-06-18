"""Tests for the static HTML demo report renderer."""

from __future__ import annotations

import json

import render_demo_ui as rdu
from render_demo_ui import render_html


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


_READY = {
    "status": "HUMAN_DECISION_REQUIRED", "status_label": "HUMAN DECISION REQUIRED",
    "reason": "tests passed but production-facing", "pr_url": "https://github.com/o/r/pull/1",
    "evidence": {"findings_fixed": 5, "verification": "passed", "verification_command": "pytest", "rereview": "completed", "cycles_used": 2, "cycles_total": 3, "false_positives_skipped": 1},
    "architecture": {"service_name": "payments-api", "exposure": "public", "ingress": [], "datastores": [], "queues": ["redis:arq"], "owners": [], "runtime": "kubernetes"},
    "production_risks": [], "obligations": [], "human_decision": {"required": True, "review_points": []}, "next_options": ["Approve"],
}


def test_html_has_five_tab_nav_and_readiness_panel():
    html = rdu.render_html(_READY)
    for label in ["Readiness", "Production Flow", "Resolve", "Audit", "Capability Packs"]:
        assert label in html
    assert 'id="tab-readiness"' in html
    assert "HUMAN DECISION REQUIRED" in html
    assert "<script" not in html


def test_flow_tab_places_obligations_on_flow():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker"}, "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment"]},
         "human_gate_pending": [], "infra_pr": {"branch": "mergeproof/worker_deployment-refund_worker", "create_url": "https://x", "pushed": False, "generated_files": ["a.yaml"]}},
    ]
    html = rdu.render_html(readiness)
    assert 'id="tab-flow"' in html
    assert "refund_worker" in html
    assert "@platform-runtime" in html
    assert "worker_deployment" in html


def test_flow_tab_renders_explicit_production_flow_evidence():
    readiness = json.loads(json.dumps(_READY))
    readiness["architecture"]["production_flow"] = [
        {
            "id": "alb",
            "label": "ALB",
            "type": "load_balancer",
            "evidence": ["envs/prod/payments-api/ingress.yaml"],
            "status": "observed",
        },
        {
            "id": "payment_api",
            "label": "Payment API",
            "type": "service",
            "evidence": ["envs/prod/payments-api/deployment.yaml"],
            "status": "observed",
        },
        {
            "id": "external_payment_service",
            "label": "External payment service",
            "type": "external_dependency",
            "evidence": [],
            "status": "inferred",
        },
    ]

    html = rdu.render_html(readiness)

    assert "ALB" in html
    assert "Payment API" in html
    assert "External payment service" in html
    assert "envs/prod/payments-api/ingress.yaml" in html
    assert "missing evidence" in html


def test_resolve_tab_shows_diff_proof_and_button():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker"}, "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment"], "checks": ["helm_template", "policy"], "human_gate": None},
         "human_gate_pending": [],
         "infra_pr": {"branch": "mergeproof/worker_deployment-refund_worker",
                       "create_url": "https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1",
                       "pushed": False, "generated_files": ["envs/prod/payments-api/workers/refund_worker.yaml"],
                       "diff": {"envs/prod/payments-api/workers/refund_worker.yaml": "kind: Deployment\nname: payments-api-refund_worker\n"}}},
    ]
    html = render_html(readiness)
    assert 'id="tab-resolve"' in html
    assert "kind: Deployment" in html
    assert "helm_template" in html and "policy" in html
    assert 'href="https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1"' in html
    assert "Create infra PR" in html


def test_resolve_tab_human_gated_shows_pending_not_button():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["app/secrets/stripe_webhook.py"],
         "inputs": {"secret_name": "stripe_webhook"}, "pack": {"approver": "@platform-secrets", "generates": ["external_secret"], "checks": ["policy"], "human_gate": "secret value provisioning"},
         "human_gate_pending": ["secret value provisioning", "input: env_var"]},
    ]
    html = render_html(readiness)
    assert "secret value provisioning" in html
    assert "input: env_var" in html


def test_audit_tab_lists_evidence_and_obligations_simply():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"], "inputs": {}, "pack": {"approver": "@platform-runtime"}, "human_gate_pending": []},
    ]
    html = render_html(readiness)
    assert 'id="tab-audit"' in html
    assert "app/workers/refund_worker.py" in html
    assert "worker_deployment" in html


def test_packs_tab_lists_declared_capabilities_readonly():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": [], "inputs": {},
         "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment", "helm_worker_values"], "checks": ["policy"], "human_gate": None}, "human_gate_pending": []},
    ]
    html = render_html(readiness)
    assert 'id="tab-packs"' in html
    assert "helm_worker_values" in html
    assert "@platform-runtime" in html
    assert "<form" not in html and "<button" not in html


def test_payments_api_demo_html_tells_the_five_point_story():
    import json
    from pathlib import Path
    from pr_obligations import load_capabilities_and_packs, _read_changed, detect_obligations
    from stage_obligations import stage_obligations
    from render_pr_readiness import build_readiness
    root = Path(__file__).resolve().parent.parent
    F = root / "demo" / "production-readiness" / "payments-api" / "fixtures"
    caps, packs, service = load_capabilities_and_packs(F / "mergeproof.yaml")
    changed = _read_changed(str(F / "changed_files.json"))
    obligations = detect_obligations(changed, caps, packs, service=service)
    obligations = stage_obligations(
        obligations, repo="acme/platform-infra", base="main",
        allow=["envs/prod/payments-api/**", "helm/payments-api/**", "terraform/payments-api/**"],
        templates_root=F / "capabilities", source_pr="https://github.com/acme/payments-api/pull/428", dry_run=True,
    )
    arch = {"service_name": service, "exposure": "public", "queues": ["redis:arq"], "ingress": [], "datastores": [], "owners": [], "runtime": "kubernetes"}
    risks = {"production_risks": [], "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0}}
    loop = json.loads((F / "loop_summary.json").read_text())
    readiness = build_readiness(loop, arch, risks, obligations=obligations)
    html = render_html(readiness)
    assert readiness["status"] == "HUMAN_DECISION_REQUIRED"   # 1: green PR still unsafe
    assert "worker_deployment" in html                         # 2 & 3: obligation seen + on flow
    assert "kind: Deployment" in html                          # 4: generated change
    assert "Create infra PR" in html                           # 4: one-click
    assert "secret value provisioning" in html                 # 5: human owns risk

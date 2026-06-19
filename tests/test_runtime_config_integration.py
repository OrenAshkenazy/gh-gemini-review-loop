import json
from pathlib import Path

from detect_env_obligations import detect_env_obligations


def _load(name: str):
    root = Path(__file__).resolve().parent / "fixtures" / "runtime_config"
    return json.loads((root / name).read_text(encoding="utf-8"))


def _caps_and_packs():
    capabilities = {
        "runtime_config": {"type": "runtime_config", "template": "x", "approver": "@platform-config"},
        "secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"},
    }
    packs = {
        "runtime_config": {
            "capability": "runtime_config",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["helm_env_wiring"], "checks": ["helm_template"],
            "approval": {"required_from": ["platform-config"]}, "human_gate": None,
        },
        "secret_wiring": {
            "capability": "secret_wiring",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["external_secret"], "checks": ["helm_template"],
            "approval": {"required_from": ["platform-secrets"]}, "human_gate": "secret value provisioning",
        },
    }
    return capabilities, packs


def test_full_chain_classifies_url_config_and_key_secret():
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(
        _load("changed_content.json"), _load("infra_files.json"), caps, packs, service="payments-api"
    )
    by_env = {o["inputs"]["env_name"]: o for o in obligations}

    url = by_env["CHARGEBACK_PROVIDER_URL"]
    assert url["type"] == "runtime_config"
    assert url["outcome"] == "matched"
    assert url["inputs"]["scope"] == "api"
    assert url["classification"]["precedent_scope"] == "workload"
    assert url["classification"]["evidence_files"] == ["helm/payments-api/values.yaml"]

    key = by_env["CHARGEBACK_SIGNING_KEY"]
    assert key["type"] == "secret_wiring"
    assert key["outcome"] == "human_gated"
    assert key["inputs"]["scope"] == "worker"
    assert "secret value provisioning" in key["human_gate_pending"]
    assert key["classification"]["evidence_files"] == ["envs/prod/payments-api/secrets/external-secret.yaml"]

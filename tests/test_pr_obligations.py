from pr_obligations import detect_obligations


def _worker_pack():
    return {
        "capability": "worker_deployment",
        "inputs": {"worker_name": "required", "service": "required", "topic": "optional"},
        "generates": ["worker_deployment", "helm_worker_values"],
        "checks": ["helm_template", "policy", "naming_convention"],
        "approval": {"required_from": ["platform-runtime"]},
        "human_gate": None,
    }


def test_added_worker_file_is_matched():
    changed = [{"path": "app/workers/refund_worker.py", "status": "added"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@platform-runtime"}}
    packs = {"worker_deployment": _worker_pack()}

    obligations = detect_obligations(changed, capabilities, packs, service="payments-api")

    assert len(obligations) == 1
    ob = obligations[0]
    assert ob["type"] == "worker_deployment"
    assert ob["outcome"] == "matched"
    assert ob["evidence_files"] == ["app/workers/refund_worker.py"]
    assert ob["inputs"] == {"worker_name": "refund_worker", "service": "payments-api"}
    assert ob["pack"]["approver"] == "@platform-runtime"
    assert ob["human_gate_pending"] == []


def _secret_pack():
    return {
        "capability": "secret_wiring",
        "inputs": {"secret_name": "required", "env_var": "required", "service": "required"},
        "generates": ["external_secret", "helm_env_wiring"],
        "checks": ["helm_template", "policy", "naming_convention"],
        "approval": {"required_from": ["platform-secrets"]},
        "human_gate": "secret value provisioning",
    }


def test_secret_with_human_gate_and_missing_input_is_human_gated():
    changed = [{"path": "app/secrets/stripe_webhook.py", "status": "added"}]
    capabilities = {"secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"}}
    packs = {"secret_wiring": _secret_pack()}

    ob = detect_obligations(changed, capabilities, packs, service="payments-api")[0]

    assert ob["outcome"] == "human_gated"
    assert "secret value provisioning" in ob["human_gate_pending"]
    assert "input: env_var" in ob["human_gate_pending"]


def test_obligation_without_declared_capability_is_blocked():
    changed = [{"path": "app/workers/refund_worker.py", "status": "added"}]
    ob = detect_obligations(changed, capabilities={}, packs={}, service="payments-api")[0]
    assert ob["outcome"] == "blocked"
    assert ob["pack"] is None


def test_infra_irrelevant_change_yields_no_obligation():
    changed = [{"path": "README.md", "status": "modified"}, {"path": "app/util/math.py", "status": "modified"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@x"}}
    packs = {"worker_deployment": _worker_pack()}
    assert detect_obligations(changed, capabilities, packs, service="payments-api") == []


def test_modified_worker_does_not_trigger_added_only_rule():
    changed = [{"path": "app/workers/refund_worker.py", "status": "modified"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@x"}}
    packs = {"worker_deployment": _worker_pack()}
    assert detect_obligations(changed, capabilities, packs, service="payments-api") == []

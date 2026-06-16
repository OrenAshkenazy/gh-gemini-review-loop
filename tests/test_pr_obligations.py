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

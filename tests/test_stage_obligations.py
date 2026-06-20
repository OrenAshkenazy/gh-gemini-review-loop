from stage_obligations import stage_obligations


def _matched_ob():
    return {
        "type": "worker_deployment", "outcome": "matched",
        "evidence_files": ["app/workers/refund_worker.py"],
        "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
        "pack": {"generates": ["worker_deployment"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None,
                 "template_map": {"worker_deployment": {"template": "templates/w.tmpl", "output": "envs/prod/payments-api/workers/${worker_name}.yaml"}}},
        "human_gate_pending": [],
    }


def test_matched_obligation_gets_infra_pr_block(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "w.tmpl").write_text("name: ${service}-${worker_name}\n", encoding="utf-8")
    obligations = [_matched_ob()]
    out = stage_obligations(
        obligations, repo="acme/platform-infra", base="main",
        allow=["envs/prod/payments-api/**"], templates_root=tmp_path,
        source_pr="https://github.com/o/r/pull/1", dry_run=True,
    )
    infra = out[0]["infra_pr"]
    assert infra["pushed"] is False
    assert infra["generated_files"] == ["envs/prod/payments-api/workers/refund_worker.yaml"]
    assert infra["branch"] == "mergeproof/worker_deployment-refund_worker"
    assert "compare/main...mergeproof/worker_deployment-refund_worker" in infra["create_url"]
    assert infra["diff"]["envs/prod/payments-api/workers/refund_worker.yaml"].startswith("name: payments-api-refund_worker")


def test_fanned_out_runtime_config_obligations_get_distinct_branches(tmp_path):
    # Two runtime_config obligations for different env vars must not collide on a
    # single branch — stage_branch checks out -B from base and force-pushes, so a
    # shared branch makes the second obligation overwrite the first's ConfigMap.
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "cfg.tmpl").write_text("data:\n  ${env_name}: set-me\n", encoding="utf-8")

    def _ob(env_name):
        return {
            "type": "runtime_config", "outcome": "matched",
            "evidence_files": ["app/x.py"],
            "inputs": {"env_name": env_name, "service": "payments-api", "scope": "api"},
            "pack": {"generates": ["configmap_entry"], "checks": [], "approver": "@platform-config",
                     "human_gate": None,
                     "template_map": {"configmap_entry": {"template": "templates/cfg.tmpl",
                                      "output": "helm/payments-api/env/${env_name}.yaml"}}},
            "human_gate_pending": [],
        }

    out = stage_obligations(
        [_ob("CHARGEBACK_PROVIDER_URL"), _ob("CHARGEBACK_QUEUE_NAME")],
        repo="acme/platform-infra", base="main", allow=["helm/payments-api/**"],
        templates_root=tmp_path, source_pr="u", dry_run=True,
    )
    branches = {o["infra_pr"]["branch"] for o in out}
    assert len(branches) == 2  # one branch per env var, so neither overwrites the other


def test_human_gated_and_blocked_are_left_unstaged(tmp_path):
    obligations = [
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": [], "inputs": {}, "pack": {}, "human_gate_pending": ["x"]},
        {"type": "worker_deployment", "outcome": "blocked", "evidence_files": [], "inputs": {}, "pack": None, "human_gate_pending": []},
    ]
    out = stage_obligations(obligations, repo="r", base="main", allow=["**"], templates_root=tmp_path,
                            source_pr="u", dry_run=True)
    assert "infra_pr" not in out[0]
    assert "infra_pr" not in out[1]


def test_detect_then_stage_end_to_end_with_demo_fixtures():
    from pathlib import Path
    from pr_obligations import load_capabilities_and_packs, _read_changed, detect_obligations
    root = Path(__file__).resolve().parent.parent
    F = root / "demo" / "production-readiness" / "payments-api" / "fixtures"
    caps, packs, service = load_capabilities_and_packs(F / "mergeproof.yaml")
    changed = _read_changed(str(F / "changed_files.json"))
    obligations = detect_obligations(changed, caps, packs, service=service)
    staged = stage_obligations(
        obligations, repo="acme/platform-infra", base="main",
        allow=["envs/prod/payments-api/**", "helm/payments-api/**", "terraform/payments-api/**"],
        templates_root=F / "capabilities", source_pr="https://github.com/o/r/pull/1", dry_run=True,
    )
    worker = next(o for o in staged if o["type"] == "worker_deployment")
    assert worker["outcome"] == "matched"
    assert "infra_pr" in worker
    assert worker["infra_pr"]["generated_files"] == [
        "envs/prod/payments-api/workers/refund_worker-deployment.yaml",
        "helm/payments-api/workers/refund_worker.yaml",
        "terraform/payments-api/workers/refund_worker.tf",
    ]

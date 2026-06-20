from detect_env_obligations import _obligations_for_read, detect_env_obligations


def _caps_and_packs():
    capabilities = {
        "runtime_config": {"type": "runtime_config", "template": "x", "approver": "@platform-config"},
        "secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"},
    }
    packs = {
        "runtime_config": {
            "capability": "runtime_config",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["helm_env_wiring"],
            "checks": ["helm_template"],
            "approval": {"required_from": ["platform-config"]},
            "human_gate": None,
        },
        "secret_wiring": {
            "capability": "secret_wiring",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["external_secret"],
            "checks": ["helm_template"],
            "approval": {"required_from": ["platform-secrets"]},
            "human_gate": "secret value provisioning",
        },
    }
    return capabilities, packs


def _infra():
    return {
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_API_KEY\n"
        ),
    }


def test_url_read_becomes_matched_runtime_config():
    changed = {"app/api/chargebacks.py": "import os\nos.environ['CHARGEBACK_PROVIDER_URL']\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, _infra(), caps, packs, service="payments-api")

    ob = next(o for o in obligations if o["inputs"].get("env_name") == "CHARGEBACK_PROVIDER_URL")
    assert ob["type"] == "runtime_config"
    assert ob["outcome"] == "matched"
    assert ob["classification"]["classification"] == "config"
    assert ob["classification"]["evidence_files"] == ["helm/payments-api/values.yaml"]
    assert ob["inputs"] == {
        "env_name": "CHARGEBACK_PROVIDER_URL", "service": "payments-api",
        "scope": "api", "config_name": "chargeback-provider-url",
    }


def test_key_read_becomes_human_gated_secret_wiring():
    changed = {"app/workers/chargeback_worker.py": "import os\nos.getenv('CHARGEBACK_API_KEY')\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, _infra(), caps, packs, service="payments-api")

    ob = next(o for o in obligations if o["inputs"].get("env_name") == "CHARGEBACK_API_KEY")
    assert ob["type"] == "secret_wiring"
    assert ob["outcome"] == "human_gated"
    assert "secret value provisioning" in ob["human_gate_pending"]
    assert ob["inputs"]["scope"] == "worker"


def test_unknown_classification_is_human_gated_with_suggestion():
    changed = {"app/api/x.py": "import os\nos.getenv('BRAND_NEW_THING')\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, {}, caps, packs, service="payments-api")

    ob = obligations[0]
    assert ob["type"] == "env_classification"
    assert ob["outcome"] == "human_gated"
    assert ob["classification"]["classification"] == "unknown"
    # BRAND_NEW_THING matches neither suffix table -> deterministic "unknown".
    assert ob["advisory_suggestion"] == "unknown"
    assert any("classify" in g for g in ob["human_gate_pending"])


def test_single_read_emits_multiple_obligations():
    # Fan-out plumbing: one read carrying two routes yields two obligations,
    # each independently citing the same read's env_name / scope / source file.
    caps, packs = _caps_and_packs()
    read = {"name": "CHARGEBACK_QUEUE_NAME", "scope": "worker", "source_file": "app/workers/w.py"}
    routes = [
        {
            "type": "runtime_config",
            "capability": caps["runtime_config"],
            "pack": packs["runtime_config"],
            "extra": {"classification": {"classification": "config"}},
        },
        {
            "type": "secret_wiring",
            "capability": caps["secret_wiring"],
            "pack": packs["secret_wiring"],
            "extra": {"classification": {"classification": "config"}},
        },
    ]
    obligations = _obligations_for_read(read, routes, "payments-api")

    assert len(obligations) == 2
    assert {o["type"] for o in obligations} == {"runtime_config", "secret_wiring"}
    assert all(o["inputs"]["env_name"] == "CHARGEBACK_QUEUE_NAME" for o in obligations)
    assert all(o["inputs"]["scope"] == "worker" for o in obligations)
    assert all(o["evidence_files"] == ["app/workers/w.py"] for o in obligations)


def test_classified_capability_without_declared_pack_is_blocked():
    # classify_env routes CHARGEBACK_API_KEY to secret_wiring, but the config
    # declares no such capability -> blocked (inputs discarded by assemble_obligation).
    changed = {"app/workers/w.py": "import os\nos.getenv('CHARGEBACK_API_KEY')\n"}
    infra = {
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_API_KEY\n"
        )
    }
    obligations = detect_env_obligations(changed, infra, capabilities={}, packs={}, service="payments-api")

    ob = next(o for o in obligations if o["type"] == "secret_wiring")
    assert ob["outcome"] == "blocked"
    assert ob["pack"] is None
    assert ob["classification"]["classification"] == "secret"


def test_k8s_name_is_bounded_to_63_chars_and_valid():
    import re
    from detect_env_obligations import _k8s_name
    n = _k8s_name("A_" * 60)  # 120-char env name
    assert 0 < len(n) <= 63
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", n), n

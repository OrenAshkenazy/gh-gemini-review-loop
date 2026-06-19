from env_precedent import classify_env


def _infra():
    return {
        # workload (payments-api) config source: a *_URL peer in Helm values
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        # workload secret source: a *_KEY peer under an ExternalSecret
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_API_KEY\n"
        ),
        # a different service, used only for the repo-wide fallback case
        "helm/orders-api/values.yaml": "env:\n  SHIPPING_PROVIDER_URL: https://ship.internal\n",
    }


def test_url_classifies_as_config_from_workload_precedent():
    result = classify_env("CHARGEBACK_PROVIDER_URL", _infra(), service="payments-api")
    assert result["classification"] == "config"
    assert result["capability"] == "runtime_config"
    assert result["precedent_scope"] == "workload"
    assert result["evidence_files"] == ["helm/payments-api/values.yaml"]


def test_key_classifies_as_secret_from_workload_precedent():
    result = classify_env("CHARGEBACK_API_KEY", _infra(), service="payments-api")
    assert result["classification"] == "secret"
    assert result["capability"] == "secret_wiring"
    assert result["precedent_scope"] == "workload"
    assert result["evidence_files"] == ["envs/prod/payments-api/secrets/external-secret.yaml"]


def test_repo_wide_fallback_when_service_has_no_local_peer():
    infra = {"helm/orders-api/values.yaml": "env:\n  SHIPPING_PROVIDER_URL: https://ship.internal\n"}
    result = classify_env("CHARGEBACK_PROVIDER_URL", infra, service="payments-api")
    assert result["classification"] == "config"
    assert result["precedent_scope"] == "repo_wide"
    assert result["evidence_files"] == ["helm/orders-api/values.yaml"]


def test_ambiguous_workload_precedent_is_unknown():
    infra = {
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: LEGACY_PROVIDER_URL\n"
        ),
    }
    result = classify_env("CHARGEBACK_PROVIDER_URL", infra, service="payments-api")
    assert result["classification"] == "unknown"
    assert result["capability"] is None
    assert result["precedent_scope"] == "workload"


def test_no_precedent_anywhere_is_unknown():
    result = classify_env("BRAND_NEW_THING", {}, service="payments-api")
    assert result["classification"] == "unknown"
    assert result["precedent_scope"] == "none"
    assert result["evidence_files"] == []


def test_single_file_that_is_both_secret_and_config_source_is_unknown():
    # One file reads as both a secret source (kind: ExternalSecret) and a config
    # source (path ends in values.yaml) -> ambiguous -> unknown, not silently secret.
    infra = {
        "helm/payments-api/values.yaml": (
            "kind: ExternalSecret\nenv:\n  REFUND_PROVIDER_URL: https://refunds.internal\n"
        ),
    }
    result = classify_env("CHARGEBACK_PROVIDER_URL", infra, service="payments-api")
    assert result["classification"] == "unknown"
    assert result["precedent_scope"] == "workload"

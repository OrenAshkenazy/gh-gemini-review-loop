"""`secret_wiring` stays narrow: it is reserved for genuine secret precedent.

A `_TOKEN`-style variable routes to `secret_wiring` *only* when an infra secret
manifest already wires a suffix-peer; a config variable never lands there, and a
`_TOKEN`-named variable with no secret precedent is NOT coerced into
`secret_wiring` by its name alone (the name-pattern table is advisory only).

This is the offline guard for issue 004 (the live run itself is HITL).
"""

from __future__ import annotations

from detect_env_obligations import detect_env_obligations


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
            "approval": {"required_from": ["platform-secrets"]},
            "human_gate": "secret value provisioning",
        },
    }
    return capabilities, packs


# A secret manifest wires a _TOKEN peer; a config file wires a _URL peer.
INFRA = {
    "envs/prod/payments-api/secrets/provider-token.yaml": (
        "kind: ExternalSecret\ndata:\n  - secretKey: REFUND_PROVIDER_TOKEN\n"
    ),
    "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
}


def _obligations(changed, infra=INFRA):
    caps, packs = _caps_and_packs()
    return detect_env_obligations(changed, infra, caps, packs, service="payments-api")


def test_token_var_with_secret_precedent_routes_to_secret_wiring():
    obs = _obligations({"app/workers/w.py": "import os\nos.getenv('CHARGEBACK_PROVIDER_TOKEN')\n"})
    secret_obs = [o for o in obs if o["inputs"].get("env_name") == "CHARGEBACK_PROVIDER_TOKEN"]
    assert [o["type"] for o in secret_obs] == ["secret_wiring"]
    assert secret_obs[0]["outcome"] == "human_gated"
    assert secret_obs[0]["classification"]["classification"] == "secret"


def test_config_var_never_routes_to_secret_wiring():
    obs = _obligations({"app/api/x.py": "import os\nos.environ['CHARGEBACK_PROVIDER_URL']\n"})
    url = next(o for o in obs if o["inputs"].get("env_name") == "CHARGEBACK_PROVIDER_URL")
    assert url["type"] == "runtime_config"
    assert all(o["type"] != "secret_wiring" for o in obs)  # config never lands in secret_wiring


def test_token_name_without_secret_precedent_is_not_coerced_to_secret_wiring():
    # Name ends _TOKEN but no infra peer shares the suffix -> unknown human gate,
    # NOT secret_wiring. The advisory hint may say "secret" but never routes.
    # (Only the config _URL precedent is present; no _TOKEN peer exists.)
    config_only = {"helm/payments-api/values.yaml": INFRA["helm/payments-api/values.yaml"]}
    obs = _obligations(
        {"app/workers/w.py": "import os\nos.getenv('BRANDNEW_ACCESS_TOKEN')\n"}, infra=config_only
    )
    ob = next(o for o in obs if o["inputs"].get("env_name") == "BRANDNEW_ACCESS_TOKEN")
    assert ob["type"] == "env_classification"
    assert ob["outcome"] == "human_gated"
    assert ob["advisory_suggestion"] == "secret"  # advisory only
    assert all(o["type"] != "secret_wiring" for o in obs)

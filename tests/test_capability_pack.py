"""Tests for the Capability Pack model (the approved templates for infra changes)."""

from __future__ import annotations

import pytest

import capability_pack as cp

PACK_OK = """\
capability: secret_wiring
inputs:
  secret_name: required
  env_var: required
  service: required
generates:
  - external_secret
  - helm_env_wiring
checks:
  - helm_template
  - policy
  - naming_convention
approval:
  required_from:
    - platform-secrets
human_gate: secret value provisioning
"""


def test_load_pack_parses_block_yaml():
    pack = cp.load_pack(PACK_OK)
    assert pack["capability"] == "secret_wiring"
    assert pack["inputs"] == {
        "secret_name": "required",
        "env_var": "required",
        "service": "required",
    }
    assert pack["generates"] == ["external_secret", "helm_env_wiring"]
    assert pack["checks"] == ["helm_template", "policy", "naming_convention"]
    assert pack["approval"] == {"required_from": ["platform-secrets"]}
    assert pack["human_gate"] == "secret value provisioning"


def test_human_gate_is_optional():
    text = """\
capability: topic_queue
inputs:
  topic: required
generates:
  - kafka_topic
checks:
  - naming_convention
approval:
  required_from:
    - data-platform
"""
    pack = cp.load_pack(text)
    assert pack["human_gate"] is None


def test_approver_handle_normalizes_to_at_prefixed():
    # required_from carries bare team slugs; the approver display is @-prefixed.
    pack = cp.load_pack(PACK_OK)
    assert cp.pack_approver(pack) == "@platform-secrets"


def test_missing_capability_name_rejected():
    text = "generates:\n  - external_secret\nchecks:\n  - policy\napproval:\n  required_from:\n    - x\n"
    with pytest.raises(cp.CapabilityPackError, match="capability"):
        cp.load_pack(text)


def test_empty_generates_rejected():
    text = "capability: x\ngenerates:\nchecks:\n  - policy\napproval:\n  required_from:\n    - y\n"
    with pytest.raises(cp.CapabilityPackError, match="generates"):
        cp.load_pack(text)


def test_empty_checks_rejected():
    text = "capability: x\ngenerates:\n  - a\nchecks:\napproval:\n  required_from:\n    - y\n"
    with pytest.raises(cp.CapabilityPackError, match="checks"):
        cp.load_pack(text)


def test_missing_approver_rejected():
    text = "capability: x\ngenerates:\n  - a\nchecks:\n  - policy\n"
    with pytest.raises(cp.CapabilityPackError, match="approval|required_from"):
        cp.load_pack(text)


def test_load_pack_preserves_template_map():
    text = (
        "capability: worker_deployment\n"
        "inputs:\n  worker_name: required\n  service: required\n"
        "generates:\n  - worker_deployment\n"
        "checks:\n  - policy\n"
        "approval:\n  required_from:\n    - platform-runtime\n"
        "template_map:\n"
        "  worker_deployment:\n"
        "    template: templates/worker_deployment.tmpl\n"
        "    output: envs/prod/x/${worker_name}.yaml\n"
    )
    from capability_pack import load_pack
    pack = load_pack(text)
    assert pack["template_map"]["worker_deployment"]["output"] == "envs/prod/x/${worker_name}.yaml"


def test_capabilities_from_config_maps_type_to_template_and_approver():
    # The base-branch mergeproof.yaml declares which capability packs apply and
    # who approves each; capability_pack reads that declaration.
    config_text = """\
version: 1
service: payments-api
architecture_sources:
  - repo: acme/platform-infra
    ref: main
    allow:
      - envs/prod/payments-api/**
capabilities:
  - type: secret_wiring
    template: capabilities/secret_wiring.yaml
    approver: "@platform-secrets"
  - type: topic_queue
    template: capabilities/topic_queue.yaml
    approver: "@data-platform"
"""
    caps = cp.capabilities_from_config(config_text)
    assert caps == {
        "secret_wiring": {
            "type": "secret_wiring",
            "template": "capabilities/secret_wiring.yaml",
            "approver": "@platform-secrets",
        },
        "topic_queue": {
            "type": "topic_queue",
            "template": "capabilities/topic_queue.yaml",
            "approver": "@data-platform",
        },
    }

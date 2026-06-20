"""queue_topic fan-out, gated by a structural resource-absence check.

A worker-scope, queue-ish env var (a queue/topic token in its name) with config
precedent always emits `runtime_config`; it *additionally* emits `queue_topic`
only when no provisioned queue/topic resource for the workload exists in the
infra slice. The gate is structural (resource presence), never the var's value.
"""

from __future__ import annotations

import base64
from pathlib import Path

import mergeproof_readiness as mr
from detect_env_obligations import detect_env_obligations

DEMO = Path(__file__).resolve().parent.parent / "demo" / "production-readiness" / "payments-api" / "fixtures"


def _caps_and_packs():
    capabilities = {
        "runtime_config": {"type": "runtime_config", "template": "x", "approver": "@platform-config"},
        "queue_topic": {"type": "queue_topic", "template": "x", "approver": "@platform-runtime"},
    }
    packs = {
        "runtime_config": {
            "capability": "runtime_config",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["helm_env_wiring"], "checks": ["helm_template"],
            "approval": {"required_from": ["platform-config"]}, "human_gate": None,
        },
        "queue_topic": {
            "capability": "queue_topic",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["queue_resource"], "checks": ["policy"],
            "approval": {"required_from": ["platform-runtime"]},
            "human_gate": "provision queue/topic resource for the workload",
        },
    }
    return capabilities, packs


# A config-source file gives the _NAME suffix config precedent (-> runtime_config)
# without provisioning any queue resource.
_VALUES = "helm/payments-api/values.yaml"
_CONFIG_ONLY = {_VALUES: "env:\n  WORKER_QUEUE_NAME: refunds\n"}
_CHANGED = {"app/workers/chargeback_worker.py": "import os\nos.getenv('CHARGEBACK_QUEUE_NAME')\n"}


def _types_for(infra):
    caps, packs = _caps_and_packs()
    obs = detect_env_obligations(_CHANGED, infra, caps, packs, service="payments-api")
    return sorted(o["type"] for o in obs if o["inputs"]["env_name"] == "CHARGEBACK_QUEUE_NAME")


def test_queue_ish_var_with_no_queue_resource_emits_runtime_config_and_queue_topic():
    assert _types_for(_CONFIG_ONLY) == ["queue_topic", "runtime_config"]


def test_same_var_with_provisioned_queue_emits_runtime_config_only():
    infra = {**_CONFIG_ONLY, "terraform/payments-api/sqs.tf": 'resource "aws_sqs_queue" "chargebacks" {}\n'}
    assert _types_for(infra) == ["runtime_config"]


def test_queue_topic_obligation_cites_inspected_infra_paths_not_content():
    caps, packs = _caps_and_packs()
    obs = detect_env_obligations(_CHANGED, _CONFIG_ONLY, caps, packs, service="payments-api")
    qt = next(o for o in obs if o["type"] == "queue_topic")

    absence = qt["resource_absence"]
    assert absence["present"] is False
    assert absence["inspected_paths"] == [_VALUES]  # the path it scanned, cited
    assert "refunds" not in repr(qt)  # no infra content / values leak


# --- end-to-end through the injected-runner seam (both branches) --------------

APP_REPO = "OrenAshkenazy/mergeproof-demo-payments-api"
PR_NUMBER = 9

CONFIG = """\
version: 1
service: payments-api
architecture_sources:
  - repo: OrenAshkenazy/mergeproof-demo-platform-infra
    ref: main
    allow:
      - helm/payments-api/**
      - terraform/payments-api/**
capabilities:
  - type: runtime_config
    template: capabilities/runtime_config.yaml
    approver: "@platform-config"
  - type: queue_topic
    template: capabilities/queue_topic.yaml
    approver: "@platform-runtime"
limits:
  max_files: 200
  max_file_bytes: 262144
"""

# Config precedent for the _NAME suffix (-> runtime_config), no queue resource.
INFRA_ABSENT = {
    "helm/payments-api/values.yaml": "env:\n  LOG_LEVEL: info\n  WORKER_QUEUE_NAME: refunds\n",
}
# Same, plus a provisioned SQS queue for the workload.
INFRA_PRESENT = {
    **INFRA_ABSENT,
    "terraform/payments-api/sqs.tf": 'resource "aws_sqs_queue" "chargebacks" {}\n',
}

E2E_CHANGED = {
    "app/workers/chargeback_worker.py": "import os\nQ = os.getenv('CHARGEBACK_QUEUE_NAME')\n",
}

LOOP_SUMMARY = {
    "pr_url": f"https://github.com/{APP_REPO}/pull/{PR_NUMBER}",
    "verification": "passed",
    "verification_command": "pytest",
    "rereview": "completed",
}


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class DemoGH:
    def __init__(self, infra):
        self.infra = infra

    def __call__(self, args):
        url = next((a for a in args if a.startswith("repos/")), args[-1])
        if f"/pulls/{PR_NUMBER}/files" in url:
            return [{"filename": f, "status": "added"} for f in E2E_CHANGED]
        if f"/pulls/{PR_NUMBER}" in url:
            return {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}}
        if "payments-api/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(CONFIG)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404 Not Found")
        if "platform-infra/commits/" in url:
            return {"sha": "infrasha"}
        if "platform-infra/git/trees/" in url:
            return {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in self.infra.items()
            ]}
        if "platform-infra/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra[path])}
        if "payments-api/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path.startswith("capabilities/"):
                return {"encoding": "base64", "content": _b64((DEMO / path).read_text(encoding="utf-8"))}
            return {"encoding": "base64", "content": _b64(E2E_CHANGED.get(path, ""))}
        raise RuntimeError(f"unexpected url: {url}")


def _e2e_obligations(infra):
    result = mr.run_readiness(APP_REPO, PR_NUMBER, LOOP_SUMMARY, runner=DemoGH(infra))
    by_type = {o["type"]: o for o in result["readiness"]["obligations"]
               if o["inputs"].get("env_name") == "CHARGEBACK_QUEUE_NAME"}
    return result, by_type


def test_e2e_queue_var_without_resource_fans_out_to_two_obligations():
    result, by_type = _e2e_obligations(INFRA_ABSENT)
    assert set(by_type) == {"runtime_config", "queue_topic"}
    assert by_type["runtime_config"]["outcome"] == "matched"

    qt = by_type["queue_topic"]
    assert qt["outcome"] == "human_gated"
    assert qt["resource_absence"]["inspected_paths"] == ["helm/payments-api/values.yaml"]
    # An open human gate escalates the whole run.
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"


def test_e2e_queue_var_with_provisioned_resource_emits_runtime_config_only():
    _result, by_type = _e2e_obligations(INFRA_PRESENT)
    assert set(by_type) == {"runtime_config"}


def test_e2e_infra_slice_never_leaks_into_pack_or_readiness():
    result = mr.run_readiness(APP_REPO, PR_NUMBER, LOOP_SUMMARY, runner=DemoGH(INFRA_PRESENT))
    assert "refunds" not in repr(result["pack"])            # no infra value
    assert "aws_sqs_queue" not in repr(result["readiness"])  # no queue resource text
    assert "WORKER_QUEUE_NAME" not in repr(result["readiness"])  # no infra peer name

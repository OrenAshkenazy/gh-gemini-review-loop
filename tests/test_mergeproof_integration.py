"""End-to-end MergeProof chain, fully offline through an injected GitHub runner."""

from __future__ import annotations

import base64
import json

import build_context_pack as bcp
import mergeproof_readiness as mr


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
      - modules/sqs/**
limits:
  max_files: 50
"""

INFRA = {
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    konghq.com/x: y\n    kubernetes.io/ingress.class: alb\n",
    "envs/prod/redis.yaml": "image: redis:7\n",
    "modules/sqs/main.tf": 'resource "aws_sqs_queue" "scan" {\n  name = "scan-events"\n}\n',
}

LOOP_SUMMARY = {
    "pr_url": "https://github.com/acme/app/pull/9",
    "fixed_count": 5,
    "verification": "passed",
    "verification_command": "uv run pytest",
    "rereview": "completed",
    "cycles_used": 1,
    "cycles_total": 3,
}


class FakeGH:
    def __init__(self, changed):
        self.changed = changed

    def __call__(self, args):
        url = next((arg for arg in args if isinstance(arg, str) and arg.startswith("repos/")), args[-1])
        if "/pulls/9/files" in url:
            return [{"filename": path} for path in self.changed]
        if "/pulls/9" in url:
            return {"base": {"ref": "main", "sha": "base"}}
        if "/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(CONFIG)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": "isha"}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {"path": p, "type": "blob", "size": len(t)}
                    for p, t in INFRA.items()
                ],
            }
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(INFRA[path])}
        raise RuntimeError(url)


def test_full_chain_public_api_and_async_risk():
    gh = FakeGH(changed=["core/api/routes.py", "core/workers/scan_worker.py"])
    result = mr.run_readiness("acme/app", 9, LOOP_SUMMARY, runner=gh)
    readiness = result["readiness"]
    assert readiness["status"] == "HUMAN_DECISION_REQUIRED"
    surfaces = {risk["surface"] for risk in readiness["production_risks"]}
    assert "public_api" in surfaces
    assert "async_processing" in surfaces
    assert readiness["architecture"]["exposure"] == "public"


def test_pack_json_is_machine_clean():
    gh = FakeGH(changed=[])
    pack = bcp.build_pack("acme/app", 9, [], runner=gh, now_iso="2026-06-14T00:00:00Z")
    blob = json.dumps(pack)
    assert "\033[" not in blob
    json.loads(blob)

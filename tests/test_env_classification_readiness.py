"""End-to-end MergeProof readiness for the env-classification (chargeback) demo.

Fully offline: GitHub access is served by an injected fake runner. This is the
front-door proof that the env-var precedent classifier, once wired into the
orchestrator, reaches the *rendered* readiness card — not just a unit dict.

Scenario mirrors the real demo: an app PR reads two new env vars whose suffixes
(_URL, _NAME) have NO wiring precedent in the (sparse) infra slice, so both must
classify as `unknown` -> human-gated, each citing why, with a fenced advisory
hint. It also asserts the raw infra slice never leaks into the published pack.
"""

from __future__ import annotations

import base64

import mergeproof_readiness as mr


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


APP_REPO = "OrenAshkenazy/mergeproof-demo-payments-api"
PR_NUMBER = 5

CONFIG = """\
version: 1
service: payments-api
architecture_sources:
  - repo: OrenAshkenazy/mergeproof-demo-platform-infra
    ref: main
    allow:
      - helm/payments-api/**
limits:
  max_files: 200
  max_file_bytes: 262144
"""

# Sparse infra: config values exist, but NONE share the _URL/_NAME suffix of the
# vars the PR introduces, so precedent is absent -> unknown.
INFRA = {
    "helm/payments-api/values.yaml": "env:\n  LOG_LEVEL: info\n  WORKER_QUEUE: refunds\n",
}

CHANGED = {
    "app/api/chargebacks.py": "import os\nPROVIDER = os.environ['CHARGEBACK_PROVIDER_URL']\n",
    "app/workers/chargeback_worker.py": "import os\nQ = os.getenv('CHARGEBACK_QUEUE_NAME')\n",
}

LOOP_SUMMARY = {
    "pr_url": f"https://github.com/{APP_REPO}/pull/{PR_NUMBER}",
    "verification": "passed",
    "verification_command": "pytest",
    "rereview": "completed",
}


class DemoGH:
    def __call__(self, args):
        url = next((a for a in args if a.startswith("repos/")), args[-1])
        if f"/pulls/{PR_NUMBER}/files" in url:
            return [{"filename": f, "status": "added"} for f in CHANGED]
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
                {"path": p, "type": "blob", "size": len(t)} for p, t in INFRA.items()
            ]}
        if "platform-infra/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(INFRA[path])}
        if "payments-api/contents/" in url:  # changed app file content at PR head
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(CHANGED.get(path, ""))}
        raise RuntimeError(f"unexpected url: {url}")


def _run():
    return mr.run_readiness(APP_REPO, PR_NUMBER, LOOP_SUMMARY, runner=DemoGH())


def test_unknown_env_vars_reach_the_rendered_human_gate_card():
    result = _run()
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"

    obligations = result["readiness"]["obligations"]
    env_obs = {o["inputs"].get("env_name"): o for o in obligations if o["type"] == "env_classification"}
    assert set(env_obs) == {"CHARGEBACK_PROVIDER_URL", "CHARGEBACK_QUEUE_NAME"}

    url_ob = env_obs["CHARGEBACK_PROVIDER_URL"]
    assert url_ob["outcome"] == "human_gated"
    assert url_ob["inputs"]["scope"] == "api"
    assert url_ob["classification"]["classification"] == "unknown"
    assert url_ob["advisory_suggestion"] == "config"

    name_ob = env_obs["CHARGEBACK_QUEUE_NAME"]
    assert name_ob["inputs"]["scope"] == "worker"


def test_rendered_card_shows_precedent_and_fenced_advisory_hint():
    md = _run()["markdown"]
    assert "`env_classification`" in md
    assert "app/api/chargebacks.py" in md            # app-source evidence
    assert "no wiring precedent for suffix _URL" in md  # honest precedent line
    assert "Advisory hint (unverified, not a verdict)" in md
    assert "suggests **config**" in md


def test_infra_slice_never_leaks_into_published_pack():
    result = _run()
    # The raw infra slice is an in-process side channel; it must not surface in
    # the persisted/rendered pack or the readiness object.
    assert "LOG_LEVEL" not in repr(result["pack"])
    assert "WORKER_QUEUE" not in repr(result["readiness"])

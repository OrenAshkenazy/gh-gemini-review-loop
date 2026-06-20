"""runtime_config first-class on the demo, proven two ways.

1. The shipped demo `mergeproof.yaml` declares `runtime_config`, backed by a
   real pack whose every referenced template exists on disk (so the config
   verdict drives generation instead of dead-ending).
2. End-to-end through the injected-runner seam: a config-precedent `_URL`
   variable resolves to a *matched* `runtime_config` obligation that renders on
   the readiness card with its app-source evidence, while a no-precedent
   variable still falls through to `unknown` -> human gate. The raw infra slice
   never leaks into the pack or readiness object.
"""

from __future__ import annotations

import base64
from pathlib import Path

import mergeproof_readiness as mr
from capability_pack import capabilities_from_config, load_pack

DEMO = Path(__file__).resolve().parent.parent / "demo" / "production-readiness" / "payments-api" / "fixtures"


# --- 1. the demo ships a real runtime_config capability -----------------------

def test_demo_declares_runtime_config_backed_by_a_real_pack():
    caps = capabilities_from_config((DEMO / "mergeproof.yaml").read_text(encoding="utf-8"))
    assert "runtime_config" in caps

    pack_path = DEMO / caps["runtime_config"]["template"]
    pack = load_pack(pack_path.read_text(encoding="utf-8"))
    assert pack["capability"] == "runtime_config"
    assert pack["human_gate"] is None  # config wiring is matched, never a human gate

    # Every template the pack references must be shipped on disk, or capability
    # loading 404s at runtime and the matched route dead-ends.
    for spec in pack["template_map"].values():
        tmpl = (pack_path.parent / spec["template"]).resolve()
        assert tmpl.exists(), f"runtime_config pack references missing template {tmpl}"


# --- 2. end-to-end through the injected-runner seam ---------------------------

APP_REPO = "OrenAshkenazy/mergeproof-demo-payments-api"
PR_NUMBER = 7

CONFIG = """\
version: 1
service: payments-api
architecture_sources:
  - repo: OrenAshkenazy/mergeproof-demo-platform-infra
    ref: main
    allow:
      - helm/payments-api/**
capabilities:
  - type: runtime_config
    template: capabilities/runtime_config.yaml
    approver: "@platform-config"
limits:
  max_files: 200
  max_file_bytes: 262144
"""

# A config-source file (values.yaml) wires a _URL suffix-peer -> config precedent
# for CHARGEBACK_PROVIDER_URL. No peer shares the _FLAG suffix -> unknown.
INFRA = {
    "helm/payments-api/values.yaml": "env:\n  LOG_LEVEL: info\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
}

CHANGED = {
    "app/api/chargebacks.py": "import os\nP = os.environ['CHARGEBACK_PROVIDER_URL']\n",
    "app/api/flags.py": "import os\nF = os.getenv('CHARGEBACK_FEATURE_FLAG')\n",
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
    """Serves the *real* shipped demo capability pack + templates from disk, so
    the matched route is proven against the artifact the demo actually ships."""

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
        if "payments-api/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path.startswith("capabilities/"):  # capability pack + its templates
                return {"encoding": "base64", "content": _b64((DEMO / path).read_text(encoding="utf-8"))}
            return {"encoding": "base64", "content": _b64(CHANGED.get(path, ""))}
        raise RuntimeError(f"unexpected url: {url}")


def _run():
    return mr.run_readiness(APP_REPO, PR_NUMBER, LOOP_SUMMARY, runner=DemoGH())


def test_config_precedent_url_var_renders_matched_runtime_config():
    result = _run()
    obligations = result["readiness"]["obligations"]
    by_env = {o["inputs"].get("env_name"): o for o in obligations}

    ob = by_env["CHARGEBACK_PROVIDER_URL"]
    assert ob["type"] == "runtime_config"
    assert ob["outcome"] == "matched"
    assert ob["inputs"]["scope"] == "api"
    assert ob["classification"]["classification"] == "config"
    assert ob["evidence_files"] == ["app/api/chargebacks.py"]


def test_no_precedent_var_still_falls_through_to_human_gate():
    result = _run()
    by_env = {o["inputs"].get("env_name"): o for o in result["readiness"]["obligations"]}

    flag = by_env["CHARGEBACK_FEATURE_FLAG"]
    assert flag["type"] == "env_classification"
    assert flag["outcome"] == "human_gated"
    assert flag["classification"]["classification"] == "unknown"
    # An open human decision escalates the whole run.
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"


def test_matched_runtime_config_renders_on_the_card_with_app_evidence():
    md = _run()["markdown"]
    assert "`runtime_config`" in md
    assert "app/api/chargebacks.py" in md  # app-source evidence on the card


def test_infra_slice_never_leaks_into_pack_or_readiness():
    result = _run()
    assert "refunds.internal" not in repr(result["pack"])      # no infra content
    assert "REFUND_PROVIDER_URL" not in repr(result["readiness"])  # no infra peer name
    assert "LOG_LEVEL" not in repr(result["pack"])

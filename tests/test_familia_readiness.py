"""End-to-end MergeProof readiness for the familia-ai PR #106 demo mock.

Fully offline: GitHub access is served by an injected fake runner. This is the
authoritative proof of the expected demo outcome (HUMAN_DECISION_REQUIRED with
public_api + async_processing risk) since the real familia-ai-infra repo may not
be accessible in CI.
"""

from __future__ import annotations

import base64

import mergeproof_readiness as mr


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


FAMILIA_CONFIG = """\
version: 1
service: familia-ai
architecture_sources:
  - repo: OrenAshkenazy/familia-ai-infra
    ref: main
    allow:
      - helm/familia-ai/**
      - modules/ingress/**
      - modules/postgresql/**
      - modules/redis/**
      - modules/secrets/**
limits:
  max_files: 200
  max_file_bytes: 262144
"""

INFRA = {
    "helm/familia-ai/templates/deployment.yaml": "kind: Deployment\nmetadata:\n  name: familia-ai\n",
    "modules/ingress/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: nginx\n",
    "modules/postgresql/main.tf": 'resource "aws_db_instance" "pg" {\n  engine = "postgres"\n}\n',
    "modules/redis/main.tf": 'resource "aws_elasticache" "redis" {}\n# ARQ worker uses RedisSettings for async jobs\n',
    "modules/secrets/secrets.yaml": (
        "env:\n"
        "  DATABASE_URL: from-secret\n"
        "  REDIS_URL: from-secret\n"
        "  SECRET_KEY: from-secret\n"
        "  ANTHROPIC_API_KEY: from-secret\n"
        "  MIZRAHI_CLIENT_ID: from-secret\n"
        "  ISRACARD_CLIENT_ID: from-secret\n"
        "  MIZRAHI_CLIENT_CERT_PATH: /certs/mizrahi.pem\n"
        "  ISRACARD_CLIENT_CERT_PATH: /certs/isracard.pem\n"
    ),
}

PR_CHANGED = [
    "backend/app/jobs/worker.py",
    "backend/app/routers/scraper_connectors.py",
]

LOOP_SUMMARY = {
    "pr_url": "https://github.com/OrenAshkenazy/familia-ai/pull/106",
    "fixed_count": 6,
    "false_positives_skipped": 1,
    "verification": "passed",
    "verification_command": "uv run pytest",
    "rereview": "completed",
    "cycles_used": 2,
    "cycles_total": 3,
}


class FamiliaGH:
    def __init__(self, changed=PR_CHANGED, config_text=FAMILIA_CONFIG, has_config=True):
        self.changed = changed
        self.config_text = config_text
        self.has_config = has_config

    def __call__(self, args):
        url = next((a for a in args if a.startswith("repos/")), args[-1])
        if "/pulls/106/files" in url:
            return [{"filename": f} for f in self.changed]
        if "/pulls/106" in url:
            return {"base": {"ref": "main", "sha": "basesha"}, "head": {"sha": "headsha"}}
        if "familia-ai/contents/mergeproof.yaml" in url:
            if not self.has_config:
                raise RuntimeError("404 Not Found")
            return {"encoding": "base64", "content": _b64(self.config_text)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404 Not Found")
        if "familia-ai-infra/commits/" in url:
            return {"sha": "infrasha"}
        if "familia-ai-infra/git/trees/" in url:
            return {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in INFRA.items()
            ]}
        if "familia-ai-infra/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(INFRA[path])}
        if "familia-ai/contents/" in url:  # changed app file content at PR head
            return {"encoding": "base64", "content": _b64("")}
        raise RuntimeError(f"unexpected url: {url}")


def _run(gh, summary=LOOP_SUMMARY):
    return mr.run_readiness("OrenAshkenazy/familia-ai", 106, summary, runner=gh)


def test_pr106_produces_human_decision_required():
    result = _run(FamiliaGH())
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"


def test_pr106_maps_expected_risks():
    result = _run(FamiliaGH())
    risks = {r["surface"]: r for r in result["risks"]["production_risks"]}

    assert risks["public_api"]["severity"] == "high"
    assert risks["public_api"]["human_decision_required"] is True
    assert risks["public_api"]["files"] == ["backend/app/routers/scraper_connectors.py"]

    assert risks["async_processing"]["severity"] == "medium"
    assert risks["async_processing"]["human_decision_required"] is True
    assert risks["async_processing"]["files"] == ["backend/app/jobs/worker.py"]

    summary = result["risks"]["summary"]
    assert summary["highest_severity"] == "high"
    assert summary["human_decision_required"] is True


def test_pr106_pack_facts_describe_production_surfaces():
    facts = _run(FamiliaGH())["pack"]["facts"]
    assert facts["runtime"] == "kubernetes"
    assert facts["exposure"] == "public"
    assert "postgresql" in facts["datastores"]
    assert "redis" in facts["datastores"]
    assert "redis:arq" in facts["queues"]
    assert "MIZRAHI_CLIENT_ID" in facts["secrets_or_env"]
    assert "MIZRAHI_CLIENT_CERT_PATH" in facts["secrets_or_env"]


def test_pr106_pack_has_no_raw_contents_or_secret_values():
    pack = _run(FamiliaGH())["pack"]
    blob = repr(pack)
    assert "kind: Deployment" not in blob
    assert "/certs/mizrahi.pem" not in blob  # no secret values
    assert "from-secret" not in blob


def test_pr106_card_branding_and_sensitive_callout():
    md = _run(FamiliaGH())["markdown"]
    assert md.startswith("<!-- mergeproof-pr-readiness -->")
    assert "## MergeProof PR Readiness" in md
    assert "familia-ai" in md
    assert "leak in connector or worker logs" in md


def test_pr106_config_changed_flags_review_required():
    gh = FamiliaGH(changed=PR_CHANGED + ["mergeproof.yaml"])
    result = _run(gh)
    assert result["readiness"]["status"] == "CONFIG_CHANGED_REVIEW_REQUIRED"


def test_pr106_verification_failed_takes_precedence():
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    result = _run(FamiliaGH(), summary=summary)
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"


def test_pr106_missing_config_skips_cleanly(capsys):
    result = _run(FamiliaGH(has_config=False))
    assert result["status"] == "skipped"
    err = capsys.readouterr().err
    assert "[mergeproof] readiness skipped" in err

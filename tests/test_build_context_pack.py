"""Tests for Production Context Pack assembly."""

from __future__ import annotations

import base64

import build_context_pack as bcp


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_inaccessible_infra_repo_ref_resolution_is_partial_not_crash():
    """A 404 on the infra repo (at ref resolution) yields a partial pack."""
    config = (
        "service: familia-ai\n"
        "architecture_sources:\n"
        "  - repo: o/missing-infra\n"
        "    ref: main\n"
        "    allow:\n"
        "      - helm/**\n"
    )

    def runner(args):
        url = next((a for a in args if a.startswith("repos/")), args[-1])
        if "/pulls/" in url:
            return {"base": {"ref": "main", "sha": "base"}, "head": {"sha": "head"}}
        if "/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(config)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        # Infra repo is inaccessible: ref resolution 404s.
        raise RuntimeError("gh: Not Found (HTTP 404)")

    pack = bcp.build_pack("o/app", 5, changed_files=[], runner=runner)
    assert pack is not None  # did not crash
    assert pack["provenance"]["file_count"] == 0
    assert pack["safety"]["failed_sources"]


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""

INFRA = {
    "envs/prod/deploy.yaml": "kind: Deployment\n",
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
    "envs/prod/sqs.tf": 'resource "aws_sqs_queue" "scan" {\n  name = "scan-events"\n}\n',
}


class FakeGH:
    def __init__(self, infra_files, base_sha="base", source_sha="isha", fail_infra=False):
        self.infra_files = infra_files
        self.base_sha = base_sha
        self.source_sha = source_sha
        self.fail_infra = fail_infra

    def __call__(self, args):
        url = args[-1]
        if "/pulls/" in url:
            return {"base": {"ref": "main", "sha": self.base_sha}}
        if "/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(CONFIG_YAML)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": self.source_sha}
        if "/git/trees/" in url:
            if self.fail_infra:
                raise RuntimeError("403 Forbidden")
            return {
                "truncated": False,
                "tree": [
                    {"path": p, "type": "blob", "size": len(t)}
                    for p, t in self.infra_files.items()
                ],
            }
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra_files[path])}
        raise RuntimeError(f"unexpected {url}")


def test_pack_has_facts_provenance_and_safety():
    gh = FakeGH(INFRA)
    pack = bcp.build_pack(
        "acme/app", 7, changed_files=[], runner=gh, now_iso="2026-06-14T00:00:00Z"
    )
    assert pack["service"] == "aegislocal-api"
    assert pack["facts"]["service_name"] == "aegislocal-api"
    assert pack["facts"]["runtime"] == "kubernetes"
    assert "sqs:scan-events" in pack["facts"]["queues"]
    assert pack["provenance"]["file_count"] == 3
    assert pack["provenance"]["sources"][0]["resolved_sha"] == "isha"
    assert pack["safety"]["secrets_redacted"] is True
    assert pack["safety"]["config_changed"] is False


def test_pack_never_contains_raw_file_contents_or_changed_files():
    gh = FakeGH(INFRA)
    pack = bcp.build_pack("acme/app", 7, changed_files=["core/api/routes.py"], runner=gh)
    text = repr(pack)
    assert "kind: Deployment" not in text
    assert "aws_sqs_queue" not in text
    assert "core/api/routes.py" not in text


def test_inaccessible_source_records_failure_not_crash():
    gh = FakeGH(INFRA, fail_infra=True)
    pack = bcp.build_pack("acme/app", 7, changed_files=[], runner=gh)
    assert pack["provenance"]["file_count"] == 0
    assert pack["safety"]["failed_sources"]


def test_missing_config_returns_none():
    class NoConfig(FakeGH):
        def __call__(self, args):
            if "/contents/mergeproof" in args[-1]:
                raise RuntimeError("404")
            return super().__call__(args)

    pack = bcp.build_pack("acme/app", 7, changed_files=[], runner=NoConfig(INFRA))
    assert pack is None

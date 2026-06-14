"""Tests for trusted-ref MergeProof config resolution."""

from __future__ import annotations

import base64

import resolve_mergeproof as rm


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""


class FakeGH:
    def __init__(
        self,
        base_sha="basesha",
        config=("mergeproof.yaml", CONFIG_YAML),
        source_sha="resolvedsha",
        missing=False,
    ):
        self.base_sha = base_sha
        self.config = config
        self.source_sha = source_sha
        self.missing = missing
        self.config_ref_seen = None

    def __call__(self, args):
        url = args[-1]
        if "/pulls/" in url:
            return {"base": {"ref": "main", "sha": self.base_sha}, "head": {"sha": "headsha"}}
        if "/contents/mergeproof" in url:
            self.config_ref_seen = url.split("ref=")[-1]
            path = url.split("/contents/")[1].split("?")[0]
            if self.missing or path != self.config[0]:
                raise RuntimeError("404 Not Found")
            return {"encoding": "base64", "content": _b64(self.config[1])}
        if "/commits/" in url:
            return {"sha": self.source_sha}
        raise RuntimeError(f"unexpected: {url}")


def test_reads_config_from_base_sha():
    gh = FakeGH()
    result = rm.resolve("acme/app", 7, changed_files=[], runner=gh)
    assert result["status"] == "OK"
    assert gh.config_ref_seen == "basesha"
    assert result["config"]["service"] == "aegislocal-api"
    assert result["config"]["architecture_sources"][0]["resolved_sha"] == "resolvedsha"
    assert result["config_changed"] is False


def test_pr_modified_config_uses_base_and_flags():
    gh = FakeGH()
    result = rm.resolve("acme/app", 7, changed_files=["mergeproof.yaml", "src/x.py"], runner=gh)
    assert result["status"] == "CONFIG_CHANGED_REVIEW_REQUIRED"
    assert result["config_changed"] is True
    assert gh.config_ref_seen == "basesha"


def test_trust_pr_config_reads_pr_head():
    gh = FakeGH()
    result = rm.resolve(
        "acme/app",
        7,
        changed_files=["mergeproof.yaml"],
        runner=gh,
        trust_pr_config=True,
        pr_head_sha="prheadsha",
    )
    assert gh.config_ref_seen == "prheadsha"
    assert result["status"] == "OK"


def test_missing_config_returns_missing_status():
    gh = FakeGH(missing=True)
    result = rm.resolve("acme/app", 7, changed_files=[], runner=gh)
    assert result["status"] == "MISSING"
    assert result["config"] is None

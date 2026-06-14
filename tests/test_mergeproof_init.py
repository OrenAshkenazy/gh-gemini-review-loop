"""Tests for mergeproof init."""

from __future__ import annotations

import pytest

import mergeproof


def test_infer_allowlist_for_familia_style_monorepo(tmp_path):
    for rel in [
        "helm/familia-ai/values.yaml",
        "helm/familia-ai/templates/backend/deployment.yaml",
        "helm/familia-ai/templates/worker/deployment.yaml",
        "helm/familia-ai/templates/redis/deployment.yaml",
        "helm/familia-ai/templates/ingress.yaml",
        "infra/terraform/environments/beta/main.tf",
        "infra/terraform/modules/ecs/main.tf",
        "backend/app/jobs/worker.py",
    ]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")

    allow = mergeproof.infer_allowlist(tmp_path)

    assert "helm/*/templates/backend/**" in allow
    assert "infra/terraform/modules/ecs/**" in allow
    assert "backend/app/jobs/**" in allow


def test_init_writes_mergeproof_yaml(tmp_path):
    (tmp_path / "infra/terraform/modules/ecs").mkdir(parents=True)

    rc = mergeproof.main(
        [
            "init",
            "--repo-root",
            str(tmp_path),
            "--repo",
            "O/R",
            "--service",
            "svc",
        ]
    )

    text = (tmp_path / "mergeproof.yaml").read_text(encoding="utf-8")
    assert rc == 0
    assert "service: svc" in text
    assert "repo: O/R" in text
    assert "infra/terraform/modules/ecs/**" in text


def test_init_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "mergeproof.yaml").write_text("existing", encoding="utf-8")

    rc = mergeproof.main(["init", "--repo-root", str(tmp_path), "--repo", "O/R"])

    assert rc == 1
    assert (tmp_path / "mergeproof.yaml").read_text(encoding="utf-8") == "existing"


def test_render_config_is_parseable():
    import mergeproof_config

    text = mergeproof.render_config(
        service="svc",
        repo="O/R",
        ref="main",
        allow=["infra/**"],
    )

    cfg = mergeproof_config.load_config(text, fmt="yaml")
    assert cfg["service"] == "svc"
    assert cfg["architecture_sources"][0]["allow"] == ["infra/**"]

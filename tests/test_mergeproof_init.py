"""Tests for `mergeproof init` (delegates to init_mergeproof discovery)."""

from __future__ import annotations

import mergeproof
import mergeproof_config


def _write(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_init_discovers_real_infra_and_writes_yaml(tmp_path):
    _write(tmp_path, "infra/terraform/modules/ecs/main.tf", 'resource "x" "y" {}')
    _write(tmp_path, "helm/familia-ai/values.yaml")

    rc = mergeproof.main(["init", "--repo-root", str(tmp_path), "--repo", "O/R", "--service", "svc"])

    text = (tmp_path / "mergeproof.yaml").read_text(encoding="utf-8")
    cfg = mergeproof_config.load_config(text, fmt="yaml")
    assert rc == 0
    assert cfg["service"] == "svc"
    # No --infra-repo -> same-repo: the app repo is the source.
    assert cfg["architecture_sources"][0]["repo"] == "O/R"
    allow = cfg["architecture_sources"][0]["allow"]
    assert "infra/terraform/modules/ecs/**" in allow
    assert "helm/familia-ai/**" in allow


def test_init_separate_infra_repo(tmp_path):
    _write(tmp_path, "infra/terraform/main.tf")
    rc = mergeproof.main([
        "init", "--repo-root", str(tmp_path),
        "--repo", "O/app", "--infra-repo", "O/infra", "--service", "svc",
    ])
    cfg = mergeproof_config.load_config(
        (tmp_path / "mergeproof.yaml").read_text(encoding="utf-8"), fmt="yaml"
    )
    assert rc == 0
    assert cfg["architecture_sources"][0]["repo"] == "O/infra"


def test_init_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "mergeproof.yaml").write_text("existing", encoding="utf-8")

    rc = mergeproof.main(["init", "--repo-root", str(tmp_path), "--repo", "O/R"])

    assert rc == 1
    assert (tmp_path / "mergeproof.yaml").read_text(encoding="utf-8") == "existing"


def test_init_print_outputs_parseable_config(tmp_path, capsys):
    _write(tmp_path, "infra/terraform/main.tf")
    rc = mergeproof.main([
        "init", "--repo-root", str(tmp_path), "--repo", "O/R", "--service", "svc", "--print",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    cfg = mergeproof_config.load_config(out, fmt="yaml")
    assert cfg["service"] == "svc"
    assert "infra/terraform/**" in cfg["architecture_sources"][0]["allow"]

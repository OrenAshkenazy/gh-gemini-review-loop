"""Tests for the standalone MergeProof config bootstrapper."""

from __future__ import annotations

import mergeproof_config as mc
import init_mergeproof as im


def test_generates_valid_config_for_familia_ai():
    text = im.render_config(
        service="familia-ai",
        infra_repo="OrenAshkenazy/familia-ai-infra",
        ref="main",
        env="prod",
        allow=im.default_allow("familia-ai"),
    )
    cfg = mc.load_config(text, fmt="yaml")
    assert cfg["service"] == "familia-ai"
    src = cfg["architecture_sources"][0]
    assert src["repo"] == "OrenAshkenazy/familia-ai-infra"
    assert src["ref"] == "main"
    assert "helm/familia-ai/**" in src["allow"]
    assert "modules/redis/**" in src["allow"]
    assert "modules/secrets/**" in src["allow"]
    assert cfg["limits"] == {"max_files": 200, "max_file_bytes": 262144}


def test_supports_custom_allow_paths():
    text = im.render_config(
        service="svc",
        infra_repo="o/infra",
        ref="main",
        env="prod",
        allow=["custom/path/**", "other/**"],
    )
    cfg = mc.load_config(text, fmt="yaml")
    assert cfg["architecture_sources"][0]["allow"] == ["custom/path/**", "other/**"]


def test_main_prints_config_without_network(capsys):
    rc = im.main([
        "--service", "familia-ai",
        "--infra-repo", "OrenAshkenazy/familia-ai-infra",
        "--env", "prod",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    cfg = mc.load_config(out, fmt="yaml")
    assert cfg["service"] == "familia-ai"


def test_main_writes_output_file(tmp_path):
    out = tmp_path / "mergeproof.yaml"
    rc = im.main([
        "--service", "familia-ai",
        "--infra-repo", "OrenAshkenazy/familia-ai-infra",
        "--env", "prod",
        "--output", str(out),
    ])
    assert rc == 0
    cfg = mc.load_config(out.read_text(encoding="utf-8"), fmt="yaml")
    assert cfg["service"] == "familia-ai"

"""Tests for the MergeProof config bootstrapper."""

from __future__ import annotations

import mergeproof_config as mc
import init_mergeproof as im


def _write(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_render_config_is_valid():
    text = im.render_config(
        service="familia-ai",
        infra_repo="OrenAshkenazy/familia-ai",
        ref="main",
        env="prod",
        allow=["infra/terraform/**", "helm/familia-ai/**"],
    )
    cfg = mc.load_config(text, fmt="yaml")
    assert cfg["service"] == "familia-ai"
    assert cfg["architecture_sources"][0]["repo"] == "OrenAshkenazy/familia-ai"
    assert cfg["architecture_sources"][0]["allow"] == ["infra/terraform/**", "helm/familia-ai/**"]


def test_discover_allow_finds_real_infra_dirs_and_ignores_noise(tmp_path):
    _write(tmp_path, "infra/terraform/main.tf", 'resource "x" "y" {}')
    _write(tmp_path, "infra/terraform/modules/ecs/main.tf")
    _write(tmp_path, "helm/familia-ai/values.yaml")
    _write(tmp_path, "helm/familia-ai/Chart.yaml")
    _write(tmp_path, "k8s/deployment.yaml", "kind: Deployment")
    _write(tmp_path, "Dockerfile", "FROM python")
    _write(tmp_path, "backend/Dockerfile", "FROM python")  # weak signal in a subdir
    # noise that must be ignored:
    _write(tmp_path, "node_modules/pkg/x.tf")
    _write(tmp_path, ".git/config")
    _write(tmp_path, "backend/app/main.py", "print()")

    allow = im.discover_allow(tmp_path)

    assert "infra/terraform/**" in allow          # collapsed to the top dir
    assert "infra/terraform/modules/ecs/**" not in allow  # subsumed by the prefix
    assert "helm/familia-ai/**" in allow
    assert "k8s/**" in allow
    assert "Dockerfile" in allow                  # root-level signal kept by name
    assert "backend/Dockerfile" in allow          # weak signal -> file, not dir/**
    assert "backend/**" not in allow              # must NOT glob all app source
    assert not any("node_modules" in g for g in allow)


def test_discover_allow_empty_repo_returns_nothing(tmp_path):
    _write(tmp_path, "README.md", "# hi")
    assert im.discover_allow(tmp_path) == []


def test_main_same_repo_default_uses_app_repo_as_source(tmp_path):
    _write(tmp_path, "infra/terraform/main.tf", 'resource "x" "y" {}')
    out = tmp_path / "mergeproof.yaml"
    rc = im.main([
        "--repo", "OrenAshkenazy/familia-ai",
        "--repo-root", str(tmp_path),
        "--output", str(out),
    ])
    assert rc == 0
    cfg = mc.load_config(out.read_text(encoding="utf-8"), fmt="yaml")
    # No --infra-repo given -> same-repo: source IS the app repo.
    assert cfg["architecture_sources"][0]["repo"] == "OrenAshkenazy/familia-ai"
    assert cfg["service"] == "familia-ai"
    # allowlist came from the real scan, not static guesses.
    assert "infra/terraform/**" in cfg["architecture_sources"][0]["allow"]


def test_main_separate_infra_repo_when_explicit(tmp_path, capsys):
    _write(tmp_path, "infra/terraform/main.tf")
    rc = im.main([
        "--repo", "OrenAshkenazy/familia-ai",
        "--infra-repo", "OrenAshkenazy/familia-ai-infra",
        "--repo-root", str(tmp_path),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    cfg = mc.load_config(out, fmt="yaml")
    assert cfg["architecture_sources"][0]["repo"] == "OrenAshkenazy/familia-ai-infra"


def test_main_infers_repo_from_git_remote(tmp_path):
    _write(tmp_path, "infra/terraform/main.tf")
    _write(
        tmp_path,
        ".git/config",
        '[remote "origin"]\n\turl = git@github.com:OrenAshkenazy/familia-ai.git\n',
    )
    out = tmp_path / "mergeproof.yaml"
    rc = im.main(["--repo-root", str(tmp_path), "--output", str(out)])
    assert rc == 0
    cfg = mc.load_config(out.read_text(encoding="utf-8"), fmt="yaml")
    assert cfg["architecture_sources"][0]["repo"] == "OrenAshkenazy/familia-ai"
    assert cfg["service"] == "familia-ai"


def test_main_errors_without_resolvable_repo(tmp_path, capsys):
    rc = im.main(["--repo-root", str(tmp_path)])
    assert rc != 0
    assert "repo" in capsys.readouterr().err.lower()


def test_explicit_allow_overrides_discovery(tmp_path):
    _write(tmp_path, "infra/terraform/main.tf")
    out = tmp_path / "mergeproof.yaml"
    rc = im.main([
        "--repo", "o/app", "--repo-root", str(tmp_path),
        "--allow", "only/this/**", "--output", str(out),
    ])
    assert rc == 0
    cfg = mc.load_config(out.read_text(encoding="utf-8"), fmt="yaml")
    assert cfg["architecture_sources"][0]["allow"] == ["only/this/**"]

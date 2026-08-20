"""Tests for the bundle/profile JSON loaders."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from loaders import bundle, profiles  # noqa: E402


def _write(path: Path, data: object) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestBundleLoaders:
    def test_read_manifest_returns_dict(self, tmp_path):
        path = _write(tmp_path / "b.json", {"manifest": {"a": 1}})
        assert bundle.read_manifest(path) == {"a": 1}

    def test_read_manifest_rejects_non_dict_root(self, tmp_path):
        path = _write(tmp_path / "b.json", [1, 2, 3])
        with pytest.raises(ValueError, match="root"):
            bundle.read_manifest(path)

    def test_read_manifest_rejects_non_dict_manifest(self, tmp_path):
        path = _write(tmp_path / "b.json", {"manifest": "nope"})
        with pytest.raises(ValueError, match="manifest"):
            bundle.read_manifest(path)

    def test_read_lockfile_returns_list(self, tmp_path):
        path = _write(tmp_path / "b.json", {"pinned": ["1.0.0", "2.0.0"]})
        assert bundle.read_lockfile(path) == ["1.0.0", "2.0.0"]

    def test_read_lockfile_rejects_non_string_entries(self, tmp_path):
        path = _write(tmp_path / "b.json", {"pinned": [1, 2]})
        with pytest.raises(ValueError, match="pinned"):
            bundle.read_lockfile(path)

    def test_read_overrides_rejects_null(self, tmp_path):
        path = _write(tmp_path / "b.json", {"overrides": None})
        with pytest.raises(ValueError, match="overrides"):
            bundle.read_overrides(path)


class TestProfileLoaders:
    def test_load_profile_returns_dict(self, tmp_path):
        path = _write(tmp_path / "p.json", {"profile": {"checks": []}})
        assert profiles.load_profile(path) == {"checks": []}

    def test_load_profile_rejects_non_dict_root(self, tmp_path):
        path = _write(tmp_path / "p.json", ["not", "a", "dict"])
        with pytest.raises(ValueError, match="root"):
            profiles.load_profile(path)

    def test_load_reviewer_map_returns_dict(self, tmp_path):
        path = _write(tmp_path / "p.json", {"reviewers": {"org/repo": "gemini-code-assist"}})
        assert profiles.load_reviewer_map(path) == {"org/repo": "gemini-code-assist"}

    def test_load_reviewer_map_rejects_null_value(self, tmp_path):
        path = _write(tmp_path / "p.json", {"reviewers": {"org/repo": None}})
        with pytest.raises(ValueError, match="reviewers"):
            profiles.load_reviewer_map(path)

    def test_load_team_defaults_rejects_non_dict(self, tmp_path):
        path = _write(tmp_path / "p.json", {"defaults": []})
        with pytest.raises(ValueError, match="defaults"):
            profiles.load_team_defaults(path)

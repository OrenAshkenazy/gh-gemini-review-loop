"""Tests for allowlisted infra file fetching."""

from __future__ import annotations

import base64

import fetch_infra_files as fif


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeGH:
    def __init__(self, tree, contents, truncated=False):
        self.tree = tree
        self.contents = contents
        self.truncated = truncated
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        url = args[-1]
        if "/git/trees/" in url:
            return {"tree": self.tree, "truncated": self.truncated}
        for path, payload in self.contents.items():
            if f"/contents/{path}?" in url:
                return payload
        raise RuntimeError(f"not found: {url}")


def test_glob_matches_double_star():
    assert fif.path_matches("envs/prod/api/deploy.yaml", ["envs/prod/**"])
    assert fif.path_matches("modules/kong/main.tf", ["modules/kong/**"])
    assert not fif.path_matches("modules/redis/main.tf", ["modules/kong/**"])
    assert fif.path_matches("a/b.tf", ["**/*.tf"])


def test_fetches_only_allowlisted_files():
    gh = FakeGH(
        tree=[
            {"path": "envs/prod/api/deploy.yaml", "type": "blob", "size": 20},
            {"path": "secrets/private.txt", "type": "blob", "size": 20},
        ],
        contents={
            "envs/prod/api/deploy.yaml": {
                "encoding": "base64",
                "content": _b64("kind: Deployment"),
            }
        },
    )
    src = {"repo": "o/infra", "resolved_sha": "sha1", "allow": ["envs/prod/**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    assert list(result["files"]) == ["envs/prod/api/deploy.yaml"]
    assert result["fetched_paths"] == ["envs/prod/api/deploy.yaml"]


def test_max_files_caps_and_records_overflow():
    tree = [{"path": f"envs/prod/f{i}.yaml", "type": "blob", "size": 5} for i in range(3)]
    contents = {
        f"envs/prod/f{i}.yaml": {"encoding": "base64", "content": _b64("x")}
        for i in range(3)
    }
    gh = FakeGH(tree=tree, contents=contents)
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["envs/prod/**"]}
    result = fif.fetch_infra_files(src, max_files=2, max_file_bytes=1024, runner=gh)
    assert len(result["files"]) == 2
    assert any(s["reason"] == "over_max_files" for s in result["skipped"])


def test_too_large_and_binary_skipped():
    gh = FakeGH(
        tree=[
            {"path": "envs/big.yaml", "type": "blob", "size": 99999},
            {"path": "envs/bin.bin", "type": "blob", "size": 5},
        ],
        contents={
            "envs/bin.bin": {
                "encoding": "base64",
                "content": base64.b64encode(b"a\x00b").decode("ascii"),
            }
        },
    )
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["envs/**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    reasons = {s["path"]: s["reason"] for s in result["skipped"]}
    assert reasons["envs/big.yaml"] == "too_large"
    assert reasons["envs/bin.bin"] == "binary"
    assert result["files"] == {}


def test_truncated_tree_flagged():
    gh = FakeGH(tree=[], contents={}, truncated=True)
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    assert result["truncated"] is True

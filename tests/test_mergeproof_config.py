"""Tests for the zero-dependency MergeProof config parser."""

from __future__ import annotations

import pytest

import mergeproof_config as mc

YAML_OK = """\
version: 1
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/aegislocal-api/**
      - modules/kong/**
limits:
  max_files: 50
  max_file_bytes: 1024
"""

JSON_OK = """\
{"version": 1, "service": "aegislocal-api",
 "architecture_sources": [{"repo": "acme/infra", "ref": "main",
   "allow": ["envs/prod/aegislocal-api/**", "modules/kong/**"]}],
 "limits": {"max_files": 50, "max_file_bytes": 1024}}
"""


def test_yaml_and_json_parse_to_same_structure():
    from_yaml = mc.load_config(YAML_OK, fmt="yaml")
    from_json = mc.load_config(JSON_OK, fmt="json")
    assert from_yaml == from_json
    assert from_yaml["service"] == "aegislocal-api"
    assert from_yaml["architecture_sources"][0]["repo"] == "acme/infra"
    assert from_yaml["architecture_sources"][0]["allow"] == [
        "envs/prod/aegislocal-api/**",
        "modules/kong/**",
    ]
    assert from_yaml["limits"] == {"max_files": 50, "max_file_bytes": 1024}


def test_defaults_applied_when_limits_absent():
    text = """\
service: s
architecture_sources:
  - repo: o/r
    allow:
      - a/**
"""
    cfg = mc.load_config(text, fmt="yaml")
    assert cfg["limits"] == {"max_files": 200, "max_file_bytes": 262144}
    assert cfg["architecture_sources"][0]["ref"] == "main"


def test_duplicate_keys_rejected():
    text = "service: a\nservice: b\narchitecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="[Dd]uplicate"):
        mc.load_config(text, fmt="yaml")


@pytest.mark.parametrize(
    "value",
    [
        "service: &anchor x",
        "service: *alias",
        "service: {a: 1}",
        "service: |\n  multi\n  line",
    ],
)
def test_unsupported_yaml_syntax_rejected(value):
    text = value + "\narchitecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="Unsupported mergeproof.yaml syntax"):
        mc.load_config(text, fmt="yaml")


def test_missing_service_rejected():
    text = "architecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="service"):
        mc.load_config(text, fmt="yaml")


def test_empty_or_bad_sources_rejected():
    with pytest.raises(mc.MergeProofConfigError, match="architecture_sources"):
        mc.load_config("service: s\narchitecture_sources:\n", fmt="yaml")
    with pytest.raises(mc.MergeProofConfigError, match="allow"):
        mc.load_config("service: s\narchitecture_sources:\n  - repo: o/r\n", fmt="yaml")


def test_strip_comment_keeps_escaped_quote_in_string():
    # An escaped quote inside a double-quoted value must not end the string, so a
    # '#' that follows inside the quotes is content, not a comment delimiter.
    cfg = mc.parse_yaml_subset('key: "a \\" # b"\n')
    assert cfg["key"] == 'a \\" # b'

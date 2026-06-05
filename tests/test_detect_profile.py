from __future__ import annotations

import json

from detect_profile import detect


def _names(result):
    return [c["name"] for c in result["candidate_checks"]]


def test_python_pyproject_with_optional_tools(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['ruff', 'mypy']\n"
    )
    (tmp_path / "tests").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert result["confidence"] == "high"
    assert "pyproject.toml" in result["reasons"]
    assert _names(result) == ["tests", "lint", "typecheck"]
    tests_check = result["candidate_checks"][0]
    assert tests_check["command"] == "pytest"
    assert tests_check["required"] is True
    assert result["candidate_checks"][2]["required"] is False  # typecheck optional


def test_python_without_optional_tools_only_pytest(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert _names(result) == ["tests"]


def test_node_only_emits_existing_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"test": "jest", "lint": "eslint ."}
    }))
    result = detect(tmp_path)
    assert result["stack"] == "node"
    assert _names(result) == ["tests", "lint"]  # no typecheck script -> absent
    assert result["candidate_checks"][0]["command"] == "npm test"
    assert result["candidate_checks"][1]["command"] == "npm run lint"


def test_rust_cargo(tmp_path):
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")
    result = detect(tmp_path)
    assert result["stack"] == "rust"
    assert result["candidate_checks"][0]["command"] == "cargo test"
    assert result["candidate_checks"][1]["required"] is False  # clippy optional


def test_go_mod(tmp_path):
    (tmp_path / "go.mod").write_text("module x\n")
    result = detect(tmp_path)
    assert result["stack"] == "go"
    assert result["candidate_checks"][0]["command"] == "go test ./..."


def test_unknown_stack_low_confidence_no_checks(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    result = detect(tmp_path)
    assert result["stack"] == "unknown"
    assert result["confidence"] == "low"
    assert result["candidate_checks"] == []


def test_node_malformed_package_json_is_safe(tmp_path):
    (tmp_path / "package.json").write_text("{ this is not valid json")
    result = detect(tmp_path)
    assert result["stack"] == "node"
    assert result["candidate_checks"] == []


def test_strong_marker_beats_bare_tests_dir(tmp_path):
    # A node repo that also has a tests/ dir must detect as node, not python:
    # tests/ is a weak signal common to many languages (Gemini review #30).
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    (tmp_path / "tests").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "node"


def test_bare_tests_dir_falls_back_to_python(tmp_path):
    # With no strong marker, a tests/ dir still falls back to python.
    (tmp_path / "tests").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert result["confidence"] == "medium"

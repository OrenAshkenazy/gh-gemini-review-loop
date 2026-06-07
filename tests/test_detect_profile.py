from __future__ import annotations

import json

from detect_profile import build_presets, detect, discover_git_tree_checks, main, parse_justfile_recipes
import subprocess


def _names(result):
    return [c["name"] for c in result["candidate_checks"]]


def _labels(presets):
    return [p["label"] for p in presets]


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


def test_node_non_dict_scripts_is_safe(tmp_path):
    # A hand-edited package.json where "scripts" is not an object must not
    # crash the `script_key in scripts` membership check with a TypeError.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": 5}))
    result = detect(tmp_path)
    assert result["stack"] == "node"
    assert result["candidate_checks"] == []


def test_python_pyproject_invalid_utf8_is_safe(tmp_path):
    # Invalid UTF-8 bytes -> read_text(encoding="utf-8") would raise
    # UnicodeDecodeError (a ValueError subclass, not OSError). Must degrade,
    # not crash. errors="replace" makes the read succeed; the garbage bytes
    # carry no tool names, so only the base "tests" check remains.
    (tmp_path / "pyproject.toml").write_bytes(b"\xff\xfe\x80\x81\x82 binary junk")
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert _names(result) == ["tests"]


def test_node_package_json_invalid_utf8_is_safe(tmp_path):
    (tmp_path / "package.json").write_bytes(b"\xff\xfe not utf8")
    result = detect(tmp_path)
    assert result["stack"] == "node"
    assert result["candidate_checks"] == []


def test_python_ruff_substring_does_not_false_positive(tmp_path):
    # "gruff" contains "ruff" but is not the ruff tool; word-boundary match
    # must not add a lint check.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\ngruff = "^1.0"\n'
    )
    assert "lint" not in _names(detect(tmp_path))


def test_python_ruff_word_is_detected(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 88\n"
    )
    assert "lint" in _names(detect(tmp_path))


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


def test_directory_named_like_marker_is_ignored(tmp_path):
    # A *directory* named pyproject.toml must not be treated as the file marker
    # (would crash read_text with IsADirectoryError) nor classify as python.
    (tmp_path / "pyproject.toml").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "unknown"


def test_directory_named_package_json_is_not_node(tmp_path):
    (tmp_path / "package.json").mkdir()
    result = detect(tmp_path)
    assert result["stack"] == "unknown"


def test_python_read_error_degrades_without_crash(tmp_path, monkeypatch):
    # is_file() True but read_text raises OSError (permissions/broken symlink):
    # detection must not crash; it degrades to the base pytest check.
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies=['ruff']\n")
    import detect_profile

    def boom(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(detect_profile.Path, "read_text", boom)
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert _names(result) == ["tests"]  # ruff/mypy skipped: read failed safely


def test_build_presets_empty_candidates_returns_no_menu():
    assert build_presets([]) == []


def test_build_presets_single_check_has_no_narrower_option():
    candidates = [{"name": "tests", "command": "cargo test", "required": True}]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — cargo test",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_multi_check_with_tests_offers_tests_only():
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "lint", "command": "ruff check .", "required": True},
    ]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — pytest + ruff check .",
        "Tests only — pytest",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_multi_check_without_tests_offers_first_check_only():
    candidates = [
        {"name": "lint", "command": "npm run lint", "required": True},
        {"name": "typecheck", "command": "npm run typecheck", "required": False},
    ]
    presets = build_presets(candidates)
    assert _labels(presets) == [
        "All detected — npm run lint + npm run typecheck",
        "First check only — npm run lint",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_forces_required_true_on_all_gating_checks():
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "typecheck", "command": "mypy .", "required": False},
    ]
    presets = build_presets(candidates)
    all_detected = presets[0]
    assert all_detected["source"] == "confirmed"
    assert all(c["required"] is True for c in all_detected["checks"])


def test_build_presets_sources_and_customize_flag():
    candidates = [
        {"name": "tests", "command": "pytest", "required": True},
        {"name": "lint", "command": "ruff check .", "required": True},
    ]
    by_label = {p["label"]: p for p in build_presets(candidates)}
    assert by_label["All detected — pytest + ruff check ."]["source"] == "confirmed"
    assert by_label["Tests only — pytest"]["source"] == "customized"
    skip = by_label["Skip — use ad-hoc verification"]
    assert skip["source"] == "skipped" and skip["checks"] == []
    customize = by_label["Customize manually"]
    assert customize["customize"] is True and customize["source"] is None


def test_main_output_includes_presets_key(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\ndependencies = ['ruff']\n"
    )
    (tmp_path / "tests").mkdir()
    rc = main(["detect_profile.py", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stack"] == "python"
    assert payload["candidate_checks"]
    assert [p["label"] for p in payload["presets"]] == [
        "All detected — pytest + ruff check .",
        "Tests only — pytest",
        "Skip — use ad-hoc verification",
        "Customize manually",
    ]


def test_build_presets_preserves_working_directory():
    candidates = [
        {"name": "backend", "command": "pytest",
         "working_directory": "test-backend", "required": True},
        {"name": "client", "command": "npm test",
         "working_directory": "familia-ai/client", "required": True},
    ]
    presets = build_presets(candidates)
    all_detected = presets[0]
    assert all_detected["source"] == "confirmed"
    cwds = {c["name"]: c.get("working_directory") for c in all_detected["checks"]}
    assert cwds == {"backend": "test-backend", "client": "familia-ai/client"}
    # every persisted check is still forced required
    assert all(c["required"] is True for c in all_detected["checks"])


def _write_justfile(tmp_path, body, name="justfile"):
    (tmp_path / name).write_text(body)


def test_justfile_emits_matching_runnable_recipes(tmp_path):
    _write_justfile(tmp_path, (
        "build:\n\tcargo build\n\n"
        "test-backend:\n\tcd test-backend && pytest\n\n"
        "test-client:\n\tcd client && npm test\n\n"
        "lint:\n\truff check .\n\n"
        "deploy:\n\t./deploy.sh\n"
    ))
    checks = parse_justfile_recipes(tmp_path)
    names = [c["name"] for c in checks]
    assert names == ["test-backend", "test-client", "lint"]
    assert all(c["command"] == f"just {c['name']}" for c in checks)
    assert all(c["working_directory"] == "." for c in checks)
    assert all(c["required"] is True for c in checks)


def test_justfile_parameter_guard(tmp_path):
    _write_justfile(tmp_path, (
        "test:\n\tpytest\n\n"                  # no params -> emit
        'test-default target="all":\n\tpytest {{target}}\n\n'  # default -> emit
        "test-target target:\n\tpytest {{target}}\n\n"          # required -> skip
        "test-many +paths:\n\tpytest {{paths}}\n\n"             # +variadic -> skip
        "test-opt *paths:\n\tpytest {{paths}}\n"                # *variadic -> emit
    ))
    names = [c["name"] for c in parse_justfile_recipes(tmp_path)]
    assert names == ["test", "test-default", "test-opt"]


def test_justfile_ignores_assignments_and_comments(tmp_path):
    _write_justfile(tmp_path, (
        "# test: this is a comment, not a recipe\n"
        'export TEST_VAR := "x"\n'
        "checkpoint:\n\techo not-a-check\n"   # name doesn't match 'check' exactly
        "check:\n\tcargo check\n"
    ))
    names = [c["name"] for c in parse_justfile_recipes(tmp_path)]
    assert names == ["check"]


def test_justfile_filename_variants(tmp_path):
    _write_justfile(tmp_path, "test:\n\tpytest\n", name="Justfile")
    assert [c["name"] for c in parse_justfile_recipes(tmp_path)] == ["test"]


def test_no_justfile_returns_empty(tmp_path):
    assert parse_justfile_recipes(tmp_path) == []


def _git_repo(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel in files:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_git_tree_maps_test_dirs_to_nearest_marker(tmp_path):
    _git_repo(tmp_path, [
        "familia-ai/client/package.json",
        "familia-ai/client/tests/test_app.js",
        "familia-ai/scraper-svc/package.json",
        "familia-ai/scraper-svc/src/__tests__/scrape.test.js",
        "test-backend/pyproject.toml",
        "test-backend/tests/test_api.py",
    ])
    checks = discover_git_tree_checks(tmp_path)
    by_cwd = {c["working_directory"]: c for c in checks}
    assert by_cwd["familia-ai/client"]["command"] == "npm test"
    assert by_cwd["familia-ai/scraper-svc"]["command"] == "npm test"
    assert by_cwd["test-backend"]["command"] == "pytest"
    assert all(c["required"] is True for c in checks)


def test_git_tree_dedups_one_check_per_marker_dir(tmp_path):
    _git_repo(tmp_path, [
        "svc/pyproject.toml",
        "svc/tests/test_a.py",
        "svc/spec/test_b.py",  # second test dir under the same marker
    ])
    checks = discover_git_tree_checks(tmp_path)
    cwds = [c["working_directory"] for c in checks]
    assert cwds == ["svc"]


def test_git_tree_skips_test_dir_without_marker(tmp_path):
    _git_repo(tmp_path, [
        "orphan/tests/test_x.py",  # no package marker anywhere up-tree
    ])
    assert discover_git_tree_checks(tmp_path) == []


def test_git_tree_empty_outside_git_repo(tmp_path):
    # no `git init` -> ls-files fails -> empty
    (tmp_path / "tests").mkdir()
    assert discover_git_tree_checks(tmp_path) == []


def test_detect_prefers_justfile_over_git_tree(tmp_path):
    _git_repo(tmp_path, [
        "svc/pyproject.toml",
        "svc/tests/test_a.py",
    ])
    (tmp_path / "justfile").write_text("test:\n\tpytest\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    result = detect(tmp_path)
    assert result["stack"] == "justfile"
    assert [c["name"] for c in result["candidate_checks"]] == ["test"]
    assert result["candidate_checks"][0]["command"] == "just test"


def test_detect_uses_git_tree_when_no_matching_recipes(tmp_path):
    _git_repo(tmp_path, [
        "svc/pyproject.toml",
        "svc/tests/test_a.py",
    ])
    # justfile present but its only recipe doesn't match a verify pattern
    (tmp_path / "justfile").write_text("build:\n\tcargo build\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    result = detect(tmp_path)
    assert result["stack"] == "monorepo"
    assert [c["working_directory"] for c in result["candidate_checks"]] == ["svc"]
    assert result["candidate_checks"][0]["command"] == "pytest"


def test_detect_falls_back_to_root_marker(tmp_path):
    # not a git repo, no justfile -> existing root single-stack detection
    (tmp_path / "pyproject.toml").write_text("[project]\ndependencies = ['ruff']\n")
    result = detect(tmp_path)
    assert result["stack"] == "python"
    assert [c["name"] for c in result["candidate_checks"]] == ["tests", "lint"]

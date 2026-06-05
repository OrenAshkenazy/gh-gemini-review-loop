from __future__ import annotations

from run_profile import run_profile


def _profile(checks, working_directory=".", timeout_seconds=300):
    return {
        "checks": checks,
        "working_directory": working_directory,
        "timeout_seconds": timeout_seconds,
    }


def test_all_required_pass_gate_passes(tmp_path):
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    assert result.checks[0].status == "passed"


def test_required_failure_fails_gate(tmp_path):
    prof = _profile([
        {"name": "boom", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.failed_required == ["boom"]


def test_optional_failure_does_not_fail_gate(tmp_path):
    prof = _profile([
        {"name": "tests", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
        {"name": "types", "command": "python3 -c \"import sys; sys.exit(1)\"",
         "required": False},
    ])
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"
    assert result.failed_required == []
    statuses = {c.name: c.status for c in result.checks}
    assert statuses == {"tests": "passed", "types": "failed"}


def test_required_timeout_fails_gate(tmp_path):
    prof = _profile(
        [{"name": "slow", "command": "python3 -c \"import time; time.sleep(5)\"",
          "required": True}],
        timeout_seconds=1,
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "failed"
    assert result.checks[0].status == "timeout"
    assert result.failed_required == ["slow"]


def test_working_directory_is_honored(tmp_path):
    sub = tmp_path / "backend"
    sub.mkdir()
    (sub / "marker.txt").write_text("x")
    prof = _profile(
        [{"name": "find", "command": "python3 -c \"import os,sys; sys.exit(0 "
          "if os.path.exists('marker.txt') else 1)\"", "required": True}],
        working_directory="backend",
    )
    result = run_profile(prof, tmp_path)
    assert result.verification == "passed"


def test_to_details_dict_is_json_serializable(tmp_path):
    import json
    prof = _profile([
        {"name": "ok", "command": "python3 -c \"import sys; sys.exit(0)\"",
         "required": True},
    ])
    result = run_profile(prof, tmp_path)
    json.dumps(result.to_details())  # must not raise
    assert result.to_details()["verification"] == "passed"

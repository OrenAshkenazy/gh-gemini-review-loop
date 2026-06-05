from __future__ import annotations

import json

import judge
from detect_profile import detect
from run_profile import main as run_profile_main
from run_profile import run_profile

# A trivially-passing command, so tests are hermetic (don't actually run pytest).
_OK = 'python3 -c "import sys; sys.exit(0)"'


def test_detect_save_run_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()  # python marker -> detect() yields a 'tests' check

    det = detect(repo)
    assert det["stack"] == "python"
    # Keep the names/required flags from detection; swap commands to be hermetic.
    checks = [
        {"name": c["name"], "command": _OK, "required": c["required"]}
        for c in det["candidate_checks"]
    ]
    judge.save_profile("o/r", source="confirmed", checks=checks,
                       detected_stack=det["stack"])

    prof = judge.get_profile("o/r")
    # check-object keys survive detection -> save -> get round trip
    assert all({"name", "command", "required"} <= set(c.keys())
               for c in prof["checks"])

    result = run_profile(prof, repo)
    assert result.verification == "passed"


def test_cli_runs_saved_profile(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    judge.save_profile("o/r", source="confirmed",
                       checks=[{"name": "ok", "command": _OK, "required": True}])
    rc = run_profile_main(["run_profile.py", "o/r", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["verification"] == "passed"


def test_cli_skipped_profile_exits_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    judge.save_profile("o/r", source="skipped")
    rc = run_profile_main(["run_profile.py", "o/r", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["verification"] == "skipped"


def test_cli_failing_required_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    judge.save_profile("o/r", source="confirmed", checks=[
        {"name": "boom", "command": 'python3 -c "import sys; sys.exit(1)"',
         "required": True},
    ])
    rc = run_profile_main(["run_profile.py", "o/r", str(tmp_path)])
    capsys.readouterr()
    assert rc == 1


def test_save_profile_upgrades_v1_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    path = tmp_path / "preferences.json"
    path.write_text(json.dumps({"schema_version": 1, "judge_mode": "off"}))
    judge.save_profile("o/r", source="confirmed", checks=[])
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == 2
    assert saved["judge_mode"] == "off"  # existing field preserved

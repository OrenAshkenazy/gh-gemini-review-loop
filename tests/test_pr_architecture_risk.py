"""Tests for mapping PR changed files to production risks."""

from __future__ import annotations

import json

import pr_architecture_risk as par


PUBLIC_CTX = {
    "service_name": "aegislocal-api",
    "exposure": "public",
    "datastores": ["postgresql"],
    "queues": ["sqs:scan-events"],
}


def test_public_api_change_maps_high_public_api_risk():
    result = par.assess(PUBLIC_CTX, ["core/api/routes.py"])

    risks = {r["surface"]: r for r in result["production_risks"]}
    assert risks["public_api"]["severity"] == "high"
    assert risks["public_api"]["human_decision_required"] is True
    assert "core/api/routes.py" in risks["public_api"]["files"]


def test_auth_file_maps_high_auth_risk():
    result = par.assess(PUBLIC_CTX, ["core/user_utils.py"])

    risks = {r["surface"]: r for r in result["production_risks"]}
    assert risks["auth_security"]["severity"] == "high"


def test_worker_file_with_queue_maps_async_risk():
    result = par.assess(PUBLIC_CTX, ["core/workers/scan_worker.py"])

    risks = {r["surface"]: r for r in result["production_risks"]}
    assert risks["async_processing"]["severity"] == "medium"
    assert risks["async_processing"]["human_decision_required"] is True


def test_worker_file_without_queue_does_not_map_async_risk():
    ctx = {"exposure": "internal", "queues": [], "datastores": []}
    result = par.assess(ctx, ["core/workers/scan_worker.py"])

    surfaces = {r["surface"] for r in result["production_risks"]}
    assert "async_processing" not in surfaces


def test_infra_file_maps_infrastructure_risk():
    result = par.assess(PUBLIC_CTX, ["terraform/sqs.tf"])

    risks = {r["surface"]: r for r in result["production_risks"]}
    assert risks["infrastructure"]["severity"] in {"medium", "high"}


def test_database_migration_maps_database_risk():
    result = par.assess(PUBLIC_CTX, ["core/db/migrations/0007_add_index.py"])

    risks = {r["surface"]: r for r in result["production_risks"]}
    assert "database_behavior" in risks


def test_no_matching_files_returns_empty_risks():
    result = par.assess(PUBLIC_CTX, ["README.md", "docs/guide.md"])

    assert result["production_risks"] == []
    assert result["summary"]["highest_severity"] == "none"
    assert result["summary"]["human_decision_required"] is False
    assert result["summary"]["risk_count"] == 0


def test_summary_reports_highest_severity_and_decision():
    result = par.assess(
        PUBLIC_CTX,
        ["core/api/routes.py", "core/workers/scan_worker.py"],
    )

    assert result["summary"]["highest_severity"] == "high"
    assert result["summary"]["human_decision_required"] is True
    assert result["summary"]["risk_count"] == 2


def test_changed_files_from_local_file(tmp_path):
    listing = tmp_path / "changed.txt"
    listing.write_text("core/api/routes.py\n\ncore/user_utils.py\n", encoding="utf-8")

    files = par.read_changed_files(listing)

    assert files == ["core/api/routes.py", "core/user_utils.py"]


def test_changed_files_from_pr_uses_injected_runner():
    captured = {}

    def fake_runner(args):
        captured["args"] = args
        return [{"filename": "core/api/routes.py"}, {"filename": "README.md"}]

    files = par.fetch_pr_changed_files("OrenAshkenazy/AegisLocal", 11, runner=fake_runner)

    assert files == ["core/api/routes.py", "README.md"]
    assert "repos/OrenAshkenazy/AegisLocal/pulls/11/files" in captured["args"]


def test_json_stdout_is_parseable_and_has_no_ansi(tmp_path, capsys):
    ctx_path = tmp_path / "ctx.json"
    ctx_path.write_text(json.dumps(PUBLIC_CTX), encoding="utf-8")
    changed = tmp_path / "changed.txt"
    changed.write_text("core/api/routes.py\n", encoding="utf-8")

    rc = par.main(
        [
            "--architecture-context",
            str(ctx_path),
            "--changed-files",
            str(changed),
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert "\033[" not in captured.out
    assert "[loop]" not in captured.out
    payload = json.loads(captured.out)
    assert payload["summary"]["highest_severity"] == "high"
    assert captured.err == ""

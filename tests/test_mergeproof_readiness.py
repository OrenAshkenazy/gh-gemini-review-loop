"""Tests for the readiness phase orchestrator."""

from __future__ import annotations

import base64
import json

import pytest

import mergeproof_readiness as mr


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""

CONFIG_WITH_CAPABILITIES = """\
service: payments-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/payments-api/**
      - terraform/payments-api/**
capabilities:
  - type: worker_deployment
    template: capabilities/worker_deployment.yaml
    approver: "@platform-runtime"
"""

WORKER_PACK = """\
capability: worker_deployment
inputs:
  worker_name: required
  service: required
generates:
  - worker_deployment
  - terraform_worker
checks:
  - helm_template
  - terraform_validate
approval:
  required_from:
    - platform-runtime
template_map:
  worker_deployment:
    template: templates/worker.tmpl
    output: envs/prod/payments-api/${worker_name}.yaml
  terraform_worker:
    template: templates/worker.tf.tmpl
    output: terraform/payments-api/workers/${worker_name}.tf
"""

WORKER_TEMPLATE = "worker: ${worker_name}\nservice: ${service}\n"
TERRAFORM_WORKER_TEMPLATE = 'resource "kubernetes_deployment" "${worker_name}" {}\n'

INFRA = {
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
}

LOOP_SUMMARY = {
    "pr_url": "https://github.com/acme/app/pull/7",
    "fixed_count": 7,
    "false_positives_skipped": 1,
    "verification": "passed",
    "verification_command": "uv run pytest",
    "rereview": "completed",
    "cycles_used": 2,
    "cycles_total": 3,
}


class FakeGH:
    def __init__(self, changed, has_config=True, infra=None, config_text=CONFIG_YAML, app_files=None):
        self.changed = changed
        self.has_config = has_config
        self.infra = infra or INFRA
        self.config_text = config_text
        self.app_files = app_files or {}
        self.infra_unreachable = False

    def __call__(self, args):
        url = next((arg for arg in args if isinstance(arg, str) and arg.startswith("repos/")), args[-1])
        if "/pulls/7/files" in url:
            rows = []
            for entry in self.changed:
                if isinstance(entry, dict):
                    rows.append({"filename": entry["path"], "status": entry.get("status", "modified")})
                else:
                    rows.append({"filename": entry, "status": "modified"})
            return rows
        if "/pulls/7" in url:
            return {"base": {"ref": "main", "sha": "base"}}
        if "/contents/mergeproof.yaml" in url:
            if not self.has_config:
                raise RuntimeError("404")
            return {"encoding": "base64", "content": _b64(self.config_text)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "repos/acme/app/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            if path in self.app_files:
                return {"encoding": "base64", "content": _b64(self.app_files[path])}
            raise RuntimeError(f"404: {path}")
        if "/commits/" in url:
            if self.infra_unreachable:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            return {"sha": "isha"}
        if "/git/trees/" in url:
            return {
                "truncated": False,
                "tree": [
                    {"path": p, "type": "blob", "size": len(t)}
                    for p, t in self.infra.items()
                ],
            }
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra[path])}
        raise RuntimeError(f"unexpected {url}")


def test_all_infra_sources_unreadable_fails_with_clear_error():
    gh = FakeGH(changed=["core/api/routes.py"])
    gh.infra_unreachable = True
    with pytest.raises(RuntimeError) as exc:
        mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    message = str(exc.value)
    assert "acme/infra" in message
    assert "404" in message
    assert "no" in message.lower() and "infra source" in message.lower()


def test_terminal_phase_renders_when_config_exists():
    gh = FakeGH(changed=["core/api/routes.py"])
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"
    assert "## MergeProof PR Readiness" in result["markdown"]
    assert "<!-- mergeproof-pr-readiness -->" in result["markdown"]


def test_missing_config_skips_without_failing(capsys):
    gh = FakeGH(changed=["core/api/routes.py"], has_config=False)
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "skipped"
    err = capsys.readouterr().err
    assert "[mergeproof] readiness skipped" in err
    assert "Reason: mergeproof.yaml not found" in err


def test_verification_failed_still_renders_with_failed_status():
    gh = FakeGH(changed=["core/api/routes.py"])
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    result = mr.run_readiness("acme/app", 7, summary, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"


def test_load_latest_run_summary_from_recorded_runs_jsonl(tmp_path):
    runs = tmp_path / "runs.jsonl"
    runs.write_text(
        json.dumps(
            {
                "repo": "acme/app",
                "pr": 7,
                "fixed_count": 4,
                "verification": "failed",
                "verification_details": {"checks": [{"command": "uv run pytest"}]},
                "outcome": "verification_failed",
                "judge": {"verdicts": {"false_positive": 2}},
                "cycles_used": 1,
                "cycle_cap": 3,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    summary = mr.load_latest_run_summary(runs, "acme/app", 7)
    assert summary["verification"] == "failed"
    assert summary["verification_command"] == "uv run pytest"
    assert summary["false_positives_skipped"] == 2
    assert summary["pr_url"] == "https://github.com/acme/app/pull/7"


def test_load_loop_summary_falls_back_to_pr_body_metrics(tmp_path):
    runs = tmp_path / "missing-runs.jsonl"
    body = """\
## Demo PR

### CR loop metrics

| metric | value |
|---|---|
| findings fixed | 5 |
| false positives skipped | 1 |
| verification | passed |
| verification command | pytest |
| re-review | completed |
| cycles used | 2 / 3 |
"""

    def runner(args):
        assert args == ["api", "repos/acme/app/pulls/7"]
        return {"body": body}

    summary = mr.load_loop_summary_for_pr(runs, "acme/app", 7, runner=runner)

    assert summary["fixed_count"] == 5
    assert summary["false_positives_skipped"] == 1
    assert summary["verification"] == "passed"
    assert summary["verification_command"] == "pytest"
    assert summary["cycles_used"] == 2
    assert summary["cycles_total"] == 3


def test_readiness_runs_from_recorded_terminal_state():
    gh = FakeGH(changed=["core/api/routes.py"])
    terminal_summary = {
        "pr_url": "https://github.com/acme/app/pull/7",
        "fixed_count": 4,
        "verification": "failed",
        "verification_command": "uv run pytest",
    }
    result = mr.run_readiness("acme/app", 7, terminal_summary, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"


def test_stage_infra_creates_pr_and_links_it_in_readiness():
    gh = FakeGH(
        changed=[{"path": "app/workers/refund_worker.py", "status": "added"}],
        config_text=CONFIG_WITH_CAPABILITIES,
        app_files={
            "capabilities/worker_deployment.yaml": WORKER_PACK,
            "capabilities/templates/worker.tmpl": WORKER_TEMPLATE,
            "capabilities/templates/worker.tf.tmpl": TERRAFORM_WORKER_TEMPLATE,
        },
    )
    git_calls = []

    def git_runner(args):
        git_calls.append(args)
        return ""

    gh_calls = []

    def infra_gh_runner(args):
        gh_calls.append(args)
        if args[0].endswith("/pulls") and "--method" not in args:
            return []
        return {"number": 12, "html_url": "https://github.com/acme/infra/pull/12"}

    result = mr.run_readiness(
        "acme/app",
        7,
        LOOP_SUMMARY,
        runner=gh,
        stage_infra=True,
        create_infra_pr=True,
        infra_git_runner=git_runner,
        infra_github_runner=infra_gh_runner,
    )

    worker = result["readiness"]["obligations"][0]
    assert worker["type"] == "worker_deployment"
    assert worker["infra_pr"]["pushed"] is True
    assert worker["infra_pr"]["pull_request"]["html_url"] == "https://github.com/acme/infra/pull/12"
    assert worker["infra_pr"]["generated_files"] == [
        "envs/prod/payments-api/refund_worker.yaml",
        "terraform/payments-api/workers/refund_worker.tf",
    ]
    assert "https://github.com/acme/infra/pull/12" in result["markdown"]
    assert any("push" in " ".join(call) for call in git_calls)
    assert any("--method" in call and "POST" in call for call in gh_calls)

"""Tests for the static architecture context scanner."""

from __future__ import annotations

import json

import architecture_context as ac


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_empty_repo_returns_safe_unknown_output(tmp_path):
    result = ac.scan(tmp_path)

    assert result["service_name"] == "unknown"
    assert result["runtime"] == "unknown"
    assert result["exposure"] == "unknown"
    assert result["owners"] == []
    assert result["datastores"] == []
    assert result["queues"] == []
    assert result["architecture_files_found"] == []
    assert result["confidence"] == "low"


def test_detects_kubernetes_runtime(tmp_path):
    _write(
        tmp_path,
        "k8s/deployment.yaml",
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
    )

    result = ac.scan(tmp_path)

    assert result["runtime"] == "kubernetes"
    assert "k8s/deployment.yaml" in result["architecture_files_found"]


def test_detects_public_ingress(tmp_path):
    _write(
        tmp_path,
        "k8s/ingress.yaml",
        "kind: Ingress\nmetadata:\n  annotations:\n"
        "    kubernetes.io/ingress.class: alb\n    konghq.com/strip-path: 'true'\n",
    )

    result = ac.scan(tmp_path)

    assert result["exposure"] == "public"
    assert "alb" in result["ingress"]
    assert "kong" in result["ingress"]


def test_detects_owner_from_codeowners(tmp_path):
    _write(tmp_path, "CODEOWNERS", "# comment\n*  @acme/platform\n")

    result = ac.scan(tmp_path)

    assert "platform" in result["owners"]


def test_detects_owner_from_catalog_info(tmp_path):
    _write(
        tmp_path,
        "catalog-info.yaml",
        "apiVersion: backstage.io/v1alpha1\nkind: Component\n"
        "metadata:\n  name: aegislocal-api\nspec:\n  owner: platform\n  type: service\n",
    )

    result = ac.scan(tmp_path)

    assert result["service_name"] == "aegislocal-api"
    assert "platform" in result["owners"]
    assert result["deployment_type"] == "service"


def test_detects_sqs_queue_from_terraform(tmp_path):
    _write(
        tmp_path,
        "terraform/sqs.tf",
        'resource "aws_sqs_queue" "scan_events" {\n  name = "scan-events"\n}\n',
    )

    result = ac.scan(tmp_path)

    assert "sqs:scan-events" in result["queues"]
    assert "terraform/sqs.tf" in result["architecture_files_found"]


def test_detects_datastore_and_env_hints(tmp_path):
    _write(
        tmp_path,
        "docker-compose.yml",
        "services:\n  db:\n    image: postgres:16\n  cache:\n    image: redis:7\n"
        "  api:\n    environment:\n      - DATABASE_URL=postgres://x\n"
        "      - OPENAI_API_KEY=sk-test\n",
    )

    result = ac.scan(tmp_path)

    assert "postgresql" in result["datastores"]
    assert "redis" in result["datastores"]
    assert "DATABASE_URL" in result["secrets_or_env"]
    assert "OPENAI_API_KEY" in result["secrets_or_env"]
    assert "openai_api" in result["external_dependencies"]


def test_json_stdout_is_parseable_and_has_no_ansi(tmp_path, capsys):
    _write(tmp_path, "k8s/deployment.yaml", "kind: Deployment\n")

    rc = ac.main(["--repo", str(tmp_path), "--json"])

    captured = capsys.readouterr()
    assert rc == 0
    assert "\033[" not in captured.out
    assert "[loop]" not in captured.out
    payload = json.loads(captured.out)
    assert payload["runtime"] == "kubernetes"
    assert captured.err == ""

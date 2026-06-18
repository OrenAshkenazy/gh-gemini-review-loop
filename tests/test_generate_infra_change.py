import pytest
from generate_infra_change import generate_files, GenerateError


def _worker_pack():
    return {
        "generates": ["worker_deployment"],
        "template_map": {
            "worker_deployment": {
                "template": "templates/worker_deployment.tmpl",
                "output": "envs/prod/payments-api/workers/${worker_name}-deployment.yaml",
            }
        },
        "human_gate": None,
    }


def test_generate_substitutes_inputs(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text(
        "name: ${service}-${worker_name}\n", encoding="utf-8"
    )
    files = generate_files(
        _worker_pack(),
        inputs={"service": "payments-api", "worker_name": "refund_worker"},
        templates_root=tmp_path,
        allow=["envs/prod/payments-api/**"],
    )
    assert files == {
        "envs/prod/payments-api/workers/refund_worker-deployment.yaml": "name: payments-api-refund_worker\n"
    }


def test_missing_required_input_raises(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text("name: ${worker_name}\n", encoding="utf-8")
    with pytest.raises(GenerateError):
        generate_files(_worker_pack(), inputs={"service": "x"}, templates_root=tmp_path, allow=["envs/**"])


def test_human_gate_placeholder_left_unfilled(tmp_path):
    pack = {
        "generates": ["external_secret"],
        "template_map": {"external_secret": {"template": "templates/s.tmpl", "output": "envs/prod/s.yaml"}},
        "human_gate": "secret value provisioning",
    }
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "s.tmpl").write_text("ref: ${HUMAN_GATE:secret value provisioning}\n", encoding="utf-8")
    files = generate_files(pack, inputs={}, templates_root=tmp_path, allow=["envs/**"])
    assert "TODO-HUMAN: secret value provisioning" in files["envs/prod/s.yaml"]


def test_output_path_outside_allowlist_is_error(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text("x\n", encoding="utf-8")
    with pytest.raises(GenerateError):
        generate_files(_worker_pack(), inputs={"service": "x", "worker_name": "w"},
                       templates_root=tmp_path, allow=["helm/**"])


def test_generates_key_without_template_map_entry_raises(tmp_path):
    pack = {"generates": ["worker_deployment"], "template_map": {}, "human_gate": None}
    with pytest.raises(GenerateError):
        generate_files(pack, inputs={"service": "x", "worker_name": "w"}, templates_root=tmp_path, allow=["**"])


def test_multiple_generated_files(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "a.tmpl").write_text("a: ${worker_name}\n", encoding="utf-8")
    (tmp_path / "templates" / "b.tmpl").write_text("b: ${worker_name}\n", encoding="utf-8")
    pack = {
        "generates": ["worker_deployment", "helm_worker_values"],
        "template_map": {
            "worker_deployment": {"template": "templates/a.tmpl", "output": "envs/${worker_name}-a.yaml"},
            "helm_worker_values": {"template": "templates/b.tmpl", "output": "helm/${worker_name}-b.yaml"},
        },
        "human_gate": None,
    }
    files = generate_files(pack, inputs={"worker_name": "w"}, templates_root=tmp_path, allow=["envs/**", "helm/**"])
    assert files == {"envs/w-a.yaml": "a: w\n", "helm/w-b.yaml": "b: w\n"}

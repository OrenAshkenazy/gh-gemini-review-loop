#!/usr/bin/env python3
"""Best-effort static architecture context scanner.

Reads real, on-disk repository files (catalog-info, CODEOWNERS, Dockerfiles,
Kubernetes/Helm manifests, Terraform, CI workflows) and extracts a compact
architecture fact sheet. It performs *no* network calls and needs no cloud
credentials — everything is derived from static text already in the repo.

The scanner is deliberately tolerant: missing files, malformed YAML, or
unexpected shapes never crash. Unknown facts come back as ``"unknown"`` or
empty lists so downstream risk mapping can degrade gracefully.

PyYAML is intentionally not a dependency. The manifests we read have shallow,
predictable shapes, so line/regex heuristics are enough and keep the plugin
zero-dependency.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Files scanned at the repo root (exact names).
ROOT_FILES = (
    "catalog-info.yaml",
    "service.yaml",
    "ggrl.yaml",
    "CODEOWNERS",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
)

# Glob patterns scanned relative to the repo root.
GLOB_PATTERNS = (
    "k8s/**/*.yaml",
    "k8s/**/*.yml",
    "helm/**/values.yaml",
    "terraform/**/*.tf",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
)

# datastore keyword -> normalized name
_DATASTORE_HINTS = {
    "postgresql": "postgresql",
    "postgres": "postgresql",
    "redis": "redis",
    "mysql": "mysql",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "dynamodb": "dynamodb",
    "elasticsearch": "elasticsearch",
}

# external dependency keyword -> normalized name
_EXTERNAL_HINTS = {
    "openai": "openai_api",
    "anthropic": "anthropic_api",
    "stripe": "stripe_api",
    "twilio": "twilio_api",
    "sendgrid": "sendgrid_api",
    "external-payment-service": "external_payment_service",
    "payment-provider": "external_payment_service",
}

# environment / secret names worth surfacing.
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_URL", "_DSN", "_CREDENTIALS")
_SECRET_EXACT = {"DATABASE_URL", "REDIS_URL", "SECRET_KEY"}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def _collect_files(root: Path) -> list[tuple[str, str]]:
    """Return ``(relpath, text)`` for every architecture file found, sorted."""
    found: dict[str, str] = {}
    for name in ROOT_FILES:
        path = root / name
        if path.is_file():
            found[name] = _read_text(path)
    for pattern in GLOB_PATTERNS:
        for path in sorted(root.glob(pattern)):
            if path.is_file():
                rel = path.relative_to(root).as_posix()
                found[rel] = _read_text(path)
    return sorted(found.items())


def _yaml_scalar(text: str, key: str) -> str | None:
    """Return the first ``key: value`` scalar in *text*, or None."""
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*(['\"]?)([^\n#]+?)\1\s*$", text)
    if not match:
        return None
    value = match.group(2).strip()
    return value or None


def _detect_service_name(files: dict[str, str]) -> str:
    for name in ("catalog-info.yaml", "service.yaml", "ggrl.yaml"):
        text = files.get(name)
        if not text:
            continue
        value = _yaml_scalar(text, "name")
        if value:
            return value
    return "unknown"


def _detect_owners(files: dict[str, str]) -> list[str]:
    owners: list[str] = []

    def _add(token: str) -> None:
        token = token.strip().lstrip("@")
        if "/" in token:  # @org/team -> team
            token = token.split("/", 1)[1]
        if token and token not in owners:
            owners.append(token)

    codeowners = files.get("CODEOWNERS", "")
    for line in codeowners.splitlines():
        line = line.split("#", 1)[0].strip()  # drop inline comments
        if not line:
            continue
        for tok in line.split()[1:]:  # skip the path pattern
            _add(tok)

    for name in ("catalog-info.yaml", "service.yaml", "ggrl.yaml"):
        text = files.get(name)
        if not text:
            continue
        owner = _yaml_scalar(text, "owner")
        if owner:
            _add(owner)
    return owners


def _detect_runtime(files: dict[str, str], blob: str = "") -> str:
    rels = files.keys()
    if any(r.startswith("k8s/") or r.startswith("helm/") for r in rels):
        return "kubernetes"
    if re.search(r"(?m)^\s*kind:\s*(Deployment|Ingress|Service|CronJob|StatefulSet)\b", blob):
        return "kubernetes"
    if any(r.startswith("terraform/") for r in rels):
        return "terraform"
    if "Dockerfile" in files or "docker-compose.yml" in files or "docker-compose.yaml" in files:
        return "docker"
    return "unknown"


def _detect_deployment_type(files: dict[str, str], blob: str) -> str:
    for name in ("catalog-info.yaml", "service.yaml", "ggrl.yaml"):
        text = files.get(name)
        if text:
            value = _yaml_scalar(text, "type")
            if value:
                return value
    if re.search(r"\b(worker|consumer|job|cronjob)\b", blob, re.IGNORECASE):
        return "worker"
    return "service"


def _detect_ingress(blob: str) -> list[str]:
    ingress: list[str] = []
    lowered = blob.lower()
    if "alb" in lowered or "application load balancer" in lowered:
        ingress.append("alb")
    if "kong" in lowered:
        ingress.append("kong")
    if re.search(r"(?m)^\s*kind:\s*ingress\b", blob, re.IGNORECASE) or "ingress.class" in lowered:
        ingress.append("ingress")
    if "loadbalancer" in lowered.replace(" ", ""):
        ingress.append("load_balancer")
    return ingress


def _detect_exposure(blob: str, ingress: list[str]) -> str:
    lowered = blob.lower()
    if ingress or "type: loadbalancer" in lowered or re.search(r"(?m)^\s*kind:\s*ingress", blob, re.IGNORECASE):
        return "public"
    if "clusterip" in lowered.replace(" ", "") or "internal" in lowered:
        return "internal"
    return "unknown"


def _detect_datastores(blob: str) -> list[str]:
    lowered = blob.lower()
    found: list[str] = []
    for hint, normalized in _DATASTORE_HINTS.items():
        if hint in lowered and normalized not in found:
            found.append(normalized)
    return found


def _detect_queues(files: dict[str, str], blob: str) -> list[str]:
    queues: list[str] = []
    for rel, text in files.items():
        if not rel.endswith(".tf"):
            continue
        for match in re.finditer(
            r'resource\s+"aws_sqs_queue"\s+"([^"]+)"\s*\{(.*?)\}', text, re.DOTALL
        ):
            label, body = match.group(1), match.group(2)
            name_match = re.search(r'name\s*=\s*"([^"]+)"', body)
            queue_name = name_match.group(1) if name_match else label
            entry = f"sqs:{queue_name}"
            if entry not in queues:
                queues.append(entry)
    if not queues and re.search(r"\bsqs\b", blob, re.IGNORECASE):
        queues.append("sqs")
    if re.search(r"\b(arq|enqueue_job|RedisSettings)\b", blob) and re.search(
        r"\bredis\b", blob, re.IGNORECASE
    ):
        entry = "redis:arq"
        if entry not in queues:
            queues.append(entry)
    return queues


def _detect_external_dependencies(blob: str) -> list[str]:
    lowered = blob.lower()
    found: list[str] = []
    for hint, normalized in _EXTERNAL_HINTS.items():
        if hint in lowered and normalized not in found:
            found.append(normalized)
    return found


def _detect_secrets_or_env(blob: str) -> list[str]:
    found: list[str] = []
    for match in _ENV_RE.finditer(blob):
        name = match.group(1)
        is_secret = name in _SECRET_EXACT or name.endswith(_SECRET_SUFFIXES)
        # Credential/cert env names (banking, open-banking, mTLS) often lack a
        # standard secret suffix (e.g. *_CLIENT_ID, *_CERT_PATH) but are exactly
        # the surfaces a human must review for data-leak risk.
        is_credential = any(
            tok in name for tok in ("CLIENT_ID", "CLIENT_SECRET", "CLIENT_CERT", "CERT_PATH")
        )
        if (is_secret or is_credential) and name not in found:
            found.append(name)
    return found


def _detect_resource_limits(files: dict[str, str]) -> dict[str, str]:
    limits: dict[str, str] = {}
    for rel, text in files.items():
        if not (rel.startswith("k8s/") or rel.startswith("helm/")):
            continue
        block = re.search(r"limits:\s*\n((?:\s+\S.*\n?)+)", text)
        scope = block.group(1) if block else text
        if "cpu" not in limits:
            cpu = re.search(r"(?m)^\s*cpu:\s*['\"]?([^\s'\"]+)", scope)
            if cpu:
                limits["cpu"] = cpu.group(1)
        if "memory" not in limits:
            mem = re.search(r"(?m)^\s*memory:\s*['\"]?([^\s'\"]+)", scope)
            if mem:
                limits["memory"] = mem.group(1)
    return limits


def _detect_verification_commands(files: dict[str, str]) -> list[str]:
    commands: list[str] = []
    ggrl = files.get("ggrl.yaml", "")
    for match in re.finditer(r"(?m)^\s*-?\s*(?:command|run):\s*(['\"]?)([^\n#]+?)\1(?:\s*#.*)?$", ggrl):
        cmd = match.group(2).strip()
        if cmd and cmd not in commands:
            commands.append(cmd)
    test_re = re.compile(r"\b(pytest|uv run|go test|npm (?:run )?test|cargo test|jest)\b")
    for rel, text in files.items():
        if not rel.startswith(".github/workflows/"):
            continue
        for match in re.finditer(r"(?m)^\s*run:\s*(['\"]?)([^\n#]+?)\1(?:\s*#.*)?$", text):
            cmd = match.group(2).strip()
            if test_re.search(cmd) and cmd not in commands:
                commands.append(cmd)
    return commands


def _evidence_paths(files: dict[str, str], *patterns: str) -> list[str]:
    """Return relpaths whose path or content supports a production-flow node."""
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    found: list[str] = []
    for rel, text in files.items():
        haystack = f"{rel}\n{text}"
        if any(pattern.search(haystack) for pattern in compiled):
            found.append(rel)
    return found


def _flow_node(
    node_id: str,
    label: str,
    node_type: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label,
        "type": node_type,
        "evidence": evidence,
        "status": "observed" if evidence else "inferred",
    }


def _title_label(value: str) -> str:
    words = [word for word in re.split(r"[-_\s]+", value.strip()) if word]
    if not words:
        return ""
    normalized: list[str] = []
    for index, word in enumerate(words):
        lowered = word.lower()
        if lowered == "api":
            normalized.append("API")
        elif index == 0:
            normalized.append(lowered.title())
        else:
            normalized.append(lowered)
    return " ".join(normalized)


def _metadata_name(text: str) -> str:
    metadata = re.search(r"(?ms)^\s*metadata:\s*\n(?P<body>(?:\s+\S.*\n?)+)", text)
    scope = metadata.group("body") if metadata else text
    name = re.search(r"(?m)^\s*name:\s*['\"]?([a-zA-Z0-9_.-]+)", scope)
    return name.group(1) if name else ""


def _metadata_name_for_kind(text: str, kinds: set[str]) -> str:
    for doc in re.split(r"(?m)^---\s*$", text):
        kind = _yaml_scalar(doc, "kind")
        if kind not in kinds:
            continue
        name = _metadata_name(doc)
        if name:
            return name
    return ""


def _first_metadata_label(files: dict[str, str], evidence: list[str]) -> str:
    for rel in evidence:
        label = _title_label(_metadata_name(files.get(rel, "")))
        if label:
            return label
    return ""


def _first_kind_metadata_label(
    files: dict[str, str], evidence: list[str], kinds: set[str]
) -> str:
    for rel in evidence:
        label = _title_label(_metadata_name_for_kind(files.get(rel, ""), kinds))
        if label:
            return label
    return ""


def _worker_evidence(files: dict[str, str]) -> list[str]:
    found: list[str] = []
    for rel, text in files.items():
        rel_lower = rel.lower()
        if "/worker" not in rel_lower and "worker" not in Path(rel).name.lower():
            continue
        if re.search(r"\b(worker|consumer|cronjob|job)\b", f"{rel}\n{text}", re.IGNORECASE):
            found.append(rel)
    return found


def _derive_production_flow(
    files: dict[str, str],
    *,
    service_name: str,
    exposure: str,
    ingress: list[str],
    queues: list[str],
    datastores: list[str],
    external_dependencies: list[str],
) -> list[dict[str, Any]]:
    """Build a read-only, evidence-backed view of the current production path."""
    flow: list[dict[str, Any]] = []

    if exposure == "public":
        flow.append(
            _flow_node(
                "public_edge",
                "Public edge",
                "external_entrypoint",
                _evidence_paths(files, r"\bkind:\s*Ingress\b", r"\btype:\s*LoadBalancer\b"),
            )
        )

    if "alb" in ingress:
        flow.append(
            _flow_node(
                "alb",
                "ALB",
                "load_balancer",
                _evidence_paths(files, r"\balb\b", r"application load balancer"),
            )
        )

    if ingress:
        flow.append(
            _flow_node(
                "ingress_controller",
                "Ingress controller",
                "ingress",
                _evidence_paths(files, r"\bkind:\s*Ingress\b", r"ingress\.class", r"\bkong\b"),
            )
        )

    gateway_evidence = _evidence_paths(
        files,
        r"api[-_ ]?gateway",
        r"aws_api_gateway",
        r"gateway\.networking\.k8s\.io",
        r"\bkong\b",
    )
    if gateway_evidence:
        gateway_label = "Kong API Gateway" if any(
            "kong" in f"{rel}\n{files.get(rel, '')}".lower() for rel in gateway_evidence
        ) else "API Gateway"
        flow.append(_flow_node("api_gateway", gateway_label, "gateway", gateway_evidence))

    service_evidence = [
        rel
        for rel in _evidence_paths(
            files,
            r"\bkind:\s*(Deployment|Service)\b",
            r"\bkubernetes_deployment\b",
            r"\bhelm/.*/values\.ya?ml\b",
        )
        if "/worker" not in rel.lower() and "worker" not in Path(rel).name.lower()
    ]
    flow.append(
        _flow_node(
            "service",
            _first_kind_metadata_label(files, service_evidence, {"Service", "Deployment"})
            or (service_name if service_name != "unknown" else "Service"),
            "service",
            service_evidence,
        )
    )

    for queue in queues:
        queue_id = re.sub(r"[^a-zA-Z0-9_]+", "_", queue).strip("_") or "queue"
        flow.append(
            _flow_node(
                f"queue_{queue_id}",
                queue,
                "queue",
                _evidence_paths(files, re.escape(queue.split(":", 1)[-1]), r"\b(sqs|queue|redis|arq)\b"),
            )
        )

    worker_evidence = _worker_evidence(files)
    if worker_evidence:
        flow.append(
            _flow_node(
                "worker",
                _first_kind_metadata_label(
                    files, worker_evidence, {"Deployment", "CronJob", "Job"}
                )
                or _first_metadata_label(files, worker_evidence)
                or "Worker",
                "worker",
                worker_evidence,
            )
        )

    for datastore in datastores:
        flow.append(
            _flow_node(
                f"datastore_{datastore}",
                datastore,
                "datastore",
                _evidence_paths(files, re.escape(datastore)),
            )
        )

    for dependency in external_dependencies:
        label = (
            "External payment service"
            if dependency == "external_payment_service"
            else _title_label(dependency)
        )
        evidence = (
            _evidence_paths(files, r"external-payment-service", r"payment-provider")
            if dependency == "external_payment_service"
            else _evidence_paths(files, re.escape(dependency.split("_", 1)[0]))
        )
        flow.append(
            _flow_node(
                f"external_{dependency}",
                label,
                "external_dependency",
                evidence,
            )
        )

    return flow


def _derive_sensitive_surfaces(
    exposure: str,
    queues: list[str],
    datastores: list[str],
    secrets: list[str],
    blob: str,
) -> list[str]:
    surfaces: list[str] = []
    if exposure == "public":
        surfaces.append("public_api")
    auth_hint = any("TOKEN" in s or "PASSWORD" in s or "SECRET" in s for s in secrets)
    if auth_hint or re.search(r"\b(auth|oauth|jwt|login)\b", blob, re.IGNORECASE):
        surfaces.append("auth")
    if queues:
        surfaces.append("worker_retry")
    if datastores:
        surfaces.append("database_write")
    return surfaces


def _derive_confidence(files_found: int, strong_fields: int) -> str:
    if files_found == 0:
        return "low"
    if files_found >= 4 and strong_fields >= 4:
        return "high"
    return "medium"


def extract_facts(
    files: dict[str, str], *, files_found: list[str] | None = None
) -> dict[str, Any]:
    """Derive the architecture fact sheet from a ``{relpath: text}`` mapping."""
    blob = "\n".join(files.values())

    service_name = _detect_service_name(files)
    owners = _detect_owners(files)
    runtime = _detect_runtime(files, blob)
    deployment_type = _detect_deployment_type(files, blob)
    ingress = _detect_ingress(blob)
    exposure = _detect_exposure(blob, ingress)
    datastores = _detect_datastores(blob)
    queues = _detect_queues(files, blob)
    external = _detect_external_dependencies(blob)
    secrets = _detect_secrets_or_env(blob)
    resource_limits = _detect_resource_limits(files)
    verification_commands = _detect_verification_commands(files)
    production_flow = _derive_production_flow(
        files,
        service_name=service_name,
        exposure=exposure,
        ingress=ingress,
        queues=queues,
        datastores=datastores,
        external_dependencies=external,
    )
    sensitive_surfaces = _derive_sensitive_surfaces(
        exposure, queues, datastores, secrets, blob
    )

    strong_fields = sum(
        [
            service_name != "unknown",
            bool(owners),
            runtime != "unknown",
            exposure != "unknown",
            bool(datastores),
        ]
    )
    found = list(files_found) if files_found is not None else sorted(files)

    return {
        "service_name": service_name,
        "owners": owners,
        "runtime": runtime,
        "deployment_type": deployment_type,
        "exposure": exposure,
        "ingress": ingress,
        "datastores": datastores,
        "queues": queues,
        "external_dependencies": external,
        "production_flow": production_flow,
        "secrets_or_env": secrets,
        "resource_limits": resource_limits,
        "verification_commands": verification_commands,
        "sensitive_surfaces": sensitive_surfaces,
        "architecture_files_found": found,
        "confidence": _derive_confidence(len(found), strong_fields),
    }


def scan(repo_root: str | Path) -> dict[str, Any]:
    """Scan *repo_root* and return a best-effort architecture context dict."""
    root = Path(repo_root)
    collected = _collect_files(root) if root.is_dir() else []
    files = dict(collected)
    return extract_facts(files, files_found=[rel for rel, _ in collected])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan a repository for best-effort production architecture context."
    )
    parser.add_argument("--repo", default=".", help="Repository root to scan. Default: current dir.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON only on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        context = scan(args.repo)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(context, indent=2, sort_keys=True))
    else:
        print(f"Service:    {context['service_name']}")
        print(f"Runtime:    {context['runtime']}")
        print(f"Exposure:   {context['exposure']}")
        print(f"Owners:     {', '.join(context['owners']) or 'unknown'}")
        print(f"Datastores: {', '.join(context['datastores']) or 'none'}")
        print(f"Queues:     {', '.join(context['queues']) or 'none'}")
        print(f"Files:      {len(context['architecture_files_found'])} "
              f"(confidence: {context['confidence']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

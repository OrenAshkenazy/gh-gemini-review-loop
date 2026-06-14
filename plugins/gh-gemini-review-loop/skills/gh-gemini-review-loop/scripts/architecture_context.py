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
        line = line.strip()
        if not line or line.startswith("#"):
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


def _detect_runtime(files: dict[str, str]) -> str:
    rels = files.keys()
    if any(r.startswith("k8s/") or r.startswith("helm/") for r in rels):
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
        if name in _SECRET_EXACT or name.endswith(_SECRET_SUFFIXES):
            if name not in found:
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
    for match in re.finditer(r"(?m)^\s*-?\s*(?:command|run):\s*(['\"]?)([^\n#]+?)\1\s*$", ggrl):
        cmd = match.group(2).strip()
        if cmd and cmd not in commands:
            commands.append(cmd)
    test_re = re.compile(r"\b(pytest|uv run|go test|npm (?:run )?test|cargo test|jest)\b")
    for rel, text in files.items():
        if not rel.startswith(".github/workflows/"):
            continue
        for match in re.finditer(r"(?m)^\s*run:\s*(['\"]?)([^\n#]+?)\1\s*$", text):
            cmd = match.group(2).strip()
            if test_re.search(cmd) and cmd not in commands:
                commands.append(cmd)
    return commands


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


def scan(repo_root: str | Path) -> dict[str, Any]:
    """Scan *repo_root* and return a best-effort architecture context dict."""
    root = Path(repo_root)
    collected = _collect_files(root) if root.is_dir() else []
    files = dict(collected)
    blob = "\n".join(files.values())

    service_name = _detect_service_name(files)
    owners = _detect_owners(files)
    runtime = _detect_runtime(files)
    deployment_type = _detect_deployment_type(files, blob)
    ingress = _detect_ingress(blob)
    exposure = _detect_exposure(blob, ingress)
    datastores = _detect_datastores(blob)
    queues = _detect_queues(files, blob)
    external = _detect_external_dependencies(blob)
    secrets = _detect_secrets_or_env(blob)
    resource_limits = _detect_resource_limits(files)
    verification_commands = _detect_verification_commands(files)
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
    files_found = [rel for rel, _ in collected]

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
        "secrets_or_env": secrets,
        "resource_limits": resource_limits,
        "verification_commands": verification_commands,
        "sensitive_surfaces": sensitive_surfaces,
        "architecture_files_found": files_found,
        "confidence": _derive_confidence(len(files_found), strong_fields),
    }


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

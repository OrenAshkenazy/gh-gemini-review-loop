#!/usr/bin/env python3
"""Bootstrap a `mergeproof.yaml` for a service (onboarding, not PR runtime).

Supports both topologies:
- **same-repo** (app + infra in one repo): omit ``--infra-repo`` and the app
  repo itself becomes the architecture source.
- **split-repo**: pass ``--infra-repo OWNER/REPO`` for a dedicated infra repo.

The allowlist is **discovered** by walking ``--repo-root`` for real Terraform /
Helm / Kubernetes / container files, so the generated config matches the actual
layout instead of guessing. ``--allow`` overrides discovery; a static fallback
is used only when nothing is found and no checkout is available.

This proposes a config; it enforces nothing. Lifecycle:
    init -> review -> commit to the app repo's trusted base branch -> run.
No network access.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import sys
from pathlib import Path

DEFAULT_LIMITS = {"max_files": 200, "max_file_bytes": 262144}

# Strong signals: the whole containing directory is infra (emit dir/**).
_STRONG_EXTS = (".tf", ".tfvars")
_STRONG_NAMES = ("Chart.yaml", "values.yaml", "kustomization.yaml")
# Weak signals: only this file is infra-relevant (emit the file path, not dir/**).
_WEAK_NAMES = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml")
_IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "dist", "build",
    ".terraform", "vendor", "__pycache__", ".next", ".cache", ".idea",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages",
}


# Path segments whose YAML manifests count as infra purely by location.
_INFRA_DIR_SEGMENTS = {"envs", "environments", "k8s", "kubernetes", "helm", "manifests", "kustomize"}


@dataclass(frozen=True)
class CapabilityProposal:
    type: str
    approver: str
    generates: list[str]
    checks: list[str]
    inputs: dict[str, str]
    template_map: dict[str, dict[str, str]]
    human_gate: str | None = None


_WORKER_TEMPLATE = """\
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${service}-${worker_name}
  labels:
    app: ${service}
    component: ${worker_name}
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: ${worker_name}
          image: ${service}:latest
          command: ["python", "-m", "app.workers.${worker_name}"]
"""

_HELM_WORKER_TEMPLATE = """\
workers:
  ${worker_name}:
    enabled: true
    service: ${service}
"""

_TERRAFORM_WORKER_TEMPLATE = """\
resource "kubernetes_deployment" "${worker_name}" {
  metadata {
    name = "${service}-${worker_name}"
    labels = {
      app       = "${service}"
      component = "${worker_name}"
    }
  }

  spec {
    replicas = 1

    selector {
      match_labels = {
        app       = "${service}"
        component = "${worker_name}"
      }
    }

    template {
      metadata {
        labels = {
          app       = "${service}"
          component = "${worker_name}"
        }
      }

      spec {
        container {
          name    = "${worker_name}"
          image   = "${service}:latest"
          command = ["python", "-m", "app.workers.${worker_name}"]
        }
      }
    }
  }
}
"""

_EXTERNAL_SECRET_TEMPLATE = """\
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: ${service}-${secret_name}
spec:
  data:
    - secretKey: ${secret_name}
      remoteRef:
        key: ${HUMAN_GATE:secret value provisioning}
"""

_HELM_ENV_TEMPLATE = """\
env:
  ${secret_name}:
    valueFrom:
      secretKeyRef:
        name: ${service}-${secret_name}
        key: ${secret_name}
"""

_KAFKA_TOPIC_TEMPLATE = """\
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: ${topic}
  labels:
    service: ${service}
"""

_TEMPLATE_TEXT = {
    "templates/worker_deployment.tmpl": _WORKER_TEMPLATE,
    "templates/helm_worker_values.tmpl": _HELM_WORKER_TEMPLATE,
    "templates/terraform_worker.tmpl": _TERRAFORM_WORKER_TEMPLATE,
    "templates/external_secret.tmpl": _EXTERNAL_SECRET_TEMPLATE,
    "templates/helm_env_wiring.tmpl": _HELM_ENV_TEMPLATE,
    "templates/kafka_topic.tmpl": _KAFKA_TOPIC_TEMPLATE,
}


def _is_strong(name: str, rel_dir: str) -> bool:
    if name.endswith(_STRONG_EXTS) or name in _STRONG_NAMES:
        return True
    if rel_dir != "." and name.endswith((".yaml", ".yml")):
        return any(seg in _INFRA_DIR_SEGMENTS for seg in rel_dir.split("/"))
    return False


def _is_weak(name: str) -> bool:
    return name in _WEAK_NAMES


def discover_allow(repo_root: str | Path) -> list[str]:
    """Walk *repo_root* and return allowlist globs for real infra locations.

    Directories containing Terraform/Helm/k8s/container files become ``dir/**``
    globs, collapsed to their shortest common ancestors. Root-level signals
    (e.g. a top-level ``Dockerfile``) are kept by name. Noise directories
    (.git, node_modules, …) are skipped.
    """
    root = Path(repo_root)
    if not root.is_dir():
        return []
    strong_dirs: set[str] = set()
    loose_files: set[str] = set()
    root_files: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        rel = Path(dirpath).relative_to(root).as_posix()
        for name in filenames:
            if _is_strong(name, rel):
                (root_files if rel == "." else strong_dirs).add(name if rel == "." else rel)
            elif _is_weak(name):
                root_files.add(name) if rel == "." else loose_files.add(f"{rel}/{name}")

    # Collapse strong dirs to shortest ancestors: keep "infra/terraform",
    # drop "infra/terraform/modules/ecs".
    minimal: list[str] = []
    for d in sorted(strong_dirs):
        if not any(d == m or d.startswith(m + "/") for m in minimal):
            minimal.append(d)

    def _covered(path: str) -> bool:
        parent = path.rsplit("/", 1)[0]
        return any(parent == m or parent.startswith(m + "/") for m in minimal)

    allow = [f"{d}/**" for d in minimal]
    allow += [f for f in sorted(loose_files) if not _covered(f)]  # e.g. backend/Dockerfile
    allow += sorted(root_files)
    return allow


def default_allow(service: str) -> list[str]:
    """Static fallback used only when discovery finds nothing."""
    return [
        f"helm/{service}/**",
        "infra/terraform/**",
        "k8s/**",
    ]


def _list_repo_paths(repo_root: str | Path) -> list[str]:
    root = Path(repo_root)
    if not root.is_dir():
        return []
    paths: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        if rel_dir != ".":
            paths.append(rel_dir + "/")
        for name in filenames:
            paths.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    return sorted(paths)


def _has_prefix(paths: list[str], prefix: str) -> bool:
    prefix = prefix.strip("/")
    return any(path == prefix or path.startswith(prefix + "/") for path in paths)


def _path_contains(paths: list[str], *needles: str) -> bool:
    return any(all(needle in path for needle in needles) for path in paths)


def _file_text(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _slug(handle: str) -> str:
    return handle.strip().lstrip("@") or "platform-team"


def _worker_capability(
    service: str,
    env: str,
    approver: str,
    paths: list[str],
    root: Path,
) -> CapabilityProposal | None:
    env_prefix = f"envs/{env}/{service}"
    helm_prefix = f"helm/{service}"
    tf_prefix = f"terraform/{service}"
    helm_values = _file_text(root, f"{helm_prefix}/values.yaml") + _file_text(root, f"{helm_prefix}/values.yml")
    has_worker_signal = (
        _has_prefix(paths, f"{env_prefix}/workers")
        or _has_prefix(paths, f"{tf_prefix}/workers")
        or _has_prefix(paths, "modules/workers")
        or "workers:" in helm_values
        or _path_contains(paths, "worker")
    )
    if not has_worker_signal:
        return None

    template_map: dict[str, dict[str, str]] = {}
    if _has_prefix(paths, env_prefix):
        template_map["worker_deployment"] = {
            "template": "templates/worker_deployment.tmpl",
            "output": f"{env_prefix}/workers/${{worker_name}}-deployment.yaml",
        }
    if _has_prefix(paths, helm_prefix):
        template_map["helm_worker_values"] = {
            "template": "templates/helm_worker_values.tmpl",
            "output": f"{helm_prefix}/workers/${{worker_name}}.yaml",
        }
    if _has_prefix(paths, tf_prefix) or _has_prefix(paths, "modules/workers"):
        template_map["terraform_worker"] = {
            "template": "templates/terraform_worker.tmpl",
            "output": f"{tf_prefix}/workers/${{worker_name}}.tf",
        }
    if not template_map:
        return None
    checks = ["helm_template", "policy", "naming_convention"]
    if "terraform_worker" in template_map:
        checks.insert(1, "terraform_validate")
    return CapabilityProposal(
        type="worker_deployment",
        approver=approver,
        generates=list(template_map),
        checks=checks,
        inputs={"worker_name": "required", "service": "required"},
        template_map=template_map,
    )


def _secret_capability(
    service: str,
    env: str,
    approver: str,
    paths: list[str],
) -> CapabilityProposal | None:
    env_prefix = f"envs/{env}/{service}"
    helm_prefix = f"helm/{service}"
    has_secret_signal = (
        _has_prefix(paths, f"{env_prefix}/secrets")
        or _has_prefix(paths, "modules/secrets")
        or _has_prefix(paths, f"{helm_prefix}/env")
        or _path_contains(paths, "secret")
    )
    if not has_secret_signal:
        return None

    template_map: dict[str, dict[str, str]] = {}
    if _has_prefix(paths, env_prefix):
        template_map["external_secret"] = {
            "template": "templates/external_secret.tmpl",
            "output": f"{env_prefix}/secrets/${{secret_name}}.yaml",
        }
    if _has_prefix(paths, helm_prefix):
        template_map["helm_env_wiring"] = {
            "template": "templates/helm_env_wiring.tmpl",
            "output": f"{helm_prefix}/env/${{secret_name}}.yaml",
        }
    if not template_map:
        return None
    return CapabilityProposal(
        type="secret_wiring",
        approver=approver,
        generates=list(template_map),
        checks=["helm_template", "policy", "naming_convention"],
        inputs={"secret_name": "required", "env_var": "required", "service": "required"},
        template_map=template_map,
        human_gate="secret value provisioning",
    )


def _topic_capability(
    service: str,
    env: str,
    approver: str,
    paths: list[str],
) -> CapabilityProposal | None:
    env_prefix = f"envs/{env}/{service}"
    has_topic_signal = (
        _has_prefix(paths, f"{env_prefix}/topics")
        or _has_prefix(paths, f"{env_prefix}/queues")
        or _has_prefix(paths, "modules/kafka")
        or _path_contains(paths, "kafka")
    )
    if not has_topic_signal or not _has_prefix(paths, env_prefix):
        return None
    return CapabilityProposal(
        type="topic_queue",
        approver=approver,
        generates=["kafka_topic"],
        checks=["helm_template", "policy", "naming_convention"],
        inputs={"topic": "required", "service": "required"},
        template_map={
            "kafka_topic": {
                "template": "templates/kafka_topic.tmpl",
                "output": f"{env_prefix}/topics/${{topic}}.yaml",
            }
        },
    )


def discover_capabilities(
    repo_root: str | Path,
    *,
    service: str,
    env: str = "prod",
    approver: str = "@platform-team",
) -> list[CapabilityProposal]:
    """Infer supported capability packs from the infra repo layout."""
    root = Path(repo_root)
    paths = _list_repo_paths(root)
    proposals = [
        _worker_capability(service, env, approver, paths, root),
        _secret_capability(service, env, approver, paths),
        _topic_capability(service, env, approver, paths),
    ]
    return [proposal for proposal in proposals if proposal is not None]


def render_config(
    *,
    service: str,
    infra_repo: str,
    ref: str,
    env: str,
    allow: list[str],
    capabilities: list[CapabilityProposal] | None = None,
    limits: dict[str, int] | None = None,
) -> str:
    limits = limits or DEFAULT_LIMITS
    lines = [
        "version: 1",
        "",
        f"service: {service}",
        "",
        f"# Production environment: {env}",
        "architecture_sources:",
        f"  - repo: {infra_repo}",
        f"    ref: {ref}",
        "    allow:",
    ]
    lines.extend(f"      - {pattern}" for pattern in allow)
    if capabilities:
        lines.extend(["", "capabilities:"])
        for capability in capabilities:
            lines.extend(
                [
                    f"  - type: {capability.type}",
                    f"    template: capabilities/{capability.type}.yaml",
                    f"    approver: \"{capability.approver}\"",
                ]
            )
    lines.extend(
        [
            "",
            "limits:",
            f"  max_files: {limits['max_files']}",
            f"  max_file_bytes: {limits['max_file_bytes']}",
            "",
        ]
    )
    return "\n".join(lines)


def render_capability_pack(capability: CapabilityProposal) -> str:
    lines = [
        f"capability: {capability.type}",
        "inputs:",
    ]
    lines.extend(f"  {key}: {value}" for key, value in capability.inputs.items())
    lines.append("generates:")
    lines.extend(f"  - {key}" for key in capability.generates)
    lines.append("checks:")
    lines.extend(f"  - {check}" for check in capability.checks)
    lines.extend(["approval:", "  required_from:", f"    - {_slug(capability.approver)}"])
    if capability.human_gate:
        lines.append(f"human_gate: {capability.human_gate}")
    lines.append("template_map:")
    for key, mapping in capability.template_map.items():
        lines.extend(
            [
                f"  {key}:",
                f"    template: {mapping['template']}",
                f"    output: {mapping['output']}",
            ]
        )
    return "\n".join(lines) + "\n"


def write_capability_files(
    output_dir: str | Path,
    capabilities: list[CapabilityProposal],
    *,
    force: bool = False,
) -> None:
    root = Path(output_dir)
    for capability in capabilities:
        pack_path = root / "capabilities" / f"{capability.type}.yaml"
        if pack_path.exists() and not force:
            raise FileExistsError(f"{pack_path} already exists; pass --force to overwrite")
        pack_path.parent.mkdir(parents=True, exist_ok=True)
        pack_path.write_text(render_capability_pack(capability), encoding="utf-8")
        for mapping in capability.template_map.values():
            template_path = root / "capabilities" / mapping["template"]
            if template_path.exists() and not force:
                raise FileExistsError(f"{template_path} already exists; pass --force to overwrite")
            template_path.parent.mkdir(parents=True, exist_ok=True)
            template_path.write_text(_TEMPLATE_TEXT[mapping["template"]], encoding="utf-8")


def repo_from_git(repo_root: str | Path) -> str | None:
    """Best-effort OWNER/REPO from a local checkout's git remote."""
    git_dir = Path(repo_root) / ".git"
    if git_dir.is_file():
        # Submodules and worktrees use a `.git` file with a `gitdir:` pointer.
        try:
            pointer = git_dir.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        target = next(
            (
                ln.split("gitdir:", 1)[1].strip()
                for ln in pointer.splitlines()
                if ln.strip().startswith("gitdir:")
            ),
            None,
        )
        if target is None:
            return None
        resolved = (git_dir.parent / target).resolve() if not Path(target).is_absolute() else Path(target)
        # Worktrees keep config in the common dir, pointed to by `commondir`.
        commondir = resolved / "commondir"
        if commondir.is_file():
            common = commondir.read_text(encoding="utf-8", errors="replace").strip()
            resolved = (resolved / common).resolve() if not Path(common).is_absolute() else Path(common)
        config = resolved / "config"
    else:
        config = git_dir / "config"
    if not config.is_file():
        return None
    try:
        config_text = config.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return None
    for line in config_text.splitlines():
        line = line.strip()
        if "github.com" not in line or not line.startswith("url"):
            continue
        url = line.split("=", 1)[-1].strip().removesuffix(".git")
        if "github.com:" in url:
            return url.split("github.com:", 1)[1]
        if "github.com/" in url:
            return url.split("github.com/", 1)[1]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a mergeproof.yaml by discovering real infra files."
    )
    parser.add_argument("--repo", help="App repo OWNER/REPO. Inferred from git remote if omitted.")
    parser.add_argument(
        "--infra-repo",
        help="Infra repo OWNER/REPO. Omit for same-repo (uses the app repo).",
    )
    parser.add_argument("--repo-root", default=".", help="Local checkout to scan. Default: cwd.")
    parser.add_argument("--service", help="Service name. Defaults to the repo name.")
    parser.add_argument("--ref", default="main", help="Infra source ref. Default: main.")
    parser.add_argument("--env", default="prod", help="Production environment. Default: prod.")
    parser.add_argument(
        "--approver",
        default="@platform-team",
        help="Default approver for discovered capability packs. Default: @platform-team.",
    )
    parser.add_argument(
        "--no-capabilities",
        action="store_true",
        help="Only generate architecture_sources/allowlist; skip capability discovery.",
    )
    parser.add_argument(
        "--allow", action="append", default=None,
        help="Override an allowlist glob (repeatable); skips discovery.",
    )
    parser.add_argument("--max-files", type=int, default=DEFAULT_LIMITS["max_files"])
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_LIMITS["max_file_bytes"])
    parser.add_argument("--output", help="Write config here. Prints to stdout when omitted.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing --output.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    root = Path(args.repo_root)
    app_repo = args.repo or repo_from_git(root)
    infra_repo = args.infra_repo or app_repo  # same-repo default
    if not infra_repo:
        print(
            "error: could not determine the source repo. Pass --repo OWNER/REPO "
            "(app repo, used as the same-repo source) or --infra-repo OWNER/REPO.",
            file=sys.stderr,
        )
        return 2

    service = args.service or (app_repo.split("/")[-1] if app_repo else root.resolve().name)

    if args.allow:
        allow = args.allow
    else:
        allow = discover_allow(root) or default_allow(service)
    capabilities = [] if args.no_capabilities else discover_capabilities(
        root, service=service, env=args.env, approver=args.approver
    )

    payload = render_config(
        service=service,
        infra_repo=infra_repo,
        ref=args.ref,
        env=args.env,
        allow=allow,
        capabilities=capabilities,
        limits={"max_files": args.max_files, "max_file_bytes": args.max_file_bytes},
    )

    if not args.output:
        print(payload, end="")
        return 0

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"error: {output} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(payload, encoding="utf-8")
        write_capability_files(output.parent, capabilities, force=args.force)
    except OSError as exc:
        print(f"error: could not write config file: {exc}", file=sys.stderr)
        return 1
    print(f"Wrote {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

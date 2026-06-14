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
_INFRA_DIR_SEGMENTS = {"k8s", "kubernetes", "helm", "manifests", "kustomize"}


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


def render_config(
    *,
    service: str,
    infra_repo: str,
    ref: str,
    env: str,
    allow: list[str],
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


def repo_from_git(repo_root: str | Path) -> str | None:
    """Best-effort OWNER/REPO from a local checkout's git remote."""
    config = Path(repo_root) / ".git" / "config"
    if not config.is_file():
        return None
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
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

    payload = render_config(
        service=service,
        infra_repo=infra_repo,
        ref=args.ref,
        env=args.env,
        allow=allow,
        limits={"max_files": args.max_files, "max_file_bytes": args.max_file_bytes},
    )

    if not args.output:
        print(payload, end="")
        return 0

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"error: {output} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(f"Wrote {output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

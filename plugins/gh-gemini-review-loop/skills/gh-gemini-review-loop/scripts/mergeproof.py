#!/usr/bin/env python3
"""MergeProof command surface.

The full product flow is agent-driven: Claude runs the CR loop to terminal
state, then this command runs the readiness phase from that terminal output.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mergeproof_readiness import _load_loop_summary, load_latest_run_summary, run_readiness
from metrics import runs_log_path
from pr_architecture_risk import parse_pr
from render_demo_ui import render_html

DEFAULT_LIMITS = {"max_files": 200, "max_file_bytes": 262144}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MergeProof command surface.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init",
        description="Create an initial mergeproof.yaml for this repo.",
    )
    init.add_argument("--repo-root", default=".", help="Repository root. Default: current dir.")
    init.add_argument("--repo", help="GitHub repo in OWNER/REPO format.")
    init.add_argument("--service", help="Service name. Defaults to repository name.")
    init.add_argument("--ref", default="main", help="Infra source ref. Default: main.")
    init.add_argument("--output", default="mergeproof.yaml", help="Config output path.")
    init.add_argument("--force", action="store_true", help="Overwrite an existing output file.")
    init.add_argument("--print", action="store_true", dest="print_config", help="Print config to stdout instead of writing.")
    run = sub.add_parser(
        "run",
        description="Run the MergeProof readiness phase from terminal CR loop output.",
    )
    run.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    source = run.add_mutually_exclusive_group()
    source.add_argument("--loop-summary", help="Path to terminal loop summary JSON.")
    source.add_argument(
        "--runs-jsonl",
        default=str(runs_log_path()),
        help="Path to runs.jsonl. Defaults to the GGRL state runs log.",
    )
    run.add_argument(
        "--mergeproof",
        help="Path to a local mergeproof.yaml/.json to use instead of the base-ref config.",
    )
    run.add_argument("--trust-pr-config", action="store_true")
    run.add_argument("--publish", action="store_true")
    run.add_argument("--json", action="store_true", dest="json_stdout")
    run.add_argument("--markdown", action="store_true", dest="markdown_stdout")
    run.add_argument("--json-output", help="Write readiness JSON to this path.")
    run.add_argument("--markdown-output", help="Write readiness Markdown to this path.")
    run.add_argument("--html-output", help="Write the static HTML report to this path.")
    return parser


def _repo_from_git(root: Path) -> str | None:
    git_config = root / ".git" / "config"
    if not git_config.is_file():
        return None
    text = git_config.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "github.com" not in line:
            continue
        url = line.split("=", 1)[-1].strip()
        url = url.removesuffix(".git")
        if "github.com:" in url:
            return url.split("github.com:", 1)[1]
        if "github.com/" in url:
            return url.split("github.com/", 1)[1]
    return None


def _service_name(root: Path, repo: str | None, explicit: str | None) -> str:
    if explicit:
        return explicit
    if repo and "/" in repo:
        return repo.rsplit("/", 1)[1]
    return root.resolve().name


def infer_allowlist(root: Path) -> list[str]:
    candidates = [
        "helm/*/values.yaml",
        "helm/*/templates/backend/**",
        "helm/*/templates/worker/**",
        "helm/*/templates/redis/**",
        "helm/*/templates/ingress.yaml",
        "infra/terraform/environments/*/**",
        "infra/terraform/modules/alb/**",
        "infra/terraform/modules/ecs/**",
        "infra/terraform/modules/rds/**",
        "infra/terraform/modules/efs/**",
        "backend/app/jobs/**",
    ]
    allowed: list[str] = []
    for pattern in candidates:
        if any(root.glob(pattern)):
            allowed.append(pattern)
    if not allowed:
        allowed = ["infra/**", "helm/**", "k8s/**"]
    return allowed


def render_config(*, service: str, repo: str, ref: str, allow: list[str]) -> str:
    lines = [
        "version: 1",
        f"service: {service}",
        "architecture_sources:",
        f"  - repo: {repo}",
        f"    ref: {ref}",
        "    allow:",
    ]
    lines.extend(f"      - {pattern}" for pattern in allow)
    lines.extend(
        [
            "limits:",
            f"  max_files: {DEFAULT_LIMITS['max_files']}",
            f"  max_file_bytes: {DEFAULT_LIMITS['max_file_bytes']}",
            "",
        ]
    )
    return "\n".join(lines)


def _write(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _init(args: argparse.Namespace) -> int:
    root = Path(args.repo_root).resolve()
    repo = args.repo or _repo_from_git(root)
    if not repo:
        raise ValueError("--repo is required when the GitHub remote cannot be inferred")
    service = _service_name(root, repo, args.service)
    payload = render_config(
        service=service,
        repo=repo,
        ref=args.ref,
        allow=infer_allowlist(root),
    )
    if args.print_config:
        print(payload, end="")
        return 0
    output = root / args.output
    if output.exists() and not args.force:
        raise ValueError(f"{output} already exists; pass --force to overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(f"Wrote {output}")
    return 0


def _run(args: argparse.Namespace) -> int:
    repo, number = parse_pr(args.pr)
    if args.loop_summary:
        loop_summary = _load_loop_summary(args.loop_summary)
    else:
        loop_summary = load_latest_run_summary(args.runs_jsonl, repo, number)

    config_override = None
    if args.mergeproof:
        from mergeproof_config import load_config

        text = Path(args.mergeproof).read_text(encoding="utf-8")
        fmt = "json" if args.mergeproof.endswith(".json") else "yaml"
        config_override = load_config(text, fmt=fmt)

    result = run_readiness(
        repo,
        number,
        loop_summary,
        trust_pr_config=args.trust_pr_config,
        do_publish=args.publish,
        config_override=config_override,
    )
    if result["status"] == "skipped":
        return 0

    readiness = result["readiness"]
    markdown = result["markdown"]
    if args.json_output:
        _write(args.json_output, json.dumps(readiness, indent=2, sort_keys=True) + "\n")
    if args.markdown_output:
        _write(args.markdown_output, markdown)
    if args.html_output:
        _write(args.html_output, render_html(readiness))
    if args.json_stdout:
        print(json.dumps(readiness, indent=2, sort_keys=True))
    elif args.markdown_stdout:
        print(markdown, end="")
    elif not (args.json_output or args.markdown_output or args.html_output or args.publish):
        print(f"[mergeproof] readiness: {readiness['status']}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.command == "init":
            return _init(args)
        if args.command == "run":
            return _run(args)
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

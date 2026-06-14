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

import init_mergeproof
from mergeproof_readiness import _load_loop_summary, load_latest_run_summary, run_readiness
from metrics import runs_log_path
from pr_architecture_risk import parse_pr
from render_demo_ui import render_html


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MergeProof command surface.")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser(
        "init",
        description="Create an initial mergeproof.yaml for this repo.",
    )
    init.add_argument("--repo-root", default=".", help="Repository root to scan. Default: current dir.")
    init.add_argument("--repo", help="App repo OWNER/REPO. Inferred from git remote if omitted.")
    init.add_argument(
        "--infra-repo", help="Infra repo OWNER/REPO. Omit for same-repo (uses the app repo)."
    )
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


def _write(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _init(args: argparse.Namespace) -> int:
    """Delegate to init_mergeproof so both entry points discover infra identically."""
    argv = ["--repo-root", args.repo_root, "--ref", args.ref]
    if args.repo:
        argv += ["--repo", args.repo]
    if args.infra_repo:
        argv += ["--infra-repo", args.infra_repo]
    if args.service:
        argv += ["--service", args.service]
    if not args.print_config:
        argv += ["--output", str(Path(args.repo_root) / args.output)]
        if args.force:
            argv += ["--force"]
    return init_mergeproof.main(argv)


def _run(args: argparse.Namespace) -> int:
    repo, number = parse_pr(args.pr)
    if args.loop_summary:
        loop_summary = _load_loop_summary(args.loop_summary)
    else:
        loop_summary = load_latest_run_summary(args.runs_jsonl, repo, number)

    config_override = None
    if args.mergeproof:
        from mergeproof_config import load_config

        text = Path(args.mergeproof).read_text(encoding="utf-8", errors="replace")
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

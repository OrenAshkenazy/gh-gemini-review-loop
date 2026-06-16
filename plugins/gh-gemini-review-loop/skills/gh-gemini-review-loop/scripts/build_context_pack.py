#!/usr/bin/env python3
"""Assemble a stable Production Context Pack for a PR."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from architecture_context import extract_facts
from fetch_infra_files import fetch_infra_files
from pr_architecture_risk import _default_pr_runner, fetch_pr_changed_files, parse_pr
from resolve_mergeproof import resolve

Runner = Callable[[list[str]], Any]

SKIP_MESSAGE = "[mergeproof] readiness skipped\nReason: mergeproof.yaml not found"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pack(
    app_repo: str,
    pr_number: int,
    changed_files: list[str],
    *,
    runner: Runner = _default_pr_runner,
    trust_pr_config: bool = False,
    now_iso: str | None = None,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    resolution = resolve(
        app_repo,
        pr_number,
        changed_files,
        runner=runner,
        trust_pr_config=trust_pr_config,
        config_override=config_override,
    )
    if resolution["status"] == "MISSING":
        return None

    config = resolution["config"]
    limits = config["limits"]
    all_files: dict[str, str] = {}
    sources_meta: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed_sources: list[dict[str, str]] = list(resolution.get("failed_sources") or [])
    tree_truncated = False

    for source in config["architecture_sources"]:
        if not source.get("resolved_sha"):
            continue  # ref resolution already failed; recorded in failed_sources
        try:
            fetched = fetch_infra_files(
                source,
                max_files=limits["max_files"],
                max_file_bytes=limits["max_file_bytes"],
                runner=runner,
            )
        except RuntimeError as exc:
            failed_sources.append({"repo": source["repo"], "error": str(exc)})
            continue
        all_files.update(fetched["files"])
        skipped.extend(fetched["skipped"])
        tree_truncated = tree_truncated or fetched["truncated"]
        sources_meta.append(
            {
                "repo": source["repo"],
                "ref": source["ref"],
                "resolved_sha": source["resolved_sha"],
                "files": fetched["fetched_paths"],
            }
        )

    facts = extract_facts(all_files, files_found=sorted(all_files))
    if facts.get("service_name") == "unknown":
        facts["service_name"] = config["service"]
    return {
        "service": config["service"],
        "facts": facts,
        "provenance": {
            "sources": sources_meta,
            "fetched_at": now_iso or _now_iso(),
            "file_count": len(all_files),
        },
        "config": {
            "path": resolution["config_path"],
            "ref": resolution["config_ref"],
            "text": resolution.get("config_text") or "",
            "architecture_sources": config["architecture_sources"],
        },
        "safety": {
            "limits": limits,
            "skipped": skipped,
            "tree_truncated": tree_truncated,
            "failed_sources": failed_sources,
            "secrets_redacted": True,
            "config_changed": resolution["config_changed"],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Production Context Pack for a PR.")
    parser.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    parser.add_argument(
        "--trust-pr-config",
        action="store_true",
        help="Read mergeproof config from PR head instead of the trusted base ref.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--output", help="Write the pack JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        repo, number = parse_pr(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        changed = fetch_pr_changed_files(repo, number)
        pack = build_pack(repo, number, changed, trust_pr_config=args.trust_pr_config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if pack is None:
        print(SKIP_MESSAGE, file=sys.stderr)
        return 0

    payload = json.dumps(pack, indent=2, sort_keys=True)
    if args.output:
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        if not args.json_output:
            print(f"Wrote {args.output}")
    if args.json_output or not args.output:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resolve MergeProof config from the trusted base ref of a PR."""

from __future__ import annotations

from typing import Any, Callable

from fetch_infra_files import _decode_content
from mergeproof_config import load_config

Runner = Callable[[list[str]], Any]

CONFIG_PATHS = ("mergeproof.json", "mergeproof.yaml", "mergeproof.yml")


def _fmt_for(path: str) -> str:
    return "json" if path.endswith(".json") else "yaml"


def fetch_config(repo: str, ref: str, runner: Runner) -> tuple[dict[str, Any], str] | None:
    for path in CONFIG_PATHS:
        try:
            payload = runner(["api", f"repos/{repo}/contents/{path}?ref={ref}"])
        except RuntimeError:
            continue
        text = _decode_content(payload)
        if text is None:
            continue
        return load_config(text, fmt=_fmt_for(path)), path
    return None


def resolve(
    app_repo: str,
    pr_number: int,
    changed_files: list[str],
    *,
    runner: Runner,
    trust_pr_config: bool = False,
    pr_head_sha: str | None = None,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pr = runner(["api", f"repos/{app_repo}/pulls/{pr_number}"])
    if not isinstance(pr, dict):
        raise RuntimeError(f"Unexpected PR response format: {pr}")
    base = pr.get("base")
    base = base if isinstance(base, dict) else {}
    head = pr.get("head")
    head = head if isinstance(head, dict) else {}
    base_sha = base.get("sha")
    head_sha = pr_head_sha or head.get("sha")
    config_ref = head_sha if trust_pr_config and head_sha else base_sha
    # Fail fast: a missing ref would otherwise be passed downstream as the string
    # "None" in API calls, producing confusing 404s far from the real cause.
    if not config_ref:
        raise RuntimeError(
            f"could not resolve a config ref for {app_repo}#{pr_number}: "
            "PR metadata has no base/head commit SHA"
        )
    config_changed = any(path in CONFIG_PATHS for path in changed_files)

    # An explicit operator-provided config (--mergeproof) is trusted as given and
    # bypasses the base-ref fetch; config-change detection is still reported.
    if config_override is not None:
        found: tuple[dict[str, Any], str] | None = (config_override, "(provided)")
    else:
        found = fetch_config(app_repo, config_ref, runner)
    if found is None:
        return {
            "status": "MISSING",
            "config": None,
            "config_changed": config_changed,
            "config_path": None,
            "config_ref": config_ref,
        }

    config, path = found
    # Resolve each source ref to an immutable SHA. An inaccessible infra repo
    # (404/403) must degrade to partial context, not crash the readiness phase.
    failed_sources: list[dict[str, str]] = []
    for source in config["architecture_sources"]:
        try:
            commit = runner(["api", f"repos/{source['repo']}/commits/{source['ref']}"])
            source["resolved_sha"] = commit.get("sha") if isinstance(commit, dict) else None
        except RuntimeError as exc:
            source["resolved_sha"] = None
            failed_sources.append({"repo": source["repo"], "error": str(exc)})
            continue
        if not source.get("resolved_sha"):
            failed_sources.append(
                {"repo": source["repo"], "error": f"ref '{source['ref']}' resolved to no commit SHA"}
            )

    status = "OK"
    if config_changed and not trust_pr_config:
        status = "CONFIG_CHANGED_REVIEW_REQUIRED"
    return {
        "status": status,
        "config": config,
        "config_changed": config_changed,
        "config_path": path,
        "config_ref": config_ref,
        "failed_sources": failed_sources,
    }

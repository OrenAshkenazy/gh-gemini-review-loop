#!/usr/bin/env python3
"""Classify an env var as secret/config/unknown from infra-repo precedent.

The infra repo is the oracle: a new env var is classified by how its
suffix-peers (vars sharing the trailing ``_TOKEN`` segment) are already wired.
Workload-scoped evidence wins; if the service has no local peer, fall back to
repo-wide evidence; if precedent is absent or contradictory, return ``unknown``
so the caller raises a human gate. Every verdict cites its evidence files.

A name-pattern table is intentionally NOT the authority here (the existing
architecture_context table even lists ``_URL`` as secret-ish). It survives only
as an advisory suggestion the caller may show inside a human gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ENV_TOKEN = re.compile(r"\b([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)\b")
_SECRET_KIND = re.compile(r"kind:\s*(?:ExternalSecret|Secret)\b")
_CONFIG_KIND = re.compile(r"kind:\s*ConfigMap\b")


def _suffix(name: str) -> str:
    return name.rsplit("_", 1)[-1]


def _is_secret_source(path: str, text: str) -> bool:
    lowered = path.lower().replace("\\", "/")  # normalize Windows separators
    return "/secrets/" in lowered or "externalsecret" in lowered or bool(_SECRET_KIND.search(text))


def _is_config_source(path: str, text: str) -> bool:
    lowered = path.lower().replace("\\", "/")  # normalize Windows separators
    return (
        lowered.endswith("values.yaml")
        or "/env/" in lowered
        or "configmap" in lowered
        or bool(_CONFIG_KIND.search(text))
    )


def _scan(name: str, paths: list[str], infra_files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (secret_evidence, config_evidence) paths that wire a suffix-peer of *name*."""
    target = _suffix(name)
    secret_ev: list[str] = []
    config_ev: list[str] = []
    for path in paths:
        text = infra_files[path]
        if not isinstance(text, str):
            continue  # defend against non-str values in the infra slice
        peers = {n for n in _ENV_TOKEN.findall(text) if n != name and _suffix(n) == target}
        if not peers:
            continue
        # A file that reads as BOTH a secret and a config source is ambiguous;
        # record it in both buckets so classify_env surfaces it as `unknown`
        # rather than silently resolving to one mechanism.
        if _is_secret_source(path, text):
            secret_ev.append(path)
        if _is_config_source(path, text):
            config_ev.append(path)
    return sorted(secret_ev), sorted(config_ev)


def _verdict(classification: str, capability: str | None, scope: str,
             evidence: list[str], reason: str) -> dict[str, Any]:
    return {
        "classification": classification,
        "capability": capability,
        "precedent_scope": scope,
        "evidence_files": evidence,
        "reason": reason,
    }


def classify_env(name: str, infra_files: dict[str, str], service: str) -> dict[str, Any]:
    """Classify *name* as secret/config/unknown from cited infra precedent."""
    target = _suffix(name)
    all_paths = sorted(infra_files)
    workload_paths = [p for p in all_paths if service and service in p]

    for scope, paths in (("workload", workload_paths), ("repo_wide", all_paths)):
        secret_ev, config_ev = _scan(name, paths, infra_files)
        if secret_ev and config_ev:
            return _verdict(
                "unknown", None, scope, [],
                f"{name}: contradictory precedent for suffix _{target} "
                f"(wired as both secret and config) at {scope} scope",
            )
        if secret_ev:
            return _verdict(
                "secret", "secret_wiring", scope, secret_ev,
                f"{name}: peers sharing suffix _{target} are wired via secret manifests",
            )
        if config_ev:
            return _verdict(
                "config", "runtime_config", scope, config_ev,
                f"{name}: peers sharing suffix _{target} are wired as config (ConfigMap/Helm values)",
            )
        # no peers at this scope -> try the next scope
    return _verdict(
        "unknown", None, "none", [],
        f"{name}: no wiring precedent for suffix _{target} in the infra repo",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Classify an env var from infra precedent.")
    parser.add_argument("--name", required=True, help="Env var name to classify.")
    parser.add_argument("--service", default="", help="Service name for workload-scoped precedent.")
    parser.add_argument(
        "--infra-files",
        required=True,
        help="Path to a JSON object mapping infra file path -> file content.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        data = json.loads(Path(args.infra_files).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    infra = data if isinstance(data, dict) else {}
    print(json.dumps(classify_env(args.name, infra, args.service), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

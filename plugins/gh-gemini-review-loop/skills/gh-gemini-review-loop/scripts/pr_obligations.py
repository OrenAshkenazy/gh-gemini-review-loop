#!/usr/bin/env python3
"""Map a PR's changed files to production infra obligations.

Deterministic, advisory, zero-dependency. An obligation says "this app change
implies an infra change of type X". Each obligation resolves to one of three
outcomes against the trusted ``mergeproof.yaml`` capabilities:

* ``matched``     — a Capability Pack exists and every required input is derivable.
* ``human_gated`` — a pack exists but a human must still supply a value
                    (a declared ``human_gate`` and/or a non-derivable required input).
* ``blocked``     — the change implies an obligation with no declared pack.

This module never generates files or writes to any repo (that is Phase 2).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from capability_pack import capabilities_from_config, load_pack
from mergeproof_config import parse_yaml_subset

# (obligation type, path regex, triggering statuses)
_RULES: tuple[tuple[str, re.Pattern[str], tuple[str, ...]], ...] = (
    ("worker_deployment", re.compile(r"(^|/)(workers?|jobs?|consumers?)/", re.IGNORECASE), ("added",)),
    ("secret_wiring", re.compile(r"(^|/)secrets?/", re.IGNORECASE), ("added", "modified")),
    ("topic_queue", re.compile(r"(^|/)(topics?|queues?|streams?)/", re.IGNORECASE), ("added",)),
)


def _normalize(changed_files: list[Any]) -> list[dict[str, str]]:
    """Accept list[str] or list[{path,status}]; default status to 'modified'."""
    norm: list[dict[str, str]] = []
    for entry in changed_files:
        if isinstance(entry, str):
            norm.append({"path": entry, "status": "modified"})
        elif isinstance(entry, dict) and isinstance(entry.get("path"), str):
            norm.append({"path": entry["path"], "status": str(entry.get("status") or "modified")})
    return norm


def _derive_inputs(obligation_type: str, evidence: list[str], service: str) -> dict[str, str]:
    """Inputs derivable from the changed-file path alone (deterministic)."""
    inputs: dict[str, str] = {"service": service} if service else {}
    stem = Path(evidence[0]).stem
    if obligation_type == "worker_deployment":
        inputs["worker_name"] = stem
    elif obligation_type == "secret_wiring":
        inputs["secret_name"] = stem
    return inputs


def detect_obligations(
    changed_files: list[Any],
    capabilities: dict[str, dict[str, Any]],
    packs: dict[str, dict[str, Any]],
    service: str = "",
) -> list[dict[str, Any]]:
    """Return deterministic obligations for *changed_files*.

    *capabilities* maps obligation type -> declared capability entry (or is empty).
    *packs* maps obligation type -> a loaded Capability Pack (see capability_pack.load_pack).
    """
    norm = _normalize(changed_files)
    obligations: list[dict[str, Any]] = []

    for obligation_type, pattern, statuses in _RULES:
        evidence = [
            entry["path"]
            for entry in norm
            if pattern.search(entry["path"]) and entry["status"] in statuses
        ]
        if not evidence:
            continue

        capability = capabilities.get(obligation_type)
        if capability is None:
            obligations.append(
                {
                    "type": obligation_type,
                    "outcome": "blocked",
                    "evidence_files": evidence,
                    "inputs": {},
                    "pack": None,
                    "human_gate_pending": [],
                }
            )
            continue

        pack = packs.get(obligation_type) or {}
        inputs = _derive_inputs(obligation_type, evidence, service)
        required = [k for k, v in (pack.get("inputs") or {}).items() if v == "required"]
        missing = [k for k in required if k not in inputs]
        gate = pack.get("human_gate")
        human_gate_pending = ([gate] if gate else []) + [f"input: {k}" for k in missing]
        approver = (
            capability.get("approver")
            or (f"@{pack['approval']['required_from'][0].lstrip('@')}" if pack.get("approval") else None)
        )

        obligations.append(
            {
                "type": obligation_type,
                "outcome": "human_gated" if human_gate_pending else "matched",
                "evidence_files": evidence,
                "inputs": inputs,
                "pack": {
                    "generates": list(pack.get("generates") or []),
                    "checks": list(pack.get("checks") or []),
                    "approver": approver,
                    "human_gate": gate,
                },
                "human_gate_pending": human_gate_pending,
            }
        )

    return obligations


def load_capabilities_and_packs(
    config_path: str | Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], str]:
    """Read a trusted mergeproof.yaml -> (capabilities, packs, service)."""
    config_path = Path(config_path)
    text = config_path.read_text(encoding="utf-8", errors="replace")
    capabilities = capabilities_from_config(text)
    data = parse_yaml_subset(text)
    service = ""
    if isinstance(data, dict) and isinstance(data.get("service"), str):
        service = data["service"]
    packs: dict[str, dict[str, Any]] = {}
    for cap_type, entry in capabilities.items():
        template = config_path.parent / entry["template"]
        packs[cap_type] = load_pack(template.read_text(encoding="utf-8", errors="replace"))
    return capabilities, packs, service


def _read_changed(path: str | Path) -> list[Any]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass
    return [line.strip() for line in text.splitlines() if line.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect production infra obligations from a PR's changed files."
    )
    parser.add_argument("--mergeproof", required=True, help="Path to trusted mergeproof.yaml.")
    parser.add_argument(
        "--changed-files",
        required=True,
        help="Path to changed files (JSON list of {path,status} or newline-delimited paths).",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit obligations JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        capabilities, packs, service = load_capabilities_and_packs(args.mergeproof)
        changed = _read_changed(args.changed_files)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    obligations = detect_obligations(changed, capabilities, packs, service=service)
    result = {"service": service, "obligations": obligations}

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        for ob in obligations:
            approver = (ob.get("pack") or {}).get("approver") or "—"
            print(f"  [{ob['outcome'].upper()}] {ob['type']} (approver {approver})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Capability Pack model: the approved, reusable template for an infra change.

A Capability Pack declares how one class of production change is made — what it
generates, what checks gate it, who approves it, and whether any step is a
human-only gate (e.g. provisioning a secret value). Packs are the *only* place
generation is allowed to happen; an obligation with no matching pack is blocked.

Packs are authored as block-style YAML and parsed with the same zero-dependency
loader used for mergeproof config (``parse_yaml_subset``). The inline flow form
shown in the design doc (``inputs: { a: required }``) is illustrative only — the
loader is block-only by design, so the fixtures use the block form.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mergeproof_config import MergeProofConfigError, parse_yaml_subset


class CapabilityPackError(ValueError):
    """Raised for a malformed or incomplete Capability Pack."""


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityPackError(f"capability pack requires a '{field}' string")
    return value.strip()


def _require_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CapabilityPackError(f"capability pack requires a non-empty '{field}' list")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise CapabilityPackError(f"'{field}' must be a list of non-empty strings")
    return [item.strip() for item in value]


def load_pack(text: str) -> dict[str, Any]:
    """Parse and validate a single Capability Pack from block YAML text."""
    try:
        data = parse_yaml_subset(text)
    except MergeProofConfigError as exc:
        raise CapabilityPackError(str(exc)) from exc
    if not isinstance(data, dict):
        raise CapabilityPackError("capability pack must be a mapping")

    capability = _require_str(data.get("capability"), "capability")
    generates = _require_str_list(data.get("generates"), "generates")
    checks = _require_str_list(data.get("checks"), "checks")

    approval = data.get("approval")
    if not isinstance(approval, dict):
        raise CapabilityPackError("capability pack requires an 'approval' mapping")
    required_from = _require_str_list(approval.get("required_from"), "required_from")

    inputs = data.get("inputs")
    if inputs is not None and not isinstance(inputs, dict):
        raise CapabilityPackError("'inputs' must be a mapping when present")

    human_gate = data.get("human_gate")
    if human_gate is not None and not isinstance(human_gate, str):
        raise CapabilityPackError("'human_gate' must be a string when present")

    return {
        "capability": capability,
        "inputs": dict(inputs) if isinstance(inputs, dict) else {},
        "generates": generates,
        "checks": checks,
        "approval": {"required_from": required_from},
        "human_gate": human_gate.strip() if isinstance(human_gate, str) else None,
        "template_map": data.get("template_map") if isinstance(data.get("template_map"), dict) else {},
    }


def pack_approver(pack: dict[str, Any]) -> str:
    """Return the @-prefixed primary approver for a pack."""
    required_from = pack["approval"]["required_from"]
    handle = required_from[0].lstrip("@")
    return f"@{handle}"


def capabilities_from_config(config_text: str) -> dict[str, dict[str, Any]]:
    """Extract the ``capabilities:`` declaration from a mergeproof.yaml.

    Returns a mapping of capability ``type`` -> ``{type, template, approver}``.
    This is read directly (not through ``validate_config``) so the well-tested
    config contract stays untouched while the demo gains the capability map.
    """
    try:
        data = parse_yaml_subset(config_text)
    except MergeProofConfigError as exc:
        raise CapabilityPackError(str(exc)) from exc
    if not isinstance(data, dict):
        raise CapabilityPackError("mergeproof config must be a mapping")
    entries = data.get("capabilities") or []
    if not isinstance(entries, list):
        raise CapabilityPackError("'capabilities' must be a list when present")
    result: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise CapabilityPackError("each capability entry must be a mapping")
        cap_type = _require_str(entry.get("type"), "type")
        template = _require_str(entry.get("template"), "template")
        approver = entry.get("approver")
        approver = approver.strip() if isinstance(approver, str) and approver.strip() else None
        result[cap_type] = {
            "type": cap_type,
            "template": template,
            "approver": approver,
        }
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate a Capability Pack YAML file.")
    parser.add_argument("pack", help="Path to a capability pack YAML file.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        pack = load_pack(Path(args.pack).read_text(encoding="utf-8", errors="replace"))
    except (OSError, CapabilityPackError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json_output:
        print(json.dumps(pack, indent=2, sort_keys=True))
    else:
        print(f"{pack['capability']}: generates {', '.join(pack['generates'])} "
              f"(approver {pack_approver(pack)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

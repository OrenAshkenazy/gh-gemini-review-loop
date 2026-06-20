#!/usr/bin/env python3
"""Compose env reads + infra-precedent classification into obligations.

Routes a ``config`` classification to the ``runtime_config`` capability and a
``secret`` classification to ``secret_wiring``. An ``unknown`` classification
becomes a human-gated ``env_classification`` obligation carrying the cited
reason and an advisory name-pattern suggestion (advisory only — never authority).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from env_precedent import classify_env
from env_reads import detect_env_reads
from pr_obligations import assemble_obligation, load_capabilities_and_packs
from queue_resource import detect_queue_resource

# Advisory-only suggestion shown inside the human gate when precedent is absent.
_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIALS", "_DSN")
_CONFIG_SUFFIXES = ("_URL", "_HOST", "_NAME", "_PORT", "_ENDPOINT")

# Name segments that mark a variable as naming a queue/topic.
_QUEUE_TOKENS = ("QUEUE", "TOPIC")


def _advisory_suggestion(name: str) -> str:
    if name.endswith(_SECRET_SUFFIXES):
        return "secret"
    if name.endswith(_CONFIG_SUFFIXES):
        return "config"
    return "unknown"


def _is_queue_ish(read: dict[str, Any]) -> bool:
    """A worker-scope read whose name carries a queue/topic token segment."""
    if read.get("scope") != "worker":
        return False
    return any(tok in read["name"].split("_") for tok in _QUEUE_TOKENS)


def _routes_for_read(
    read: dict[str, Any],
    infra_files: dict[str, str],
    capabilities: dict[str, dict[str, Any]],
    packs: dict[str, dict[str, Any]],
    service: str,
) -> list[dict[str, Any]]:
    """Resolve a single env read into a *list* of obligation routes (0..n).

    This is the fan-out seam: today each read resolves to exactly one route,
    but the list shape lets a single read imply several production obligations
    (e.g. a queue-ish variable -> ``runtime_config`` *and* ``queue_topic``).
    Each route is a self-contained spec for one obligation.
    """
    name = read["name"]
    classification = classify_env(name, infra_files, service)
    cap_type = classification["capability"]

    if cap_type is None:
        # unknown precedent -> human gate to classify, with an advisory suggestion.
        return [
            {
                "type": "env_classification",
                "capability": {"approver": None},
                "pack": {
                    "inputs": {},
                    "human_gate": f"classify {name} as secret or config",
                    "generates": [],
                    "checks": [],
                    "approval": {},
                },
                "extra": {
                    "classification": classification,
                    "advisory_suggestion": _advisory_suggestion(name),
                },
            }
        ]

    capability = capabilities.get(cap_type)
    routes = [
        {
            "type": cap_type,
            "capability": capability,
            "pack": packs.get(cap_type) if capability is not None else None,
            "extra": {"classification": classification},
        }
    ]

    # Fan-out: a queue-ish worker-scope variable that wires as config also needs
    # its queue/topic resource — but only when none is provisioned for the
    # workload. The gate is structural (resource presence), never the var value.
    if cap_type == "runtime_config" and _is_queue_ish(read):
        resource = detect_queue_resource(infra_files, service)
        if not resource["present"]:
            qt_cap = capabilities.get("queue_topic")
            routes.append(
                {
                    "type": "queue_topic",
                    "capability": qt_cap,
                    "pack": packs.get("queue_topic") if qt_cap is not None else None,
                    "extra": {
                        "classification": {
                            "classification": "queue_absent",
                            "capability": "queue_topic",
                            "precedent_scope": "workload",
                            "evidence_files": resource["inspected_paths"],
                            "reason": (
                                f"{name}: no provisioned queue/topic resource for "
                                f"{service or 'the workload'}; inspected "
                                f"{len(resource['inspected_paths'])} infra path(s)"
                            ),
                        },
                        "resource_absence": resource,
                    },
                }
            )

    return routes


def _k8s_name(env_name: str) -> str:
    """Normalize an env var name to an RFC1123 label fragment: lowercase, no _,
    bounded to the 63-char label limit (trailing '-' trimmed after truncation)."""
    slug = re.sub(r"[^a-z0-9]+", "-", env_name.lower()).strip("-")
    return slug[:63].rstrip("-")


def _obligations_for_read(
    read: dict[str, Any],
    routes: list[dict[str, Any]],
    service: str,
) -> list[dict[str, Any]]:
    """Assemble one obligation per route, all citing this read's evidence."""
    base_inputs = {"env_name": read["name"], "service": service, "scope": read["scope"]}
    obligations: list[dict[str, Any]] = []
    for route in routes:
        inputs = dict(base_inputs)
        # runtime_config renders a ConfigMap whose metadata.name must be a valid,
        # per-variable RFC1123 name, or two generated ConfigMaps collide on apply.
        if route["type"] == "runtime_config":
            inputs["config_name"] = _k8s_name(read["name"])
        obligations.append(
            assemble_obligation(
                route["type"],
                [read["source_file"]],
                inputs,
                route["capability"],
                route["pack"],
                extra=route["extra"],
            )
        )
    return obligations


def detect_env_obligations(
    changed_content: dict[str, str],
    infra_files: dict[str, str],
    capabilities: dict[str, dict[str, Any]],
    packs: dict[str, dict[str, Any]],
    service: str = "",
) -> list[dict[str, Any]]:
    """Return classified env obligations for the changed Python files."""
    obligations: list[dict[str, Any]] = []
    for read in detect_env_reads(changed_content):
        routes = _routes_for_read(read, infra_files, capabilities, packs, service)
        obligations.extend(_obligations_for_read(read, routes, service))
    return obligations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect classified env obligations.")
    parser.add_argument("--changed-content", required=True, help="JSON {path: content} for changed files.")
    parser.add_argument("--infra-files", required=True, help="JSON {path: content} for infra files.")
    parser.add_argument("--mergeproof", required=True, help="Path to trusted mergeproof.yaml.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        changed = json.loads(Path(args.changed_content).read_text(encoding="utf-8", errors="replace"))
        infra = json.loads(Path(args.infra_files).read_text(encoding="utf-8", errors="replace"))
        capabilities, packs, service = load_capabilities_and_packs(args.mergeproof)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    obligations = detect_env_obligations(
        changed if isinstance(changed, dict) else {},
        infra if isinstance(infra, dict) else {},
        capabilities,
        packs,
        service=service,
    )
    if args.json_output:
        print(json.dumps({"service": service, "obligations": obligations}, indent=2, sort_keys=True))
    else:
        for ob in obligations:
            print(f"  [{ob['outcome'].upper()}] {ob['type']} {ob['inputs'].get('env_name')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

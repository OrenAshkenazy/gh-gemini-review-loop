#!/usr/bin/env python3
"""Structural resource-absence check for the queue_topic fan-out gate.

Scans the in-process infra slice for a queue/topic resource provisioned for the
workload — a Terraform ``aws_sqs_queue`` / ``aws_sns_topic`` or a declared queue
block in Helm values. The decision is driven purely by **structural presence**,
never by an env var's value, and the result records the infra **paths** it
inspected as evidence — it never copies infra content.

Present  -> the queue already exists, no queue_topic obligation.
Absent   -> the workload would start against a non-existent queue; emit
            queue_topic so the resource is provisioned.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Terraform queue/topic resources, or a top-level Helm `sqs:`/`sns:`/`queue:`/
# `topic:` block declaring a provisioned queue. Env entries like
# ``WORKER_QUEUE_NAME:`` do not match — the key must *be* the queue marker.
_RESOURCE_MARKERS = (
    re.compile(r'resource\s+"aws_sqs_queue"'),
    re.compile(r'resource\s+"aws_sns_topic"'),
    re.compile(r"(?im)^\s*(?:sqs|sns|queue|topic)s?\s*:"),
)


def detect_queue_resource(infra_files: dict[str, str], service: str) -> dict[str, Any]:
    """Return ``{present, inspected_paths}`` for the workload's queue resource.

    Only workload-scoped infra paths (those naming *service*) are inspected, so
    the verdict is bound to the workload. ``inspected_paths`` is the cited
    evidence — the paths scanned, never their content.
    """
    inspected = sorted(p for p in infra_files if service and service in p)
    present = any(
        marker.search(infra_files[path])
        for path in inspected
        for marker in _RESOURCE_MARKERS
    )
    return {"present": present, "inspected_paths": inspected}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Structural queue/topic resource-absence check.")
    parser.add_argument("--service", required=True, help="Workload service name.")
    parser.add_argument("--infra-files", required=True, help="JSON {path: content} for infra files.")
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
    print(json.dumps(detect_queue_resource(infra, args.service), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

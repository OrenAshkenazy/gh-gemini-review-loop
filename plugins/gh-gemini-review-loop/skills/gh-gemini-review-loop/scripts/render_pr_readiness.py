#!/usr/bin/env python3
"""Render a production-aware PR Readiness Card.

Combines three inputs into one decision-ready artifact:

* the AI review loop summary (fixes, verification, re-review, cycles)
* the static architecture context (service, exposure, data, async)
* the PR's production risks (surfaces a human should review)

Outputs either polished GitHub-flavored Markdown (``--markdown``) or the
canonical readiness JSON (``--json``) consumed by the demo UI renderer.

The card is advisory: it states evidence and a recommended decision but never
merges or blocks. The AI runs the loop; the human owns production risk.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Stable hidden marker so the published PR comment updates in place.
READINESS_MARKER = "<!-- mergeproof-pr-readiness -->"

STATUS_LABELS = {
    "VERIFICATION_FAILED": "VERIFICATION FAILED",
    "CONFIG_CHANGED_REVIEW_REQUIRED": "CONFIG CHANGED - REVIEW REQUIRED",
    "HUMAN_DECISION_REQUIRED": "HUMAN DECISION REQUIRED",
    "PENDING_CONFIRMATION": "PENDING CONFIRMATION",
    "READY": "READY",
}

_DATASTORE_LABELS = {
    "postgresql": "PostgreSQL",
    "redis": "Redis",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "dynamodb": "DynamoDB",
    "elasticsearch": "Elasticsearch",
}

_INGRESS_LABELS = {
    "alb": "ALB",
    "kong": "Kong",
    "ingress": "Ingress",
    "load_balancer": "LoadBalancer",
}

_SURFACE_LABELS = {
    "public_api": "Public API",
    "auth_security": "Auth / user behavior",
    "async_processing": "Async processing",
    "database_behavior": "Database behavior",
    "infrastructure": "Infrastructure",
}

_REVIEW_POINTS = {
    "public_api": "API behavior, contract, and error handling",
    "auth_security": "auth / user semantics and access control",
    "async_processing": "worker retry and duplicate-processing behavior",
    "database_behavior": "migration safety and query/data behavior",
    "infrastructure": "infrastructure change blast radius",
}

_NEXT_OPTIONS = {
    "VERIFICATION_FAILED": [
        "Ask the AI to fix the failing verification",
        "Inspect the failing checks locally",
        "Hold the PR until verification passes",
    ],
    "CONFIG_CHANGED_REVIEW_REQUIRED": [
        "Review the MergeProof config change before merge",
        "Confirm the new infra paths are intended",
        "Merge only after the config change is approved",
    ],
    "HUMAN_DECISION_REQUIRED": [
        "Approve the production risk and merge",
        "Ask AI to adjust the implementation",
        "Split risky behavior into a follow-up PR",
    ],
    "PENDING_CONFIRMATION": [
        "Wait for Gemini to re-confirm the latest changes",
        "Re-request review",
        "Merge if confident the applied fixes are complete",
    ],
    "READY": [
        "Merge the PR",
        "Request one more review pass",
        "Hold for manual QA if desired",
    ],
}

_REASONS = {
    "VERIFICATION_FAILED": (
        "Verification failed during the AI review loop; this PR is not ready to merge."
    ),
    "CONFIG_CHANGED_REVIEW_REQUIRED": (
        "This PR modifies the MergeProof config. The base-branch config was used "
        "for this run; review the config change before it affects production "
        "context resolution."
    ),
    "HUMAN_DECISION_REQUIRED_RISK": (
        "AI review loop completed and tests passed, but this PR touches "
        "production-facing surfaces."
    ),
    "HUMAN_DECISION_REQUIRED_SEMANTIC": (
        "AI review loop completed and tests passed, but a semantic risk was "
        "flagged for human review."
    ),
    "PENDING_CONFIRMATION": (
        "AI review loop completed and fixes were applied, but Gemini has not "
        "re-confirmed the latest changes."
    ),
    "READY": (
        "AI review loop completed, tests passed, and no production-facing "
        "surfaces were affected."
    ),
}


def _as_bool(value: Any) -> bool:
    return bool(value)


def _unwrap_architecture(architecture: dict[str, Any]) -> tuple[dict, dict | None, dict]:
    """Accept either a flat facts dict or a full Production Context Pack."""
    if isinstance(architecture, dict) and "facts" in architecture and "provenance" in architecture:
        facts = dict(architecture.get("facts") or {})
        if facts.get("service_name") in (None, "", "unknown") and architecture.get("service"):
            facts["service_name"] = architecture["service"]
        return (
            facts,
            architecture.get("provenance"),
            architecture.get("safety") or {},
        )
    return architecture or {}, None, {}


def build_readiness(
    loop_summary: dict[str, Any],
    architecture: dict[str, Any],
    production_risks: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the canonical, deterministic readiness data structure."""
    loop_summary = loop_summary or {}
    architecture = architecture or {}
    production_risks = production_risks or {}
    architecture, provenance, safety = _unwrap_architecture(architecture)

    risks = production_risks.get("production_risks")
    risks = risks if isinstance(risks, list) else []
    risk_summary = production_risks.get("summary")
    risk_summary = risk_summary if isinstance(risk_summary, dict) else {}
    verification = str(loop_summary.get("verification") or "unknown").lower()
    semantic_risk = _as_bool(loop_summary.get("semantic_risk"))
    pending = _as_bool(loop_summary.get("pending_confirmation"))
    human_required = _as_bool(risk_summary.get("human_decision_required")) or any(
        r.get("human_decision_required") for r in risks
    )

    if verification == "failed":
        status = "VERIFICATION_FAILED"
        reason = _REASONS["VERIFICATION_FAILED"]
    elif safety.get("config_changed"):
        status = "CONFIG_CHANGED_REVIEW_REQUIRED"
        reason = _REASONS["CONFIG_CHANGED_REVIEW_REQUIRED"]
    elif human_required:
        status = "HUMAN_DECISION_REQUIRED"
        reason = _REASONS["HUMAN_DECISION_REQUIRED_RISK"]
    elif semantic_risk:
        status = "HUMAN_DECISION_REQUIRED"
        reason = _REASONS["HUMAN_DECISION_REQUIRED_SEMANTIC"]
    elif pending:
        status = "PENDING_CONFIRMATION"
        reason = _REASONS["PENDING_CONFIRMATION"]
    else:
        status = "READY"
        reason = _REASONS["READY"]

    # De-duplicated, order-stable review points from the risk surfaces present.
    review_points: list[str] = []
    for risk in risks:
        point = _REVIEW_POINTS.get(risk.get("surface", ""))
        if point and point not in review_points:
            review_points.append(point)
    if semantic_risk and not review_points:
        review_points.append("semantic behavior change flagged during review")

    # If the service handles credentials/certs/banking secrets and the PR touches
    # connector or async surfaces, call out sensitive-data-in-logs explicitly.
    secret_names = architecture.get("secrets_or_env") or []
    sensitive = any(
        any(tok in name.upper() for tok in ("CERT", "CLIENT_ID", "CLIENT_SECRET", "CREDENTIAL", "PRIVATE_KEY"))
        for name in secret_names
    )
    risky_surfaces = {r.get("surface") for r in risks}
    if review_points and sensitive and ({"public_api", "async_processing"} & risky_surfaces):
        review_points.append(
            "whether sensitive credential/banking data could leak in connector or worker logs"
        )

    evidence = {
        "findings_fixed": int(loop_summary.get("fixed_count") or 0),
        "false_positives_skipped": int(loop_summary.get("false_positives_skipped") or 0),
        "verification": verification,
        "verification_command": loop_summary.get("verification_command") or "",
        "rereview": str(loop_summary.get("rereview") or "unknown"),
        "cycles_used": loop_summary.get("cycles_used"),
        "cycles_total": loop_summary.get("cycles_total"),
    }

    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "reason": reason,
        "pr_url": loop_summary.get("pr_url") or architecture.get("pr_url"),
        "evidence": evidence,
        "architecture": {
            "service_name": architecture.get("service_name") or "unknown",
            "owners": list(architecture.get("owners") or []),
            "runtime": architecture.get("runtime") or "unknown",
            "exposure": architecture.get("exposure") or "unknown",
            "ingress": list(architecture.get("ingress") or []),
            "datastores": list(architecture.get("datastores") or []),
            "queues": list(architecture.get("queues") or []),
        },
        "production_risks": risks,
        "provenance": provenance,
        "safety": safety,
        "risk_summary": {
            "highest_severity": risk_summary.get("highest_severity", "none"),
            "human_decision_required": human_required,
            "risk_count": risk_summary.get("risk_count", len(risks)),
        },
        "human_decision": {
            "required": status in ("HUMAN_DECISION_REQUIRED", "CONFIG_CHANGED_REVIEW_REQUIRED"),
            "review_points": review_points,
        },
        "next_options": _NEXT_OPTIONS[status],
    }


# ---- presentation helpers -------------------------------------------------


def _pretty_datastores(datastores: list[str]) -> str:
    return ", ".join(_DATASTORE_LABELS.get(d, d.title()) for d in datastores) or "none"


def _pretty_ingress(ingress: list[str]) -> str:
    if not ingress:
        return "none"
    labels = [_INGRESS_LABELS.get(i, i.title()) for i in ingress]
    return " → ".join([*labels, "service"])


def _pretty_queues(queues: list[str]) -> str:
    parts: list[str] = []
    for queue in queues:
        if ":" in queue:
            proto, name = queue.split(":", 1)
            parts.append(f"{proto.upper()} `{name}`")
        else:
            parts.append(queue.upper())
    return ", ".join(parts) or "none"


def _verification_cell(evidence: dict[str, Any]) -> str:
    command = evidence.get("verification_command")
    result = evidence.get("verification", "unknown")
    if command:
        return f"`{command}` {result}"
    return result


def _cycles_cell(evidence: dict[str, Any]) -> str:
    used, total = evidence.get("cycles_used"), evidence.get("cycles_total")
    if used is not None and total is not None:
        return f"{used}/{total}"
    if used is not None:
        return str(used)
    return "unknown"


def _rereview_cell(value: str) -> str:
    mapping = {"completed": "Gemini completed", "timed_out": "Gemini did not re-confirm"}
    return mapping.get(value, value)


def render_markdown(readiness: dict[str, Any]) -> str:
    arch = readiness["architecture"]
    evidence = readiness["evidence"]
    lines: list[str] = []

    lines.append(READINESS_MARKER)
    lines.append("## MergeProof PR Readiness")
    lines.append("")
    lines.append(f"**Status:** {readiness['status_label']}  ")
    lines.append(f"**Reason:** {readiness['reason']}")
    if readiness.get("pr_url"):
        lines.append("")
        lines.append(f"PR: {readiness['pr_url']}")
    lines.append("")

    lines.append("### Merge evidence")
    lines.append("")
    lines.append("| Signal | Result |")
    lines.append("|---|---|")
    lines.append(f"| AI findings fixed | {evidence['findings_fixed']} |")
    lines.append(f"| False positives skipped | {evidence['false_positives_skipped']} |")
    lines.append(f"| Verification | {_verification_cell(evidence)} |")
    lines.append(f"| Re-review | {_rereview_cell(evidence['rereview'])} |")
    lines.append(f"| Cycles used | {_cycles_cell(evidence)} |")
    lines.append("")

    lines.append("### Production architecture context")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Service | `{arch['service_name']}` |")
    lines.append(f"| Runtime | {arch['runtime'].title()} |")
    lines.append(f"| Exposure | {arch['exposure'].title()} |")
    lines.append(f"| Ingress | {_pretty_ingress(arch['ingress'])} |")
    lines.append(f"| Data | {_pretty_datastores(arch['datastores'])} |")
    lines.append(f"| Async | {_pretty_queues(arch['queues'])} |")
    lines.append(f"| Owner | {', '.join(arch['owners']) or 'unknown'} |")
    provenance = readiness.get("provenance")
    if provenance and provenance.get("sources"):
        source = provenance["sources"][0]
        sha = (source.get("resolved_sha") or "")[:7]
        lines.append(
            f"| Production context | {provenance.get('file_count', 0)} files from "
            f"`{source.get('repo', '')}@{sha}` |"
        )
    lines.append("")

    lines.append("### Production risks")
    lines.append("")
    if readiness["production_risks"]:
        lines.append("| Severity | Surface | Evidence |")
        lines.append("|---|---|---|")
        for risk in readiness["production_risks"]:
            surface = _SURFACE_LABELS.get(risk.get("surface", ""), risk.get("surface", ""))
            files = ", ".join(f"`{f}`" for f in risk.get("files", []))
            evidence_cell = f"{files} — {risk.get('reason', '')}".strip(" —")
            lines.append(f"| {risk['severity'].title()} | {surface} | {evidence_cell} |")
    else:
        lines.append("No production-facing surfaces were changed by this PR.")
    lines.append("")

    lines.append("### Human decision required")
    lines.append("")
    if readiness["human_decision"]["required"]:
        lines.append(
            "Tests passed and AI review findings were fixed, but this PR changes "
            "code mapped to production-facing behavior."
        )
        if readiness["human_decision"]["review_points"]:
            lines.append("")
            lines.append("Review before merge:")
            for i, point in enumerate(readiness["human_decision"]["review_points"], 1):
                lines.append(f"{i}. {point}")
    else:
        lines.append(readiness["reason"])
    lines.append("")

    lines.append("### Recommended next options")
    lines.append("")
    for i, option in enumerate(readiness["next_options"], 1):
        lines.append(f"{i}. {option}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _load_json(path: str, label: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError(f"{label} must be a JSON object")
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render a production-aware PR Readiness Card."
    )
    parser.add_argument("--loop-summary", required=True, help="Path to loop summary JSON.")
    parser.add_argument(
        "--architecture-context", required=True, help="Path to architecture context JSON."
    )
    parser.add_argument(
        "--production-risks", required=True, help="Path to production risks JSON."
    )
    out = parser.add_mutually_exclusive_group()
    out.add_argument("--markdown", action="store_true", help="Render GitHub Markdown.")
    out.add_argument(
        "--json", action="store_true", dest="json_output", help="Emit readiness JSON."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        loop_summary = _load_json(args.loop_summary, "loop summary")
        architecture = _load_json(args.architecture_context, "architecture context")
        risks = _load_json(args.production_risks, "production risks")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    readiness = build_readiness(loop_summary, architecture, risks)

    if args.json_output:
        print(json.dumps(readiness, indent=2, sort_keys=True))
    else:
        print(render_markdown(readiness), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

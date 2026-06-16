#!/usr/bin/env python3
"""Render the readiness data as a single polished, static HTML report.

Produces one self-contained HTML file with embedded CSS, no JavaScript, and no
external network assets — safe to open from disk and clean on a screen share.
It consumes the same canonical readiness JSON emitted by
``render_pr_readiness.py --json`` so the demo never invents data.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

from render_pr_readiness import (
    _DATASTORE_LABELS,
    _INGRESS_LABELS,
    _SURFACE_LABELS,
)

_STATUS_THEME = {
    "READY": ("#16a34a", "ready"),
    "HUMAN_DECISION_REQUIRED": ("#d97706", "decision"),
    "CONFIG_CHANGED_REVIEW_REQUIRED": ("#ea580c", "config"),
    "PENDING_CONFIRMATION": ("#2563eb", "pending"),
    "VERIFICATION_FAILED": ("#dc2626", "failed"),
}

_SEVERITY_CLASS = {"high": "sev-high", "medium": "sev-medium", "low": "sev-low"}

_FLOW_OUTCOME_CLASS = {"matched": "node service", "human_gated": "node exposure", "blocked": "node"}

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 0 48px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: #0b1220; color: #e6edf6; line-height: 1.5;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 0 24px; }
header.top { padding: 32px 0 20px; border-bottom: 1px solid #1e293b; }
header.top h1 { margin: 0; font-size: 24px; letter-spacing: -0.01em; }
header.top p.sub { margin: 6px 0 0; color: #94a3b8; font-size: 15px; }
.banner {
  margin: 24px 0; padding: 20px 24px; border-radius: 14px;
  background: linear-gradient(135deg, #111c30, #0e1830);
  border: 1px solid #1e293b; border-left: 6px solid var(--accent);
}
.banner .status { font-size: 22px; font-weight: 700; color: var(--accent); letter-spacing: 0.02em; }
.banner .reason { margin-top: 6px; color: #cbd5e1; font-size: 15px; }
.banner .strip-line { margin-top: 10px; color: #94a3b8; font-size: 14px; }
.cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin: 24px 0; }
.card {
  background: #0f1a2e; border: 1px solid #1e293b; border-radius: 12px;
  padding: 14px 16px; min-height: 78px;
}
.card .label { color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
.card .value { margin-top: 6px; font-size: 20px; font-weight: 700; }
.card .value.sm { font-size: 15px; font-weight: 600; }
h2.section { font-size: 15px; text-transform: uppercase; letter-spacing: 0.05em;
  color: #94a3b8; margin: 32px 0 12px; }
.arch {
  background: #0f1a2e; border: 1px solid #1e293b; border-radius: 12px; padding: 20px 22px;
  font-size: 16px; display: flex; flex-direction: column; gap: 12px;
}
.flow { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.node { background: #15233c; border: 1px solid #243450; border-radius: 8px; padding: 6px 12px; font-weight: 600; }
.node.service { background: #1d3a5f; border-color: #2f5a8f; }
.node.exposure { background: #3a2417; border-color: #7c4a1e; color: #fbbf24; }
.arrow { color: #64748b; font-size: 18px; }
.flow.sub { color: #94a3b8; padding-left: 6px; }
table { width: 100%; border-collapse: collapse; background: #0f1a2e;
  border: 1px solid #1e293b; border-radius: 12px; overflow: hidden; }
th, td { text-align: left; padding: 12px 16px; border-bottom: 1px solid #1a2740; font-size: 14px; }
th { color: #94a3b8; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.04em; }
tr:last-child td { border-bottom: none; }
td code, .arch code { background: #14233d; padding: 2px 6px; border-radius: 5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
.sev { font-weight: 700; padding: 2px 10px; border-radius: 999px; font-size: 12px; }
.sev-high { background: #3b1518; color: #fca5a5; }
.sev-medium { background: #3a2c11; color: #fcd34d; }
.sev-low { background: #1c2b22; color: #86efac; }
.decision { background: #0f1a2e; border: 1px solid #1e293b; border-left: 6px solid var(--accent);
  border-radius: 12px; padding: 20px 22px; }
.decision p { margin: 0 0 12px; color: #cbd5e1; }
.decision ol { margin: 0; padding-left: 20px; }
.decision li { margin: 6px 0; }
.points { color: #94a3b8; font-size: 14px; margin-top: 8px; }
.context-grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: 14px; margin-top: 12px; }
.context-panel {
  background: #101b2c; border: 1px solid #27364d; border-radius: 10px;
  padding: 14px 16px; min-width: 0;
}
.context-panel .label { color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }
.context-panel .main { margin-top: 6px; font-size: 15px; font-weight: 650; color: #e6edf6; }
.context-panel .meta { margin-top: 4px; color: #9fb0c7; font-size: 13px; overflow-wrap: anywhere; }
.safety { background: #0f1a2e; border: 1px solid #27364d; border-radius: 12px; overflow: hidden; }
.safety-row { display: grid; grid-template-columns: 180px 1fr; gap: 12px; padding: 12px 16px; border-bottom: 1px solid #1a2740; }
.safety-row:last-child { border-bottom: none; }
.safety-row strong { color: #cbd5e1; font-size: 13px; }
.safety-row span { color: #9fb0c7; font-size: 13px; }
.warn { color: #fbbf24; font-weight: 650; }
footer { margin-top: 36px; padding-top: 18px; border-top: 1px solid #1e293b;
  color: #64748b; font-size: 13px; text-align: center; font-style: italic; }
a { color: #7dd3fc; }
@media (max-width: 760px) {
  .cards { grid-template-columns: repeat(2, 1fr); }
  .context-grid { grid-template-columns: 1fr; }
  .safety-row { grid-template-columns: 1fr; gap: 4px; }
}
.tabs { display: flex; gap: 6px; margin: 24px 0 0; border-bottom: 1px solid #1e293b; flex-wrap: wrap; }
.tabs label { padding: 10px 16px; cursor: pointer; color: #94a3b8; font-weight: 600; font-size: 14px; border-bottom: 2px solid transparent; }
.tabnav { display: none; }
.tabpanel { display: none; padding-top: 8px; }
#t-readiness:checked ~ .tabs label[for="t-readiness"],
#t-flow:checked ~ .tabs label[for="t-flow"],
#t-resolve:checked ~ .tabs label[for="t-resolve"],
#t-audit:checked ~ .tabs label[for="t-audit"],
#t-packs:checked ~ .tabs label[for="t-packs"] { color: #e6edf6; border-bottom-color: var(--accent); }
#t-readiness:checked ~ #tab-readiness,
#t-flow:checked ~ #tab-flow,
#t-resolve:checked ~ #tab-resolve,
#t-audit:checked ~ #tab-audit,
#t-packs:checked ~ #tab-packs { display: block; }
.obl-grid { display: grid; gap: 10px; }
.obl-node { background: #0f1a2e; border: 1px solid #1e293b; border-radius: 10px; padding: 12px 14px; }
.obl-meta { color: #94a3b8; font-size: 13px; margin-top: 6px; }
.resolve-card { background: #0f1a2e; border: 1px solid #1e293b; border-radius: 12px; padding: 16px 18px; margin-bottom: 14px; }
.resolve-head { font-weight: 700; font-size: 15px; }
.diff-path { color: #7dd3fc; font-size: 13px; margin-top: 10px; font-family: ui-monospace, Menlo, monospace; }
pre.diff { background: #0a1322; border: 1px solid #1a2740; border-radius: 8px; padding: 12px 14px; overflow-x: auto; font-size: 12.5px; color: #cbd5e1; }
.btn { display: inline-block; margin-top: 12px; padding: 9px 16px; background: var(--accent); color: #0b1220; font-weight: 700; border-radius: 8px; text-decoration: none; }
.gate { margin-top: 12px; color: #fbbf24; font-weight: 650; }
.audit { color: #cbd5e1; font-size: 14px; line-height: 1.9; }
"""


def _e(value: Any) -> str:
    return html.escape(str(value))


def _arch_flow(arch: dict[str, Any]) -> str:
    nodes: list[str] = []
    exposure = (arch.get("exposure") or "unknown").title()
    if arch.get("exposure") == "public":
        nodes.append(f'<span class="node exposure">{_e(exposure)}</span>')
    for ing in arch.get("ingress") or []:
        label = _INGRESS_LABELS.get(ing, ing.title())
        nodes.append(f'<span class="node">{_e(label)}</span>')
    nodes.append(f'<span class="node service">{_e(arch.get("service_name") or "service")}</span>')
    stores = [
        _DATASTORE_LABELS.get(d, d.title()) for d in (arch.get("datastores") or [])
    ]
    if stores:
        nodes.append(f'<span class="node">{_e(" / ".join(stores))}</span>')
    flow = '<span class="arrow">→</span>'.join(nodes)
    return f'<div class="flow">{flow}</div>'


def _arch_async(arch: dict[str, Any]) -> str:
    queues = arch.get("queues") or []
    if not queues:
        return ""
    parts: list[str] = []
    for queue in queues:
        if ":" in queue:
            proto, name = queue.split(":", 1)
            parts.append(f'{_e(proto.upper())} <code>{_e(name)}</code>')
        else:
            parts.append(_e(queue.upper()))
    return (
        '<div class="flow sub"><span class="arrow">↓</span>'
        + " · ".join(parts)
        + "</div>"
    )


def _evidence_cards(evidence: dict[str, Any]) -> str:
    verification = evidence.get("verification", "unknown")
    command = evidence.get("verification_command")
    ver_value = f"{verification}" + (f"<br><span style='font-size:12px;color:#94a3b8'>{_e(command)}</span>" if command else "")
    used, total = evidence.get("cycles_used"), evidence.get("cycles_total")
    cycles = f"{used}/{total}" if used is not None and total is not None else (str(used) if used is not None else "—")
    cards = [
        ("Findings fixed", str(evidence.get("findings_fixed", 0)), ""),
        ("False positives skipped", str(evidence.get("false_positives_skipped", 0)), ""),
        ("Cycles used", cycles, ""),
        ("Verification", ver_value, "sm"),
        ("Re-review", _e(evidence.get("rereview", "unknown")), "sm"),
    ]
    out = []
    for label, value, cls in cards:
        out.append(
            f'<div class="card"><div class="label">{_e(label)}</div>'
            f'<div class="value {cls}">{value}</div></div>'
        )
    return "".join(out)


def _risk_rows(risks: list[dict[str, Any]]) -> str:
    if not risks:
        return '<tr><td colspan="3">No production-facing surfaces were changed by this PR.</td></tr>'
    rows = []
    for risk in risks:
        sev = risk.get("severity", "low")
        sev_class = _SEVERITY_CLASS.get(sev, "sev-low")
        surface = _SURFACE_LABELS.get(risk.get("surface", ""), risk.get("surface", ""))
        files = " ".join(f"<code>{_e(f)}</code>" for f in risk.get("files", []))
        evidence = f"{files} {_e(risk.get('reason', ''))}".strip()
        rows.append(
            f'<tr><td><span class="sev {sev_class}">{_e(sev.upper())}</span></td>'
            f"<td>{_e(surface)}</td><td>{evidence}</td></tr>"
        )
    return "".join(rows)


def _context_panels(readiness: dict[str, Any]) -> str:
    provenance = readiness.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    sources = provenance.get("sources")
    sources = sources if isinstance(sources, list) else []
    if not sources:
        return ""
    source = sources[0]
    if not isinstance(source, dict):
        return ""
    sha = (source.get("resolved_sha") or "")[:7] or "unknown"
    repo = source.get("repo") or "unknown"
    count = provenance.get("file_count", 0)
    fetched_at = provenance.get("fetched_at") or "unknown"
    files = source.get("files")
    file_list = ", ".join(files if isinstance(files, list) else [])
    if len(file_list) > 140:
        file_list = file_list[:137] + "..."
    return (
        '<div class="context-grid">'
        '<div class="context-panel">'
        '<div class="label">Production context pack</div>'
        f'<div class="main">{_e(count)} files from <code>{_e(repo)}@{_e(sha)}</code></div>'
        f'<div class="meta">Fetched at {_e(fetched_at)}</div>'
        "</div>"
        '<div class="context-panel">'
        '<div class="label">Evidence paths</div>'
        f'<div class="main">{_e(file_list or "none")}</div>'
        "</div>"
        "</div>"
    )


def _safety_panel(readiness: dict[str, Any]) -> str:
    safety = readiness.get("safety")
    safety = safety if isinstance(safety, dict) else {}
    if not safety:
        return ""
    skipped = safety.get("skipped") or []
    failed = safety.get("failed_sources") or []
    warnings: list[tuple[str, str]] = []
    if safety.get("config_changed"):
        warnings.append(("Config changed", "Base-branch config was used; review the PR config change."))
    if safety.get("tree_truncated"):
        warnings.append(("Tree truncated", "GitHub returned a partial infra tree listing."))
    if skipped:
        reasons = ", ".join(sorted({str(item.get("reason", "unknown")) for item in skipped if isinstance(item, dict)}))
        warnings.append(("Skipped files", f"{len(skipped)} skipped ({reasons or 'unknown reason'})."))
    if failed:
        repos = ", ".join(str(item.get("repo", "unknown")) for item in failed if isinstance(item, dict))
        warnings.append(("Failed sources", repos or f"{len(failed)} source(s) failed."))
    if not warnings:
        warnings.append(("Safety", "No config, fetch, or source warnings recorded."))
    rows = "".join(
        f'<div class="safety-row"><strong>{_e(label)}</strong><span>{_e(value)}</span></div>'
        for label, value in warnings
    )
    return f'<h2 class="section">Pack safety</h2><div class="safety">{rows}</div>'


def _flow_tab(readiness: dict[str, Any]) -> str:
    arch = readiness.get("architecture") or {}
    obligations = readiness.get("obligations") or []
    nodes = []
    for ob in obligations:
        pack = ob.get("pack") or {}
        primary = next((v for v in (ob.get("inputs") or {}).values() if v), ob.get("type", ""))
        cls = _FLOW_OUTCOME_CLASS.get(ob.get("outcome", ""), "node")
        approver = pack.get("approver") or "—"
        nodes.append(
            f'<div class="obl-node"><span class="{cls}">{_e(ob.get("type",""))}</span>'
            f'<div class="obl-meta">{_e(primary)} · {_e(ob.get("outcome",""))} · {_e(approver)}</div></div>'
        )
    obl_html = "".join(nodes) or '<p class="points">No production obligations detected.</p>'
    return (
        '<section class="tabpanel" id="tab-flow">'
        '<h2 class="section">Production flow</h2>'
        f'<div class="arch">{_arch_flow(arch)}{_arch_async(arch)}</div>'
        '<h2 class="section">Obligations on this change</h2>'
        f'<div class="obl-grid">{obl_html}</div>'
        '</section>'
    )


def _resolve_tab(readiness: dict[str, Any]) -> str:
    obligations = readiness.get("obligations") or []
    cards = []
    for ob in obligations:
        pack = ob.get("pack") or {}
        checks = ", ".join(pack.get("checks") or []) or "none"
        header = f'{_e(ob.get("type",""))} — {_e(ob.get("outcome",""))} · approver {_e(pack.get("approver") or "—")}'
        infra_pr = ob.get("infra_pr") or {}
        diff = infra_pr.get("diff") or {}
        diff_html = ""
        for path, content in diff.items():
            diff_html += f'<div class="diff-path">{_e(path)}</div><pre class="diff">{_e(content)}</pre>'
        if ob.get("human_gate_pending"):
            pending = ", ".join(ob["human_gate_pending"])
            action_html = f'<div class="gate">Needs a human before merge: {_e(pending)}</div>'
        elif infra_pr.get("create_url"):
            action_html = (
                f'<a class="btn" href="{_e(infra_pr["create_url"])}">Open infra PR ▸</a>'
                f'<div class="points">Branch <code>{_e(infra_pr.get("branch",""))}</code>'
                f'{" (pushed)" if infra_pr.get("pushed") else " (staged, dry-run)"}</div>'
            )
        else:
            action_html = '<div class="points">No approved capability — escalate to platform.</div>'
        cards.append(
            f'<div class="resolve-card"><div class="resolve-head">{header}</div>'
            f'<div class="points">Proof — checks: {_e(checks)}</div>'
            f'{diff_html}{action_html}</div>'
        )
    body = "".join(cards) or '<p class="points">Nothing to resolve.</p>'
    return f'<section class="tabpanel" id="tab-resolve"><h2 class="section">Resolve</h2>{body}</section>'


def _audit_tab(readiness: dict[str, Any]) -> str:
    ev = readiness.get("evidence") or {}
    rows = [f'<li>AI loop: {_e(ev.get("findings_fixed", 0))} fixed · verification {_e(ev.get("verification","unknown"))}</li>']
    for ob in readiness.get("obligations") or []:
        files = ", ".join(ob.get("evidence_files") or [])
        rows.append(f'<li>Obligation <code>{_e(ob.get("type",""))}</code> ({_e(ob.get("outcome",""))}) from {_e(files or "—")}</li>')
    return (
        '<section class="tabpanel" id="tab-audit"><h2 class="section">Audit trail</h2>'
        f'<ol class="audit">{"".join(rows)}</ol></section>'
    )


def _packs_tab(readiness: dict[str, Any]) -> str:
    seen: dict[str, dict[str, Any]] = {}
    for ob in readiness.get("obligations") or []:
        pack = ob.get("pack")
        if pack and ob.get("type") not in seen:
            seen[ob["type"]] = pack
    if not seen:
        return '<section class="tabpanel" id="tab-packs"><h2 class="section">Capability packs</h2><p class="points">No capability packs referenced.</p></section>'
    rows = ""
    for cap_type, pack in seen.items():
        rows += (
            f'<tr><td><code>{_e(cap_type)}</code></td>'
            f'<td>{_e(", ".join(pack.get("generates") or []))}</td>'
            f'<td>{_e(", ".join(pack.get("checks") or []))}</td>'
            f'<td>{_e(pack.get("approver") or "—")}</td>'
            f'<td>{_e(pack.get("human_gate") or "—")}</td></tr>'
        )
    return (
        '<section class="tabpanel" id="tab-packs"><h2 class="section">Capability packs</h2>'
        '<table><thead><tr><th>Type</th><th>Generates</th><th>Checks</th><th>Approver</th><th>Human gate</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></section>'
    )


def render_html(readiness: dict[str, Any]) -> str:
    status = readiness.get("status", "READY")
    accent, _ = _STATUS_THEME.get(status, ("#64748b", "neutral"))
    arch = readiness.get("architecture") or {}
    evidence = readiness.get("evidence") or {}
    risks = readiness.get("production_risks") or []
    decision = readiness.get("human_decision") or {}
    options = readiness.get("next_options") or []

    hero_stats = []
    if evidence.get("findings_fixed") is not None:
        hero_stats.append(f"{evidence['findings_fixed']} fixes")
    if evidence.get("verification") == "passed":
        hero_stats.append("tests passed")
    if readiness.get("risk_summary", {}).get("risk_count"):
        hero_stats.append("production risk detected")
    strip = " · ".join(hero_stats)

    pr_link = ""
    if readiness.get("pr_url"):
        pr_link = f'<div class="strip-line">PR: <a href="{_e(readiness["pr_url"])}">{_e(readiness["pr_url"])}</a></div>'

    points_html = ""
    if decision.get("review_points"):
        items = "".join(f"<li>{_e(p)}</li>" for p in decision["review_points"])
        points_html = f'<div class="points">Review before merge:<ol>{items}</ol></div>'

    options_html = "".join(f"<li>{_e(o)}</li>" for o in options)

    tabs_nav = (
        '<input class="tabnav" type="radio" name="tab" id="t-readiness" checked>'
        '<input class="tabnav" type="radio" name="tab" id="t-flow">'
        '<input class="tabnav" type="radio" name="tab" id="t-resolve">'
        '<input class="tabnav" type="radio" name="tab" id="t-audit">'
        '<input class="tabnav" type="radio" name="tab" id="t-packs">'
        '<nav class="tabs">'
        '<label for="t-readiness">Readiness</label>'
        '<label for="t-flow">Production Flow</label>'
        '<label for="t-resolve">Resolve</label>'
        '<label for="t-audit">Audit</label>'
        '<label for="t-packs">Capability Packs</label>'
        '</nav>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GGRL — Production-Aware PR Readiness</title>
<style>{_CSS}</style>
</head>
<body style="--accent: {accent};">
<div class="wrap">
  <header class="top">
    <h1>GGRL — Production-Aware PR Readiness</h1>
    <p class="sub">AI fixed the review loop. GGRL tells you if the PR is safe for production.</p>
  </header>
  {tabs_nav}
  <section class="tabpanel" id="tab-readiness">

  <section class="banner">
    <div class="status">{_e(readiness.get("status_label", status))}</div>
    <div class="reason">{_e(readiness.get("reason", ""))}</div>
    {f'<div class="strip-line">{_e(strip)}</div>' if strip else ''}
    {pr_link}
  </section>

  <div class="cards">{_evidence_cards(evidence)}</div>

  <h2 class="section">Production architecture context</h2>
  <div class="arch">
    {_arch_flow(arch)}
    {_arch_async(arch)}
    <div class="flow sub">Owner: {_e(", ".join(arch.get("owners") or []) or "unknown")} ·
      Runtime: {_e((arch.get("runtime") or "unknown").title())}</div>
    {_context_panels(readiness)}
  </div>

  {_safety_panel(readiness)}

  <h2 class="section">Production risks</h2>
  <table>
    <thead><tr><th>Severity</th><th>Surface</th><th>Evidence</th></tr></thead>
    <tbody>{_risk_rows(risks)}</tbody>
  </table>

  <h2 class="section">Human decision required</h2>
  <div class="decision">
    <p>{_e(readiness.get("reason", ""))}</p>
    {points_html}
    <ol>{options_html}</ol>
  </div>

  </section>
  {_flow_tab(readiness)}
  {_resolve_tab(readiness)}
  {_audit_tab(readiness)}
  {_packs_tab(readiness)}

  <footer>The AI runs the loop. The human owns production risk and merge judgment.</footer>
</div>
</body>
</html>
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Render the readiness JSON as a polished static HTML report."
    )
    parser.add_argument("--readiness", required=True, help="Path to readiness JSON.")
    parser.add_argument("--output", required=True, help="Output HTML file path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        readiness = json.loads(Path(args.readiness).read_text(encoding="utf-8", errors="replace"))
        if not isinstance(readiness, dict):
            raise ValueError("readiness JSON must be an object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    html_doc = render_html(readiness)
    output = Path(args.output)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(html_doc, encoding="utf-8")
    except OSError as exc:
        print(f"error: could not write output: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

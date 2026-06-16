#!/usr/bin/env python3
"""Run the payments-api Capability-Pack demo end-to-end (offline, deterministic).

Chains the whole MergeProof pipeline on the seeded payments-api app PR:

    detect obligations  ->  stage infra changes (dry-run)  ->  build readiness
    ->  render the 5-tab HTML report

Nothing here touches the network, GitHub, or any real repo: the infra repo
(``acme/platform-infra``) is fictional, so staging runs in dry-run — real
generated file contents and a constructed PR-create deep-link, no push.

Writes ``readiness.json`` and ``pr_readiness_report.html`` next to this file
(the committed demo artifacts). Run it, then open the HTML in any browser:

    python3 demo/production-readiness/payments-api/run_demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SCRIPTS = ROOT / "plugins" / "gh-gemini-review-loop" / "skills" / "gh-gemini-review-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pr_obligations import detect_obligations, load_capabilities_and_packs  # noqa: E402
from render_demo_ui import render_html  # noqa: E402
from render_pr_readiness import build_readiness  # noqa: E402
from stage_obligations import stage_obligations  # noqa: E402

FIXTURES = HERE / "fixtures"

# Demo production context for payments-api (a public, Kubernetes-hosted service
# with a Redis/arq async tier). In a real run this comes from the architecture
# scan + Production Context Pack; here it is a fixed demo constant.
ARCHITECTURE = {
    "service_name": "payments-api",
    "exposure": "public",
    "runtime": "kubernetes",
    "ingress": [],
    "datastores": [],
    "queues": ["redis:arq"],
    "owners": [],
}
NO_RISK = {
    "production_risks": [],
    "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0},
}


def main() -> int:
    capabilities, packs, service = load_capabilities_and_packs(FIXTURES / "mergeproof.yaml")
    changed = json.loads((FIXTURES / "changed_files.json").read_text(encoding="utf-8"))
    loop_summary = json.loads((FIXTURES / "loop_summary.json").read_text(encoding="utf-8"))

    obligations = detect_obligations(changed, capabilities, packs, service=service)
    obligations = stage_obligations(
        obligations,
        repo="acme/platform-infra",
        base="main",
        allow=["envs/prod/payments-api/**", "helm/payments-api/**", "terraform/payments-api/**"],
        templates_root=FIXTURES / "capabilities",
        source_pr=loop_summary.get("pr_url", ""),
        dry_run=True,
    )

    readiness = build_readiness(loop_summary, ARCHITECTURE, NO_RISK, obligations=obligations)

    (HERE / "readiness.json").write_text(
        json.dumps(readiness, indent=2, sort_keys=True), encoding="utf-8"
    )
    (HERE / "pr_readiness_report.html").write_text(render_html(readiness), encoding="utf-8")

    print(f"status: {readiness['status']}")
    for ob in readiness["obligations"]:
        print(f"  obligation: {ob['type']} -> {ob['outcome']}")
    print(f"wrote {HERE / 'readiness.json'}")
    print(f"wrote {HERE / 'pr_readiness_report.html'}  (open in a browser)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

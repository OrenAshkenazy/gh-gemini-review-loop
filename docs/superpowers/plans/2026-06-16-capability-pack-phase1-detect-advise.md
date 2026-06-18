# Capability-Pack Phase 1 — Detect + Advise — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the MergeProof readiness card name the *specific* infra obligation a PR implies — which capability, which repo, which approver, and what a human must still supply — without writing anything to any repo.

**Architecture:** A new deterministic detector (`pr_obligations.py`) maps a PR's changed files to obligations, guarded by the capability types declared in the trusted `mergeproof.yaml`. Each obligation resolves to one of three outcomes — `matched` / `human_gated` / `blocked` — by looking up the declared Capability Pack (reusing the already-built `capability_pack.py`). The obligations are threaded into the existing `build_readiness` data structure as a new `obligations` key, two new status rules force `HUMAN_DECISION_REQUIRED`, and `render_markdown` grows an action panel. No generation, no infra writes — that is Phase 2.

**Tech Stack:** Python 3.10+, stdlib only (`re`, `json`, `argparse`, `pathlib`), `from __future__ import annotations`. Tests via `/opt/homebrew/bin/pytest`. GitHub calls stay behind an injectable runner (existing pattern).

**Spec:** `docs/superpowers/specs/2026-06-16-capability-pack-infra-pr-automation-design.md`

**Scope note:** This is Phase 1 of the spec. Phase 2 (generate + one-click infra PR + tabbed HTML UI) is a separate plan written after this one lands. Phase 1 ships working, testable software on its own: the card names concrete obligations.

---

## File Structure

| File | Responsibility | Created/Modified |
|---|---|---|
| `plugins/.../scripts/capability_pack.py` | Pack model (`load_pack`, `capabilities_from_config`, `pack_approver`). Already written, **untracked** — committed in Task 1. | Commit existing |
| `tests/test_capability_pack.py` | Pack model tests (8, green). Already written, **untracked**. | Commit existing |
| `demo/production-readiness/payments-api/` | Demo fixtures (config + packs + changed files). Already written, **untracked**; `worker_deployment.yaml` edited so its required inputs are path-derivable. | Commit + edit |
| `.gitignore` | Ignore working-tree noise (`err.log`, `prof_out.json`, `wait_*.log/json`, `skills-lock.json`). | Modify |
| `plugins/.../scripts/pr_obligations.py` | **New.** Deterministic obligation detector + three-outcome resolver + CLI. | Create |
| `tests/test_pr_obligations.py` | **New.** Unit tests for detection, the three outcomes, input derivation, CLI. | Create |
| `plugins/.../scripts/render_pr_readiness.py` | Add optional `obligations` to `build_readiness`, two status rules, `obligations` output key, action panel in `render_markdown`, `--obligations` CLI flag. | Modify |
| `tests/test_render_pr_readiness.py` | Add tests for status rules + action panel rendering. | Modify |

Throughout, `SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts`.

---

## Task 1: Foundation — commit the seed, edit the demo pack, gitignore noise

**Files:**
- Commit: `plugins/.../scripts/capability_pack.py`, `tests/test_capability_pack.py`, `demo/production-readiness/payments-api/**`
- Modify: `demo/production-readiness/payments-api/fixtures/capabilities/worker_deployment.yaml`
- Modify: `.gitignore`

- [ ] **Step 1: Confirm the seed is green before committing**

Run: `/opt/homebrew/bin/pytest tests/test_capability_pack.py -q`
Expected: `8 passed`.

- [ ] **Step 2: Make the demo worker pack's required inputs path-derivable**

The detector derives `worker_name` and `service` from the changed file path; `topic`
is not path-derivable. So a `matched` worker demo requires the worker pack to not
require `topic`. Edit `demo/production-readiness/payments-api/fixtures/capabilities/worker_deployment.yaml` to:

```yaml
capability: worker_deployment
inputs:
  worker_name: required
  service: required
  topic: optional
generates:
  - worker_deployment
  - helm_worker_values
checks:
  - helm_template
  - policy
  - naming_convention
approval:
  required_from:
    - platform-runtime
```

(Only the `topic:` line changed: `required` → `optional`.)

- [ ] **Step 3: Add a secret-wiring trigger file to the demo changed-files fixture**

So the demo shows a `human_gated` outcome alongside the `matched` worker, edit
`demo/production-readiness/payments-api/fixtures/changed_files.json` to:

```json
[
  {"path": "app/providers/acme.py", "status": "modified"},
  {"path": "app/secrets/stripe_webhook.py", "status": "added"},
  {"path": "app/workers/refund_worker.py", "status": "added"},
  {"path": "tests/test_refund_worker.py", "status": "added"}
]
```

- [ ] **Step 4: Append working-tree noise to `.gitignore`**

Add these lines to `.gitignore` (create the block if absent):

```gitignore
# transient local run artifacts (never committed)
err.log
wait_err.log
wait_out.json
prof_out.json
skills-lock.json
```

- [ ] **Step 5: Verify the noise is now ignored and the seed is staged**

Run: `git status --short`
Expected: `err.log`, `wait_err.log`, `wait_out.json`, `prof_out.json`, `skills-lock.json` no longer listed; `capability_pack.py`, `test_capability_pack.py`, and `payments-api/` present as untracked/modified.

- [ ] **Step 6: Run the full suite (nothing should break)**

Run: `/opt/homebrew/bin/pytest -q`
Expected: all pass (711 + 8 capability_pack = 719).

- [ ] **Step 7: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/capability_pack.py \
        tests/test_capability_pack.py \
        demo/production-readiness/payments-api \
        .gitignore
git commit -m "chore: track capability-pack seed + payments-api fixtures, ignore run noise"
```

---

## Task 2: `pr_obligations.py` — detect a `matched` worker obligation

**Files:**
- Create: `plugins/.../scripts/pr_obligations.py`
- Test: `tests/test_pr_obligations.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pr_obligations.py
from pr_obligations import detect_obligations


def _worker_pack():
    return {
        "capability": "worker_deployment",
        "inputs": {"worker_name": "required", "service": "required", "topic": "optional"},
        "generates": ["worker_deployment", "helm_worker_values"],
        "checks": ["helm_template", "policy", "naming_convention"],
        "approval": {"required_from": ["platform-runtime"]},
        "human_gate": None,
    }


def test_added_worker_file_is_matched():
    changed = [{"path": "app/workers/refund_worker.py", "status": "added"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@platform-runtime"}}
    packs = {"worker_deployment": _worker_pack()}

    obligations = detect_obligations(changed, capabilities, packs, service="payments-api")

    assert len(obligations) == 1
    ob = obligations[0]
    assert ob["type"] == "worker_deployment"
    assert ob["outcome"] == "matched"
    assert ob["evidence_files"] == ["app/workers/refund_worker.py"]
    assert ob["inputs"] == {"worker_name": "refund_worker", "service": "payments-api"}
    assert ob["pack"]["approver"] == "@platform-runtime"
    assert ob["human_gate_pending"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pr_obligations'`.

- [ ] **Step 3: Write the minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/pr_obligations.py tests/test_pr_obligations.py
git commit -m "feat: detect matched worker_deployment obligation from PR diff"
```

---

## Task 3: `human_gated`, `blocked`, and no-obligation cases

**Files:**
- Test: `tests/test_pr_obligations.py`
- (No implementation change expected — Task 2's code already covers these. This task proves it.)

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_pr_obligations.py

def _secret_pack():
    return {
        "capability": "secret_wiring",
        "inputs": {"secret_name": "required", "env_var": "required", "service": "required"},
        "generates": ["external_secret", "helm_env_wiring"],
        "checks": ["helm_template", "policy", "naming_convention"],
        "approval": {"required_from": ["platform-secrets"]},
        "human_gate": "secret value provisioning",
    }


def test_secret_with_human_gate_and_missing_input_is_human_gated():
    changed = [{"path": "app/secrets/stripe_webhook.py", "status": "added"}]
    capabilities = {"secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"}}
    packs = {"secret_wiring": _secret_pack()}

    ob = detect_obligations(changed, capabilities, packs, service="payments-api")[0]

    assert ob["outcome"] == "human_gated"
    # declared gate + the one required input the path can't supply (env_var)
    assert "secret value provisioning" in ob["human_gate_pending"]
    assert "input: env_var" in ob["human_gate_pending"]


def test_obligation_without_declared_capability_is_blocked():
    changed = [{"path": "app/workers/refund_worker.py", "status": "added"}]
    ob = detect_obligations(changed, capabilities={}, packs={}, service="payments-api")[0]
    assert ob["outcome"] == "blocked"
    assert ob["pack"] is None


def test_infra_irrelevant_change_yields_no_obligation():
    changed = [{"path": "README.md", "status": "modified"}, {"path": "app/util/math.py", "status": "modified"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@x"}}
    packs = {"worker_deployment": _worker_pack()}
    assert detect_obligations(changed, capabilities, packs, service="payments-api") == []


def test_modified_worker_does_not_trigger_added_only_rule():
    changed = [{"path": "app/workers/refund_worker.py", "status": "modified"}]
    capabilities = {"worker_deployment": {"type": "worker_deployment", "template": "x", "approver": "@x"}}
    packs = {"worker_deployment": _worker_pack()}
    assert detect_obligations(changed, capabilities, packs, service="payments-api") == []
```

- [ ] **Step 2: Run tests to verify pass (behavior already implemented)**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py -v`
Expected: all PASS. If `test_secret_..._human_gated` fails on the `env_var` assertion, confirm `_derive_inputs` does not add `env_var` for `secret_wiring` (it must not — only `secret_name` + `service`).

- [ ] **Step 3: Commit**

```bash
git add tests/test_pr_obligations.py
git commit -m "test: cover human_gated, blocked, and no-obligation detection"
```

---

## Task 4: `pr_obligations.py` CLI — load packs from config + emit JSON

**Files:**
- Modify: `plugins/.../scripts/pr_obligations.py`
- Test: `tests/test_pr_obligations.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_pr_obligations.py
import json
from pathlib import Path

from pr_obligations import load_capabilities_and_packs


def test_load_capabilities_and_packs_reads_templates(tmp_path: Path):
    (tmp_path / "capabilities").mkdir()
    (tmp_path / "mergeproof.yaml").write_text(
        "version: 1\n"
        "service: payments-api\n"
        "capabilities:\n"
        "  - type: worker_deployment\n"
        "    template: capabilities/worker_deployment.yaml\n"
        "    approver: \"@platform-runtime\"\n",
        encoding="utf-8",
    )
    (tmp_path / "capabilities" / "worker_deployment.yaml").write_text(
        "capability: worker_deployment\n"
        "inputs:\n"
        "  worker_name: required\n"
        "  service: required\n"
        "generates:\n"
        "  - worker_deployment\n"
        "checks:\n"
        "  - helm_template\n"
        "approval:\n"
        "  required_from:\n"
        "    - platform-runtime\n",
        encoding="utf-8",
    )

    capabilities, packs, service = load_capabilities_and_packs(tmp_path / "mergeproof.yaml")

    assert service == "payments-api"
    assert "worker_deployment" in capabilities
    assert packs["worker_deployment"]["generates"] == ["worker_deployment"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py::test_load_capabilities_and_packs_reads_templates -v`
Expected: FAIL — `ImportError: cannot import name 'load_capabilities_and_packs'`.

- [ ] **Step 3: Implement the loader + CLI**

Add to `pr_obligations.py` (imports `capability_pack` and `mergeproof_config`, both on `sys.path` inside the plugin and via `conftest`):

```python
from capability_pack import capabilities_from_config, load_pack
from mergeproof_config import parse_yaml_subset


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
```

- [ ] **Step 4: Run the test + the file as a script against the demo fixtures**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py -q`
Expected: all PASS.

Run:
```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
F=demo/production-readiness/payments-api/fixtures
python3 $SCRIPTS/pr_obligations.py --mergeproof $F/mergeproof.yaml --changed-files $F/changed_files.json --json
```
Expected: JSON with a `matched` `worker_deployment` and a `human_gated` `secret_wiring`.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/pr_obligations.py tests/test_pr_obligations.py
git commit -m "feat: pr_obligations CLI loads packs from mergeproof.yaml and emits JSON"
```

---

## Task 5: Thread obligations into `build_readiness` (data + status rules)

**Files:**
- Modify: `plugins/.../scripts/render_pr_readiness.py`
- Test: `tests/test_render_pr_readiness.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/test_render_pr_readiness.py
from render_pr_readiness import build_readiness

_PASS_LOOP = {"pr_url": "https://github.com/o/r/pull/1", "verification": "passed", "fixed_count": 3}
_ARCH = {"service_name": "payments-api", "exposure": "public", "queues": ["redis:arq"]}
_NO_RISK = {"production_risks": [], "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0}}


def test_blocked_obligation_forces_human_decision():
    obligations = [{"type": "worker_deployment", "outcome": "blocked", "evidence_files": ["a"], "inputs": {}, "pack": None, "human_gate_pending": []}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "HUMAN_DECISION_REQUIRED"
    assert r["obligations"] == obligations


def test_human_gated_obligation_forces_human_decision():
    obligations = [{"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["a"], "inputs": {}, "pack": {"approver": "@x"}, "human_gate_pending": ["secret value provisioning"]}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "HUMAN_DECISION_REQUIRED"


def test_all_matched_obligations_do_not_block_ready():
    obligations = [{"type": "worker_deployment", "outcome": "matched", "evidence_files": ["a"], "inputs": {}, "pack": {"approver": "@x"}, "human_gate_pending": []}]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    assert r["status"] == "READY"
    assert r["obligations"] == obligations


def test_obligations_default_empty_when_omitted():
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK)
    assert r["obligations"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -k obligation -v`
Expected: FAIL — `build_readiness() got an unexpected keyword argument 'obligations'`.

- [ ] **Step 3: Modify `build_readiness`**

Change the signature (around `render_pr_readiness.py:143`):

```python
def build_readiness(
    loop_summary: dict[str, Any],
    architecture: dict[str, Any],
    production_risks: dict[str, Any],
    obligations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
```

Just after `obligations = obligations or []` add normalization near the top of the body
(after `production_risks = production_risks or {}`):

```python
    obligations = obligations if isinstance(obligations, list) else []
    blocked = any(o.get("outcome") == "blocked" for o in obligations)
    gated = any(o.get("human_gate_pending") for o in obligations)
```

Add a `_REASONS` entry near the existing ones:

```python
    "HUMAN_DECISION_REQUIRED_OBLIGATION": (
        "AI review loop completed and tests passed, but this PR implies a "
        "production infra change that needs a human (no approved path, or a "
        "value only a human can provide)."
    ),
```

Insert two rules into the status chain, **after** the `safety.get("config_changed")`
branch and **before** the `elif human_required:` branch:

```python
    elif blocked or gated:
        status = "HUMAN_DECISION_REQUIRED"
        reason = _REASONS["HUMAN_DECISION_REQUIRED_OBLIGATION"]
```

Add `"obligations": obligations,` to the returned dict (next to `"production_risks": risks,`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -q`
Expected: all PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_pr_readiness.py tests/test_render_pr_readiness.py
git commit -m "feat: thread obligations into readiness data and status logic"
```

---

## Task 6: Action panel in `render_markdown`

**Files:**
- Modify: `plugins/.../scripts/render_pr_readiness.py`
- Test: `tests/test_render_pr_readiness.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_render_pr_readiness.py
from render_pr_readiness import render_markdown


def test_markdown_renders_obligation_action_panel():
    obligations = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
         "pack": {"generates": ["worker_deployment", "helm_worker_values"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None},
         "human_gate_pending": []},
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["app/secrets/stripe_webhook.py"],
         "inputs": {"secret_name": "stripe_webhook", "service": "payments-api"},
         "pack": {"generates": ["external_secret"], "checks": ["policy"], "approver": "@platform-secrets", "human_gate": "secret value provisioning"},
         "human_gate_pending": ["secret value provisioning", "input: env_var"]},
    ]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    md = render_markdown(r)

    assert "### Production obligations" in md
    assert "worker_deployment" in md
    assert "@platform-runtime" in md
    assert "Needs a human" in md            # the human_gated row label
    assert "secret value provisioning" in md


def test_markdown_omits_obligation_panel_when_none():
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK)
    assert "### Production obligations" not in render_markdown(r)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -k obligation_action -v`
Expected: FAIL — `### Production obligations` not found.

- [ ] **Step 3: Implement the panel**

Add a helper above `render_markdown`:

```python
_OUTCOME_LABEL = {
    "matched": "Ready to stage",
    "human_gated": "Needs a human",
    "blocked": "Blocked — no approved capability",
}


def _render_obligations(obligations: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = ["### Production obligations", ""]
    lines.append("| Capability | Status | Approver | Action |")
    lines.append("|---|---|---|---|")
    for ob in obligations:
        pack = ob.get("pack") or {}
        label = _OUTCOME_LABEL.get(ob.get("outcome", ""), ob.get("outcome", ""))
        approver = pack.get("approver") or "—"
        files = ", ".join(f"`{f}`" for f in ob.get("evidence_files", []))
        if ob.get("outcome") == "blocked":
            action = f"{files} implies infra change but no approved capability — escalate to platform."
        elif ob.get("human_gate_pending"):
            action = f"{files} — provide before merge: {', '.join(ob['human_gate_pending'])}."
        else:
            generates = ", ".join(pack.get("generates") or [])
            action = f"{files} — generates {generates}. (Phase 2 stages the infra PR.)"
        lines.append(f"| `{ob.get('type', '')}` | {label} | {approver} | {action} |")
    lines.append("")
    return lines
```

In `render_markdown`, after the `### Production risks` block (right before
`lines.append("### Human decision required")`), add:

```python
    if readiness.get("obligations"):
        lines.extend(_render_obligations(readiness["obligations"]))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_pr_readiness.py tests/test_render_pr_readiness.py
git commit -m "feat: render production-obligation action panel in readiness card"
```

---

## Task 7: `--obligations` CLI flag on `render_pr_readiness.py`

**Files:**
- Modify: `plugins/.../scripts/render_pr_readiness.py`
- Test: `tests/test_render_pr_readiness.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_render_pr_readiness.py
import json
from pathlib import Path
from render_pr_readiness import main as render_main


def _write(p: Path, obj) -> str:
    p.write_text(json.dumps(obj), encoding="utf-8")
    return str(p)


def test_cli_accepts_obligations_file(tmp_path, capsys):
    loop = _write(tmp_path / "loop.json", _PASS_LOOP)
    arch = _write(tmp_path / "arch.json", _ARCH)
    risks = _write(tmp_path / "risks.json", _NO_RISK)
    obs = _write(tmp_path / "obs.json", {"obligations": [
        {"type": "worker_deployment", "outcome": "blocked", "evidence_files": ["a"], "inputs": {}, "pack": None, "human_gate_pending": []}
    ]})

    rc = render_main(["--loop-summary", loop, "--architecture-context", arch,
                      "--production-risks", risks, "--obligations", obs, "--json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["status"] == "HUMAN_DECISION_REQUIRED"
    assert out["obligations"][0]["outcome"] == "blocked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py::test_cli_accepts_obligations_file -v`
Expected: FAIL — `unrecognized arguments: --obligations`.

- [ ] **Step 3: Implement the flag**

In `build_parser` add (after `--production-risks`):

```python
    parser.add_argument(
        "--obligations",
        help="Optional path to pr_obligations.py --json output.",
    )
```

In `main`, after the three `_load_json` calls, load obligations when provided and
pass them through:

```python
    obligations: list[dict[str, Any]] = []
    if args.obligations:
        try:
            ob_doc = _load_json(args.obligations, "obligations")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raw = ob_doc.get("obligations")
        obligations = raw if isinstance(raw, list) else []

    readiness = build_readiness(loop_summary, architecture, risks, obligations=obligations)
```

(Replace the existing `readiness = build_readiness(loop_summary, architecture, risks)` line.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_pr_readiness.py tests/test_render_pr_readiness.py
git commit -m "feat: render_pr_readiness accepts an --obligations file"
```

---

## Task 8: End-to-end demo wiring + full-suite gate

**Files:**
- Test: `tests/test_pr_obligations.py` (one integration test)
- Verify: full suite + a manual demo render

- [ ] **Step 1: Write the failing integration test (real fixtures)**

```python
# append to tests/test_pr_obligations.py
def test_payments_api_fixtures_produce_matched_and_gated():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    fixtures = root / "demo" / "production-readiness" / "payments-api" / "fixtures"
    capabilities, packs, service = load_capabilities_and_packs(fixtures / "mergeproof.yaml")
    changed = _read_changed(str(fixtures / "changed_files.json"))
    obligations = detect_obligations(changed, capabilities, packs, service=service)

    by_type = {o["type"]: o for o in obligations}
    assert by_type["worker_deployment"]["outcome"] == "matched"
    assert by_type["secret_wiring"]["outcome"] == "human_gated"
```

(Imports `_read_changed`, `load_capabilities_and_packs`, `detect_obligations` already
present at the top of the test module from earlier tasks.)

- [ ] **Step 2: Run the integration test**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py::test_payments_api_fixtures_produce_matched_and_gated -v`
Expected: PASS. (If `worker_deployment` is `human_gated`, re-check Task 1 Step 2 made `topic` optional.)

- [ ] **Step 3: Manual end-to-end render against the demo fixtures**

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
F=demo/production-readiness/payments-api/fixtures
# obligations from the detector
python3 $SCRIPTS/pr_obligations.py --mergeproof $F/mergeproof.yaml \
  --changed-files $F/changed_files.json --json > /tmp/obs.json
# minimal arch + empty-risks inputs (the obligation panel is what we're checking)
printf '%s' '{"service_name":"payments-api","exposure":"public","queues":["redis:arq"]}' > /tmp/arch.json
printf '%s' '{"production_risks":[],"summary":{"highest_severity":"none","human_decision_required":false,"risk_count":0}}' > /tmp/risks.json
python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary $F/loop_summary.json \
  --architecture-context /tmp/arch.json \
  --production-risks /tmp/risks.json \
  --obligations /tmp/obs.json --markdown
```
Expected: the card prints a **### Production obligations** panel with a `matched`
`worker_deployment` (approver `@platform-runtime`) and a `human_gated`
`secret_wiring` (approver `@platform-secrets`, gate `secret value provisioning`).

- [ ] **Step 4: Run the full suite (Phase 1 done bar)**

Run: `/opt/homebrew/bin/pytest -q`
Expected: all pass (719 + the new `pr_obligations`/readiness tests).

- [ ] **Step 5: Commit**

```bash
git add tests/test_pr_obligations.py
git commit -m "test: payments-api fixtures yield matched + human_gated obligations"
```

---

## Phase 1 Definition of Done

- [ ] Seed (`capability_pack.py`, its tests, `payments-api/` fixtures) is committed; run noise is gitignored.
- [ ] `pr_obligations.py` detects `matched` / `human_gated` / `blocked` deterministically and has a CLI.
- [ ] `build_readiness` carries an `obligations` key and forces `HUMAN_DECISION_REQUIRED` on `blocked`/`human_gate_pending`.
- [ ] `render_markdown` shows the obligation action panel; omits it when there are no obligations.
- [ ] `render_pr_readiness.py` accepts `--obligations`.
- [ ] Full suite green.
- [ ] Demo fixtures yield a `matched` worker + a `human_gated` secret obligation.

Phase 2 (generate the infra files, push a branch, the one-click deep-link button,
and the tabbed HTML UI per the spec's UI priority order) is the next plan.

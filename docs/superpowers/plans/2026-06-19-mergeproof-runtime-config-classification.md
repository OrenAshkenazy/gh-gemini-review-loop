# MergeProof Runtime-Config Classification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect env-var reads in a PR's changed Python files and classify each as a `runtime_config` (ConfigMap/Helm) or `secret_wiring` (ExternalSecret) obligation by matching infra-repo precedent, never a name guess.

**Architecture:** Three small deterministic units — a content-aware env-read detector, an infra-precedent classifier, and an obligation composer — plus a shared `assemble_obligation` helper refactored out of `pr_obligations.py`. The classifier is the router: it reads how this service already wires peer env vars (workload-scoped first, repo-wide fallback, else `unknown` → human gate) and emits a cited classification. A new `runtime_config` capability pack is the generation boundary for config wiring.

**Tech Stack:** Python 3.9+ (`from __future__ import annotations`), zero third-party deps, stdlib `re`/`json`/`pathlib`, pytest (run via `/opt/homebrew/bin/pytest`). Capability packs are block-style YAML parsed by the existing `parse_yaml_subset`.

**Scope:** This plan covers the env config-vs-secret classification core (spec Decision 1, half a). The queue-resource second-signal gating (`queue_topic` only when a producer/consumer signal *and* an infra queue pattern exist) is a deferred follow-on plan. Orchestrator wiring that fetches PR-head changed-file *content* is also a follow-on; this plan delivers a fully unit- and integration-tested detector that operates on in-memory content, plus a CLI entry.

**Spec:** `docs/superpowers/specs/2026-06-19-mergeproof-phase2-open-design-questions-resolution.md` (Decision 1).

**Conventions to follow (existing codebase):**
- Every script starts with `#!/usr/bin/env python3`, a module docstring, then `from __future__ import annotations`.
- Obligation dicts use the shape produced by `pr_obligations.detect_obligations` (keys: `type`, `outcome`, `evidence_files`, `inputs`, `pack`, `human_gate_pending`).
- Tests import scripts directly (e.g. `from env_reads import detect_env_reads`); `tests/conftest.py` already puts the scripts dir on `sys.path`.
- Scripts live in `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/`. Tests live in `tests/`.

---

### Task 1: Extract `assemble_obligation` helper in `pr_obligations.py`

Refactor the obligation-building logic out of the detection loop so the new env composer (Task 5) reuses one source of truth for `outcome` computation. Existing behavior and tests must stay green.

**Files:**
- Modify: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/pr_obligations.py:73-125`
- Test: `tests/test_pr_obligations.py` (existing — must stay green)

- [ ] **Step 1: Add the helper, keeping existing output identical**

Insert this function above `detect_obligations` (after `_derive_inputs`):

```python
def assemble_obligation(
    obligation_type: str,
    evidence_files: list[str],
    inputs: dict[str, str],
    capability: dict[str, Any] | None,
    pack: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one obligation dict with its outcome computed.

    Single source of truth for outcome: ``blocked`` when no capability is
    declared, ``human_gated`` when a human gate or a required input is missing,
    else ``matched``. *extra* merges additional keys (e.g. classification fields).
    """
    if capability is None:
        obligation = {
            "type": obligation_type,
            "outcome": "blocked",
            "evidence_files": evidence_files,
            "inputs": {},
            "pack": None,
            "human_gate_pending": [],
        }
        if extra:
            obligation.update(extra)
        return obligation

    pack = pack or {}
    required = [k for k, v in (pack.get("inputs") or {}).items() if v == "required"]
    missing = [k for k in required if k not in inputs]
    gate = pack.get("human_gate")
    human_gate_pending = ([gate] if gate else []) + [f"input: {k}" for k in missing]
    approval = pack.get("approval") or {}
    required_from = approval.get("required_from") or []
    approver = capability.get("approver") or (
        f"@{required_from[0].lstrip('@')}" if required_from else None
    )
    obligation = {
        "type": obligation_type,
        "outcome": "human_gated" if human_gate_pending else "matched",
        "evidence_files": evidence_files,
        "inputs": inputs,
        "pack": {
            "generates": list(pack.get("generates") or []),
            "checks": list(pack.get("checks") or []),
            "approver": approver,
            "human_gate": gate,
            "template_map": pack.get("template_map") or {},
        },
        "human_gate_pending": human_gate_pending,
    }
    if extra:
        obligation.update(extra)
    return obligation
```

- [ ] **Step 2: Rewrite the `detect_obligations` loop body to call the helper**

Replace the body of the `for obligation_type, pattern, statuses in _RULES:` loop (the block that builds and appends each obligation, currently lines ~73-123) with:

```python
    for obligation_type, pattern, statuses in _RULES:
        evidence = [
            entry["path"]
            for entry in norm
            if pattern.search(entry["path"]) and entry["status"] in statuses
        ]
        if not evidence:
            continue

        capability = capabilities.get(obligation_type)
        pack = packs.get(obligation_type) if capability is not None else None
        inputs = _derive_inputs(obligation_type, evidence, service) if capability is not None else {}
        obligations.append(
            assemble_obligation(obligation_type, evidence, inputs, capability, pack)
        )
```

- [ ] **Step 3: Run the existing obligation tests to verify no behavior change**

Run: `/opt/homebrew/bin/pytest tests/test_pr_obligations.py -q`
Expected: PASS (all existing tests green — the refactor is behavior-preserving).

- [ ] **Step 4: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/pr_obligations.py
git commit -m "refactor: extract assemble_obligation helper in pr_obligations"
```

---

### Task 2: Content-aware env-read detector (`env_reads.py`)

Parse changed Python files for environment reads. Pure function over an in-memory `{path: content}` mapping — no I/O, fully testable.

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/env_reads.py`
- Test: `tests/test_env_reads.py`

- [ ] **Step 1: Write the failing test**

```python
from env_reads import detect_env_reads


def test_detects_environ_subscript_and_getenv_with_scope_and_line():
    changed = {
        "app/api/chargebacks.py": "import os\nX = os.environ['CHARGEBACK_PROVIDER_URL']\n",
        "app/workers/chargeback_worker.py": "import os\nQ = os.getenv('CHARGEBACK_QUEUE_NAME')\n",
    }
    reads = detect_env_reads(changed)
    by_name = {r["name"]: r for r in reads}

    assert set(by_name) == {"CHARGEBACK_PROVIDER_URL", "CHARGEBACK_QUEUE_NAME"}
    assert by_name["CHARGEBACK_PROVIDER_URL"]["scope"] == "api"
    assert by_name["CHARGEBACK_PROVIDER_URL"]["source_file"] == "app/api/chargebacks.py"
    assert by_name["CHARGEBACK_PROVIDER_URL"]["source_line"] == 2
    assert by_name["CHARGEBACK_QUEUE_NAME"]["scope"] == "worker"


def test_same_var_in_api_and_worker_is_scope_both():
    changed = {
        "app/api/x.py": "import os\nos.getenv('SHARED_FLAG')\n",
        "app/workers/y.py": "import os\nos.getenv('SHARED_FLAG')\n",
    }
    reads = detect_env_reads(changed)
    assert len(reads) == 1
    assert reads[0]["name"] == "SHARED_FLAG"
    assert reads[0]["scope"] == "both"


def test_ignores_non_python_and_lowercase_names():
    changed = {
        "README.md": "os.environ['NOT_CODE']",
        "app/api/z.py": "import os\nos.getenv('lower_case')\n",
    }
    assert detect_env_reads(changed) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_env_reads.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'env_reads'`.

- [ ] **Step 3: Write the implementation**

```python
#!/usr/bin/env python3
"""Detect environment-variable reads in a PR's changed Python files.

Deterministic and zero-dependency. Operates on an in-memory ``{path: content}``
mapping so it is fully unit-testable offline. An env read is one obligation
*signal*; classification into config vs secret happens in ``env_precedent``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# os.environ["NAME"] | os.environ.get("NAME") | os.getenv("NAME"); single or double quotes.
_ENV_ACCESS = re.compile(
    r"os\.(?:environ\s*\[\s*|environ\.get\s*\(\s*|getenv\s*\(\s*)"
    r"""['"]([A-Z][A-Z0-9_]*)['"]"""
)

_WORKER_SEGMENTS = ("/workers/", "/jobs/", "/consumers/")


def _scope_for(path: str) -> str:
    lowered = path.lower()
    return "worker" if any(seg in lowered for seg in _WORKER_SEGMENTS) else "api"


def detect_env_reads(changed_content: dict[str, str]) -> list[dict[str, Any]]:
    """Return one record per distinct env name: ``{name, scope, source_file, source_line}``.

    Scope is ``worker`` or ``api`` per source path; a name seen in both becomes
    ``both``. The first occurrence supplies ``source_file``/``source_line``.
    """
    reads: dict[str, dict[str, Any]] = {}
    for path in sorted(changed_content):
        if not path.endswith(".py"):
            continue
        text = changed_content[path]
        scope = _scope_for(path)
        for match in _ENV_ACCESS.finditer(text):
            name = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            existing = reads.get(name)
            if existing is None:
                reads[name] = {
                    "name": name,
                    "scope": scope,
                    "source_file": path,
                    "source_line": line,
                }
            elif existing["scope"] != scope:
                existing["scope"] = "both"
    return [reads[name] for name in sorted(reads)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect env-var reads in changed Python files.")
    parser.add_argument(
        "--changed-content",
        required=True,
        help="Path to a JSON object mapping changed file path -> file content.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        data = json.loads(Path(args.changed_content).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    content = data if isinstance(data, dict) else {}
    reads = detect_env_reads(content)
    if args.json_output:
        print(json.dumps(reads, indent=2, sort_keys=True))
    else:
        for read in reads:
            print(f"  {read['name']} (scope {read['scope']}) {read['source_file']}:{read['source_line']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_env_reads.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/env_reads.py tests/test_env_reads.py
git commit -m "feat: content-aware env-read detector"
```

---

### Task 3: Infra-precedent classifier (`env_precedent.py`)

Classify an env name as `secret` / `config` / `unknown` by matching how peer env vars (same trailing suffix token) are wired in the infra files — workload-scoped first, repo-wide fallback, else `unknown`. Every decision cites its evidence files.

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/env_precedent.py`
- Test: `tests/test_env_precedent.py`

- [ ] **Step 1: Write the failing test**

```python
from env_precedent import classify_env


def _infra():
    return {
        # workload (payments-api) config source: a *_URL peer in Helm values
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        # workload secret source: a *_KEY peer under an ExternalSecret
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_API_KEY\n"
        ),
        # a different service, used only for the repo-wide fallback case
        "helm/orders-api/values.yaml": "env:\n  SHIPPING_PROVIDER_URL: https://ship.internal\n",
    }


def test_url_classifies_as_config_from_workload_precedent():
    result = classify_env("CHARGEBACK_PROVIDER_URL", _infra(), service="payments-api")
    assert result["classification"] == "config"
    assert result["capability"] == "runtime_config"
    assert result["precedent_scope"] == "workload"
    assert result["evidence_files"] == ["helm/payments-api/values.yaml"]


def test_key_classifies_as_secret_from_workload_precedent():
    result = classify_env("CHARGEBACK_API_KEY", _infra(), service="payments-api")
    assert result["classification"] == "secret"
    assert result["capability"] == "secret_wiring"
    assert result["precedent_scope"] == "workload"
    assert result["evidence_files"] == ["envs/prod/payments-api/secrets/external-secret.yaml"]


def test_repo_wide_fallback_when_service_has_no_local_peer():
    infra = {"helm/orders-api/values.yaml": "env:\n  SHIPPING_PROVIDER_URL: https://ship.internal\n"}
    result = classify_env("CHARGEBACK_PROVIDER_URL", infra, service="payments-api")
    assert result["classification"] == "config"
    assert result["precedent_scope"] == "repo_wide"
    assert result["evidence_files"] == ["helm/orders-api/values.yaml"]


def test_ambiguous_workload_precedent_is_unknown():
    infra = {
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: LEGACY_PROVIDER_URL\n"
        ),
    }
    result = classify_env("CHARGEBACK_PROVIDER_URL", infra, service="payments-api")
    assert result["classification"] == "unknown"
    assert result["capability"] is None
    assert result["precedent_scope"] == "workload"


def test_no_precedent_anywhere_is_unknown():
    result = classify_env("BRAND_NEW_THING", {}, service="payments-api")
    assert result["classification"] == "unknown"
    assert result["precedent_scope"] == "none"
    assert result["evidence_files"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_env_precedent.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'env_precedent'`.

- [ ] **Step 3: Write the implementation**

```python
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
    lowered = path.lower()
    return "/secrets/" in lowered or "externalsecret" in lowered or bool(_SECRET_KIND.search(text))


def _is_config_source(path: str, text: str) -> bool:
    lowered = path.lower()
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
        peers = {n for n in _ENV_TOKEN.findall(text) if n != name and _suffix(n) == target}
        if not peers:
            continue
        if _is_secret_source(path, text):
            secret_ev.append(path)
        elif _is_config_source(path, text):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_env_precedent.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/env_precedent.py tests/test_env_precedent.py
git commit -m "feat: infra-precedent env classifier"
```

---

### Task 4: `runtime_config` capability pack fixture

Add the config-wiring pack so the composer (Task 5) can route `config` classifications to a real, approved generation boundary. No `human_gate` — config has no gated value.

**Files:**
- Create: `demo/production-readiness/payments-api/fixtures/capabilities/runtime_config.yaml`
- Test: `tests/test_capability_pack.py` (add one case)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_capability_pack.py`:

```python
def test_runtime_config_fixture_pack_loads():
    from pathlib import Path
    from capability_pack import load_pack
    root = Path(__file__).resolve().parent.parent
    pack_path = (
        root / "demo" / "production-readiness" / "payments-api"
        / "fixtures" / "capabilities" / "runtime_config.yaml"
    )
    pack = load_pack(pack_path.read_text(encoding="utf-8"))
    assert pack["capability"] == "runtime_config"
    assert "helm_env_wiring" in pack["generates"]
    assert pack["human_gate"] is None
    assert pack["approval"]["required_from"] == ["platform-config"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_capability_pack.py::test_runtime_config_fixture_pack_loads -q`
Expected: FAIL with `FileNotFoundError`.

- [ ] **Step 3: Create the pack fixture**

`demo/production-readiness/payments-api/fixtures/capabilities/runtime_config.yaml`:

```yaml
capability: runtime_config
inputs:
  env_name: required
  service: required
  scope: required
generates:
  - configmap_entry
  - helm_env_wiring
checks:
  - helm_template
  - policy
  - naming_convention
approval:
  required_from:
    - platform-config
template_map:
  configmap_entry:
    template: templates/configmap_entry.tmpl
    output: helm/payments-api/env/${env_name}.yaml
  helm_env_wiring:
    template: templates/helm_env_wiring.tmpl
    output: helm/payments-api/values.yaml
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_capability_pack.py::test_runtime_config_fixture_pack_loads -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add demo/production-readiness/payments-api/fixtures/capabilities/runtime_config.yaml tests/test_capability_pack.py
git commit -m "feat: runtime_config capability pack fixture"
```

---

### Task 5: Env-obligation composer (`detect_env_obligations.py`)

Compose detector + classifier + `assemble_obligation` into classified obligations. Routes `config` → `runtime_config`, `secret` → `secret_wiring`, and `unknown` → a human-gated obligation that carries the classification reason and an advisory name-pattern suggestion.

**Files:**
- Create: `plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_env_obligations.py`
- Test: `tests/test_detect_env_obligations.py`

- [ ] **Step 1: Write the failing test**

```python
from detect_env_obligations import detect_env_obligations


def _caps_and_packs():
    capabilities = {
        "runtime_config": {"type": "runtime_config", "template": "x", "approver": "@platform-config"},
        "secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"},
    }
    packs = {
        "runtime_config": {
            "capability": "runtime_config",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["helm_env_wiring"],
            "checks": ["helm_template"],
            "approval": {"required_from": ["platform-config"]},
            "human_gate": None,
        },
        "secret_wiring": {
            "capability": "secret_wiring",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["external_secret"],
            "checks": ["helm_template"],
            "approval": {"required_from": ["platform-secrets"]},
            "human_gate": "secret value provisioning",
        },
    }
    return capabilities, packs


def _infra():
    return {
        "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n",
        "envs/prod/payments-api/secrets/external-secret.yaml": (
            "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_API_KEY\n"
        ),
    }


def test_url_read_becomes_matched_runtime_config():
    changed = {"app/api/chargebacks.py": "import os\nos.environ['CHARGEBACK_PROVIDER_URL']\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, _infra(), caps, packs, service="payments-api")

    ob = next(o for o in obligations if o["inputs"].get("env_name") == "CHARGEBACK_PROVIDER_URL")
    assert ob["type"] == "runtime_config"
    assert ob["outcome"] == "matched"
    assert ob["classification"]["classification"] == "config"
    assert ob["classification"]["evidence_files"] == ["helm/payments-api/values.yaml"]
    assert ob["inputs"] == {"env_name": "CHARGEBACK_PROVIDER_URL", "service": "payments-api", "scope": "api"}


def test_key_read_becomes_human_gated_secret_wiring():
    changed = {"app/workers/chargeback_worker.py": "import os\nos.getenv('CHARGEBACK_API_KEY')\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, _infra(), caps, packs, service="payments-api")

    ob = next(o for o in obligations if o["inputs"].get("env_name") == "CHARGEBACK_API_KEY")
    assert ob["type"] == "secret_wiring"
    assert ob["outcome"] == "human_gated"
    assert "secret value provisioning" in ob["human_gate_pending"]
    assert ob["inputs"]["scope"] == "worker"


def test_unknown_classification_is_human_gated_with_suggestion():
    changed = {"app/api/x.py": "import os\nos.getenv('BRAND_NEW_THING')\n"}
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(changed, {}, caps, packs, service="payments-api")

    ob = obligations[0]
    assert ob["type"] == "env_classification"
    assert ob["outcome"] == "human_gated"
    assert ob["classification"]["classification"] == "unknown"
    assert ob["advisory_suggestion"] in {"secret", "config", "unknown"}
    assert any("classify" in g for g in ob["human_gate_pending"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_detect_env_obligations.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'detect_env_obligations'`.

- [ ] **Step 3: Write the implementation**

```python
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
import sys
from pathlib import Path
from typing import Any

from env_precedent import classify_env
from env_reads import detect_env_reads
from pr_obligations import assemble_obligation

# Advisory-only suggestion shown inside the human gate when precedent is absent.
_SECRET_SUFFIXES = ("_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIALS", "_DSN")
_CONFIG_SUFFIXES = ("_URL", "_HOST", "_NAME", "_PORT", "_ENDPOINT")


def _advisory_suggestion(name: str) -> str:
    if name.endswith(_SECRET_SUFFIXES):
        return "secret"
    if name.endswith(_CONFIG_SUFFIXES):
        return "config"
    return "unknown"


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
        name = read["name"]
        classification = classify_env(name, infra_files, service)
        cap_type = classification["capability"]
        inputs = {"env_name": name, "service": service, "scope": read["scope"]}

        if cap_type is None:
            # unknown precedent -> human gate to classify, with an advisory suggestion.
            suggestion = _advisory_suggestion(name)
            obligation = assemble_obligation(
                "env_classification",
                [read["source_file"]],
                inputs,
                capability={"approver": None},
                pack={
                    "inputs": {},
                    "human_gate": f"classify {name} as secret or config",
                    "generates": [],
                    "checks": [],
                    "approval": {},
                },
                extra={"classification": classification, "advisory_suggestion": suggestion},
            )
            obligations.append(obligation)
            continue

        capability = capabilities.get(cap_type)
        pack = packs.get(cap_type) if capability is not None else None
        obligations.append(
            assemble_obligation(
                cap_type,
                [read["source_file"]],
                inputs,
                capability,
                pack,
                extra={"classification": classification},
            )
        )
    return obligations


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect classified env obligations.")
    parser.add_argument("--changed-content", required=True, help="JSON {path: content} for changed files.")
    parser.add_argument("--infra-files", required=True, help="JSON {path: content} for infra files.")
    parser.add_argument("--mergeproof", required=True, help="Path to trusted mergeproof.yaml.")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    from pr_obligations import load_capabilities_and_packs

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/opt/homebrew/bin/pytest tests/test_detect_env_obligations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/detect_env_obligations.py tests/test_detect_env_obligations.py
git commit -m "feat: compose classified env obligations"
```

---

### Task 6: End-to-end fixture integration test

Prove the full chain over realistic fixtures: changed app content + infra content → a matched `runtime_config`, a human-gated `secret_wiring`, and a cited classification — matching the spec's Decision 1 acceptance.

**Files:**
- Create: `tests/fixtures/runtime_config/changed_content.json`
- Create: `tests/fixtures/runtime_config/infra_files.json`
- Test: `tests/test_runtime_config_integration.py`

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/runtime_config/changed_content.json`:

```json
{
  "app/api/chargebacks.py": "import os\nPROVIDER = os.environ['CHARGEBACK_PROVIDER_URL']\n",
  "app/workers/chargeback_worker.py": "import os\nKEY = os.getenv('CHARGEBACK_SIGNING_KEY')\n"
}
```

`tests/fixtures/runtime_config/infra_files.json`:

```json
{
  "helm/payments-api/values.yaml": "env:\n  REFUND_PROVIDER_URL: https://refunds.internal\n  STRIPE_API_BASE: https://api.stripe.com\n",
  "envs/prod/payments-api/secrets/external-secret.yaml": "kind: ExternalSecret\ndata:\n  - secretKey: STRIPE_SIGNING_KEY\n"
}
```

- [ ] **Step 2: Write the failing test**

```python
import json
from pathlib import Path

from detect_env_obligations import detect_env_obligations


def _load(name: str):
    root = Path(__file__).resolve().parent / "fixtures" / "runtime_config"
    return json.loads((root / name).read_text(encoding="utf-8"))


def _caps_and_packs():
    capabilities = {
        "runtime_config": {"type": "runtime_config", "template": "x", "approver": "@platform-config"},
        "secret_wiring": {"type": "secret_wiring", "template": "x", "approver": "@platform-secrets"},
    }
    packs = {
        "runtime_config": {
            "capability": "runtime_config",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["helm_env_wiring"], "checks": ["helm_template"],
            "approval": {"required_from": ["platform-config"]}, "human_gate": None,
        },
        "secret_wiring": {
            "capability": "secret_wiring",
            "inputs": {"env_name": "required", "service": "required", "scope": "required"},
            "generates": ["external_secret"], "checks": ["helm_template"],
            "approval": {"required_from": ["platform-secrets"]}, "human_gate": "secret value provisioning",
        },
    }
    return capabilities, packs


def test_full_chain_classifies_url_config_and_key_secret():
    caps, packs = _caps_and_packs()
    obligations = detect_env_obligations(
        _load("changed_content.json"), _load("infra_files.json"), caps, packs, service="payments-api"
    )
    by_env = {o["inputs"]["env_name"]: o for o in obligations}

    url = by_env["CHARGEBACK_PROVIDER_URL"]
    assert url["type"] == "runtime_config"
    assert url["outcome"] == "matched"
    assert url["classification"]["precedent_scope"] == "workload"
    assert url["classification"]["evidence_files"] == ["helm/payments-api/values.yaml"]

    key = by_env["CHARGEBACK_SIGNING_KEY"]
    assert key["type"] == "secret_wiring"
    assert key["outcome"] == "human_gated"
    assert key["classification"]["evidence_files"] == ["envs/prod/payments-api/secrets/external-secret.yaml"]
```

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `/opt/homebrew/bin/pytest tests/test_runtime_config_integration.py -q`
Expected: FAIL first only if a fixture path is wrong; with the fixtures created and Tasks 2-5 done, expected PASS. If it fails, fix the fixture JSON, not the production code.

- [ ] **Step 4: Run the whole suite to confirm nothing regressed**

Run: `/opt/homebrew/bin/pytest -q`
Expected: PASS (all green, including the untouched existing tests).

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/runtime_config tests/test_runtime_config_integration.py
git commit -m "test: end-to-end runtime-config classification over fixtures"
```

---

## Follow-on (out of scope for this plan, note for the next one)

1. **Orchestrator wiring** — `mergeproof_readiness.py` must fetch PR-head changed-file *content* (not just paths) and pass it plus the already-fetched infra files into `detect_env_obligations`, then merge these obligations into the readiness report. The infra `{path: content}` mapping already exists via `fetch_infra_files` / `build_context_pack`; the new piece is fetching changed-file content at the PR head.
2. **Queue-resource second-signal gating** — raise a separate `queue_topic` obligation only when a producer/consumer signal in the changed content coincides with an existing infra queue-provisioning pattern; otherwise `unknown`. (Decision 1, half b.)
3. **Report rendering** — surface the classification (`classification`, `precedent_scope`, `evidence_files`, `reason`) and the advisory suggestion in `render_pr_readiness.py`'s support matrix.

## Self-Review

- **Spec coverage (Decision 1, half a):** env detection (Task 2), infra-precedent sensitivity with workload→repo_wide→unknown (Task 3), `runtime_config` pack distinct from `secret_wiring` (Task 4), routing + unknown→human-gate with advisory-only name table (Task 5), cited verdicts end-to-end (Task 6). Half b (queue gating) and orchestrator wiring are explicitly deferred in the Follow-on section.
- **Placeholder scan:** none — every code step contains complete, runnable code.
- **Type consistency:** `detect_env_reads` returns `{name, scope, source_file, source_line}` (Task 2), consumed by `detect_env_obligations` (Task 5). `classify_env` returns `{classification, capability, precedent_scope, evidence_files, reason}` (Task 3), stored under the obligation's `classification` key (Tasks 5, 6). `assemble_obligation(obligation_type, evidence_files, inputs, capability, pack, extra=None)` defined in Task 1 and called identically in Task 5. Obligation dict keys match `pr_obligations` output plus `classification`/`advisory_suggestion` extras.

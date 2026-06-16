# Capability-Pack Phase 2 — Generate + One-Click + Tabbed UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each `matched` obligation into a generated infra change and a one-click "Open infra PR" button in a tabbed, demo-ready static HTML report — closing the loop from "production-facing PR" to "stage the infra change."

**Architecture:** Two parts. **Part A (functional core):** a deterministic template generator (`generate_infra_change.py`) renders a pack's `generates:` outputs into files via safe `${input}` substitution; a publisher (`publish_infra_pr.py`) computes a branch name + a prefilled GitHub compare deep-link and, when not in dry-run, pushes the branch via an injectable `gh`/git runner; an orchestration helper attaches an `infra_pr` block to each matched obligation. **Part B (UI):** `render_demo_ui.py` becomes a 5-tab static report (Readiness, Production Flow, Resolve, Audit, Capability Packs) built in the spec's priority order, where Resolve shows the generated diff + proof and hosts the one-click button.

**Tech Stack:** Python 3.10+, stdlib only (`string.Template`, `urllib.parse`, `subprocess` behind an injectable runner, `html`, `json`, `pathlib`), `from __future__ import annotations`. Tests via `/opt/homebrew/bin/pytest`; lint via `ruff check plugins/ tests/` (must stay exit 0 — keep all imports at top of test files). GitHub/git calls isolated behind injectable runners (existing pattern, see `pr_architecture_risk._default_pr_runner`).

**Spec:** `docs/superpowers/specs/2026-06-16-capability-pack-infra-pr-automation-design.md`
**Builds on:** Phase 1 (`pr_obligations.py`, the `obligations` block in `render_pr_readiness.build_readiness`, payments-api fixtures). Phase 1 obligations have keys: `type, outcome, evidence_files, inputs, pack:{generates,checks,approver,human_gate}, human_gate_pending`. Phase 2 adds an `infra_pr` block to matched obligations.

**Determinism / demo note:** The demo infra repo (`acme/platform-infra`) is fictional, so the demo runs publish in **dry-run**: real generated file contents + a constructed deep-link, no live push. Live repos pass `dry_run=False` to push. "Proof" in the demo = generated diff + the pack's declared `checks`/`human_gate` (advisory), not executed checks.

---

## File Structure

| File | Responsibility | Created/Modified |
|---|---|---|
| `demo/production-readiness/payments-api/fixtures/capabilities/templates/*.tmpl` | Demo infra templates, one per `generates:` key (`worker_deployment`, `helm_worker_values`, `external_secret`, `helm_env_wiring`, `kafka_topic`). `${input}` + `${HUMAN_GATE:...}` placeholders. | Create |
| `plugins/.../scripts/generate_infra_change.py` | **New.** Render a matched obligation's pack `generates:` outputs into `{path: content}` via safe `${input}` substitution; leave `${HUMAN_GATE:...}` unfilled; enforce allowlist + required inputs. | Create |
| `plugins/.../scripts/publish_infra_pr.py` | **New.** Branch name, prefilled compare deep-link, dry-run staging vs live push via injectable runner; idempotent; returns the `infra_pr` block. | Create |
| `plugins/.../scripts/stage_obligations.py` | **New.** Orchestration: given obligations + config + repo paths, generate & (dry-run) stage each `matched` obligation and attach its `infra_pr` block. Pure-ish, runner injected. | Create |
| `plugins/.../scripts/render_pr_readiness.py` | Markdown action panel: show the infra-PR link/branch for staged obligations. | Modify |
| `plugins/.../scripts/render_demo_ui.py` | 5-tab static report; Resolve tab renders generated diff + proof + the one-click button. | Modify (significant) |
| `tests/test_generate_infra_change.py`, `tests/test_publish_infra_pr.py`, `tests/test_stage_obligations.py` | **New.** Unit tests. | Create |
| `tests/test_render_pr_readiness.py`, `tests/test_render_demo_ui.py` | Extend for the link + tabs. | Modify |

Throughout, `SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts`. Keep test imports at the TOP of each test file (ruff E402).

---

# Part A — Functional one-click core

## Task A1: Demo infra templates + pack `template_map`

**Files:**
- Create: `demo/production-readiness/payments-api/fixtures/capabilities/templates/{worker_deployment,helm_worker_values,external_secret,helm_env_wiring,kafka_topic}.tmpl`
- Modify: the three demo packs to map each `generates:` key to a template file.

- [ ] **Step 1: Create the five template files.**

`worker_deployment.tmpl`:
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${service}-${worker_name}
  labels:
    app: ${service}
    component: ${worker_name}
spec:
  replicas: 1
  template:
    spec:
      containers:
        - name: ${worker_name}
          image: ${service}:latest
          command: ["python", "-m", "app.workers.${worker_name}"]
```

`helm_worker_values.tmpl`:
```yaml
workers:
  ${worker_name}:
    enabled: true
    service: ${service}
```

`external_secret.tmpl`:
```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: ${service}-${secret_name}
spec:
  target:
    name: ${secret_name}
  data:
    - secretKey: ${secret_name}
      remoteRef:
        key: ${HUMAN_GATE:secret value provisioning}
```

`helm_env_wiring.tmpl`:
```yaml
env:
  - name: ${secret_name}
    valueFrom:
      secretKeyRef:
        name: ${secret_name}
        key: value
```

`kafka_topic.tmpl`:
```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: ${topic}
  labels:
    service: ${service}
```

- [ ] **Step 2: Add a `template_map` + `output_path` to each demo pack** so generation knows which template renders each `generates:` key and where each rendered file lands (allowlisted path). Edit `worker_deployment.yaml` to append:

```yaml
template_map:
  worker_deployment:
    template: templates/worker_deployment.tmpl
    output: envs/prod/payments-api/workers/${worker_name}-deployment.yaml
  helm_worker_values:
    template: templates/helm_worker_values.tmpl
    output: helm/payments-api/workers/${worker_name}.yaml
```

Edit `secret_wiring.yaml` to append:
```yaml
template_map:
  external_secret:
    template: templates/external_secret.tmpl
    output: envs/prod/payments-api/secrets/${secret_name}.yaml
  helm_env_wiring:
    template: templates/helm_env_wiring.tmpl
    output: helm/payments-api/env/${secret_name}.yaml
```

Edit `topic_queue.yaml` to append:
```yaml
template_map:
  kafka_topic:
    template: templates/kafka_topic.tmpl
    output: envs/prod/payments-api/topics/${topic}.yaml
```

- [ ] **Step 3: Confirm `capability_pack.load_pack` preserves the new keys.** `load_pack` returns a fixed dict and will DROP `template_map`. Open `plugins/.../scripts/capability_pack.py`, find `load_pack`, and add `template_map` to its returned dict:

```python
        "template_map": data.get("template_map") if isinstance(data.get("template_map"), dict) else {},
```
(Place it alongside the other returned keys. Do not change other behavior.)

- [ ] **Step 4: Add a regression test** in `tests/test_capability_pack.py`:

```python
def test_load_pack_preserves_template_map():
    text = (
        "capability: worker_deployment\n"
        "inputs:\n  worker_name: required\n  service: required\n"
        "generates:\n  - worker_deployment\n"
        "checks:\n  - policy\n"
        "approval:\n  required_from:\n    - platform-runtime\n"
        "template_map:\n"
        "  worker_deployment:\n"
        "    template: templates/worker_deployment.tmpl\n"
        "    output: envs/prod/x/${worker_name}.yaml\n"
    )
    from capability_pack import load_pack
    pack = load_pack(text)
    assert pack["template_map"]["worker_deployment"]["output"] == "envs/prod/x/${worker_name}.yaml"
```

- [ ] **Step 5: Verify + commit.**

Run: `/opt/homebrew/bin/pytest tests/test_capability_pack.py -q` (expect all pass)
Run: `/opt/homebrew/bin/pytest -q` and `ruff check plugins/ tests/`
```bash
git add demo/production-readiness/payments-api/fixtures/capabilities plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/capability_pack.py tests/test_capability_pack.py
git commit -m "feat: add demo infra templates and pack template_map"
```

---

## Task A2: `generate_infra_change.py` — render templates

**Files:**
- Create: `plugins/.../scripts/generate_infra_change.py`
- Test: `tests/test_generate_infra_change.py`

- [ ] **Step 1: Write the failing test.**

```python
import pytest
from generate_infra_change import generate_files, GenerateError


def _worker_pack():
    return {
        "generates": ["worker_deployment"],
        "template_map": {
            "worker_deployment": {
                "template": "templates/worker_deployment.tmpl",
                "output": "envs/prod/payments-api/workers/${worker_name}-deployment.yaml",
            }
        },
        "human_gate": None,
    }


def test_generate_substitutes_inputs(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text(
        "name: ${service}-${worker_name}\n", encoding="utf-8"
    )
    allow = ["envs/prod/payments-api/**"]
    files = generate_files(
        _worker_pack(),
        inputs={"service": "payments-api", "worker_name": "refund_worker"},
        templates_root=tmp_path,
        allow=allow,
    )
    assert files == {
        "envs/prod/payments-api/workers/refund_worker-deployment.yaml": "name: payments-api-refund_worker\n"
    }


def test_missing_required_input_raises(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text("name: ${worker_name}\n", encoding="utf-8")
    with pytest.raises(GenerateError):
        generate_files(_worker_pack(), inputs={"service": "x"}, templates_root=tmp_path, allow=["envs/**"])


def test_human_gate_placeholder_left_unfilled(tmp_path):
    pack = {
        "generates": ["external_secret"],
        "template_map": {"external_secret": {"template": "templates/s.tmpl", "output": "envs/prod/s.yaml"}},
        "human_gate": "secret value provisioning",
    }
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "s.tmpl").write_text("ref: ${HUMAN_GATE:secret value provisioning}\n", encoding="utf-8")
    files = generate_files(pack, inputs={}, templates_root=tmp_path, allow=["envs/**"])
    body = files["envs/prod/s.yaml"]
    assert "TODO-HUMAN: secret value provisioning" in body


def test_output_path_outside_allowlist_is_error(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "worker_deployment.tmpl").write_text("x\n", encoding="utf-8")
    with pytest.raises(GenerateError):
        generate_files(_worker_pack(), inputs={"service": "x", "worker_name": "w"},
                       templates_root=tmp_path, allow=["helm/**"])  # output is envs/**, not allowed
```

- [ ] **Step 2: Run, confirm FAIL** (`ModuleNotFoundError`).
Run: `/opt/homebrew/bin/pytest tests/test_generate_infra_change.py -v`

- [ ] **Step 3: Implement `generate_infra_change.py`.**

```python
#!/usr/bin/env python3
"""Render a matched obligation's Capability Pack into infra files.

Deterministic, zero-dependency. Each ``generates:`` key maps (via the pack's
``template_map``) to a template file and an output path. Templates use
``string.Template`` ``${input}`` substitution. A ``${HUMAN_GATE:reason}`` marker
is never filled with a real value — it is replaced with a greppable
``TODO-HUMAN: reason`` placeholder so a human must complete it before merge.

Never writes to disk or any repo; returns ``{output_path: content}``. Writing /
pushing is the publisher's job.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

_HUMAN_GATE_RE = re.compile(r"\$\{HUMAN_GATE:([^}]*)\}")


class GenerateError(ValueError):
    """Raised when a template cannot be rendered safely."""


def _allowed(path: str, allow: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in allow)


def _render(template_text: str, inputs: dict[str, str]) -> str:
    # Replace human-gate markers FIRST so they survive ${...} substitution.
    text = _HUMAN_GATE_RE.sub(lambda m: f"TODO-HUMAN: {m.group(1)}", template_text)
    try:
        return string.Template(text).substitute(inputs)
    except KeyError as exc:
        raise GenerateError(f"template requires input {exc} which was not provided") from exc
    except ValueError as exc:
        raise GenerateError(f"malformed template placeholder: {exc}") from exc


def _subst_path(path_template: str, inputs: dict[str, str]) -> str:
    try:
        return string.Template(path_template).substitute(inputs)
    except KeyError as exc:
        raise GenerateError(f"output path requires input {exc} which was not provided") from exc


def generate_files(
    pack: dict[str, Any],
    inputs: dict[str, str],
    templates_root: str | Path,
    allow: list[str],
) -> dict[str, str]:
    """Return ``{output_path: rendered_content}`` for every ``generates:`` key."""
    templates_root = Path(templates_root)
    template_map = pack.get("template_map") or {}
    result: dict[str, str] = {}
    for key in pack.get("generates") or []:
        entry = template_map.get(key)
        if not isinstance(entry, dict) or "template" not in entry or "output" not in entry:
            raise GenerateError(f"pack has no template_map entry for generates key '{key}'")
        template_path = templates_root / entry["template"]
        try:
            template_text = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GenerateError(f"cannot read template {entry['template']}: {exc}") from exc
        output_path = _subst_path(entry["output"], inputs)
        if not _allowed(output_path, allow):
            raise GenerateError(f"generated path '{output_path}' is outside the allowlist")
        result[output_path] = _render(template_text, inputs)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a Capability Pack's infra files (no writes).")
    parser.add_argument("--pack", required=True, help="Path to a capability pack YAML file.")
    parser.add_argument("--inputs", required=True, help="JSON object of input values.")
    parser.add_argument("--templates-root", required=True, help="Directory holding the pack's templates.")
    parser.add_argument("--allow", required=True, help="Comma-separated allowlist globs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    from capability_pack import load_pack

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        pack = load_pack(Path(args.pack).read_text(encoding="utf-8"))
        inputs = json.loads(args.inputs)
        files = generate_files(pack, inputs, args.templates_root, args.allow.split(","))
    except (OSError, ValueError, GenerateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(files, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run targeted + full suite + ruff.**
Run: `/opt/homebrew/bin/pytest tests/test_generate_infra_change.py -v`
Run: `/opt/homebrew/bin/pytest -q` ; `ruff check plugins/ tests/`

- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/generate_infra_change.py tests/test_generate_infra_change.py
git commit -m "feat: generate infra files from a capability pack via safe substitution"
```

---

## Task A3: `publish_infra_pr.py` — branch name + deep-link + dry-run staging

**Files:**
- Create: `plugins/.../scripts/publish_infra_pr.py`
- Test: `tests/test_publish_infra_pr.py`

- [ ] **Step 1: Write the failing test.**

```python
from publish_infra_pr import branch_name, compare_url, stage_branch


def test_branch_name_is_deterministic_and_safe():
    assert branch_name("worker_deployment", {"worker_name": "refund_worker"}) == "mergeproof/worker_deployment-refund_worker"
    assert branch_name("secret_wiring", {"secret_name": "stripe/webhook"}) == "mergeproof/secret_wiring-stripe-webhook"


def test_compare_url_encodes_title_and_body():
    url = compare_url(
        repo="acme/platform-infra", base="main", branch="mergeproof/worker_deployment-refund_worker",
        title="MergeProof: worker_deployment for payments-api",
        body="Approver @platform-runtime\nSource: https://github.com/o/r/pull/1",
    )
    assert url.startswith("https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?")
    assert "expand=1" in url
    assert "title=MergeProof%3A+worker_deployment+for+payments-api" in url
    assert "%40platform-runtime" in url  # @ encoded in body


def test_stage_branch_dry_run_does_not_push():
    calls = []
    def runner(args):
        calls.append(args)
        return ""
    result = stage_branch(
        repo="acme/platform-infra", base="main", branch="mergeproof/x",
        files={"envs/prod/a.yaml": "x: 1\n"},
        commit_message="msg", dry_run=True, runner=runner,
    )
    assert result["pushed"] is False
    assert result["generated_files"] == ["envs/prod/a.yaml"]
    assert calls == []  # dry-run performs no git/gh calls


def test_stage_branch_live_push_invokes_runner():
    calls = []
    def runner(args):
        calls.append(args)
        return ""
    result = stage_branch(
        repo="acme/platform-infra", base="main", branch="mergeproof/x",
        files={"envs/prod/a.yaml": "x: 1\n"},
        commit_message="msg", dry_run=False, runner=runner,
    )
    assert result["pushed"] is True
    assert any("push" in " ".join(c) for c in calls)
```

- [ ] **Step 2: Run, confirm FAIL.**
Run: `/opt/homebrew/bin/pytest tests/test_publish_infra_pr.py -v`

- [ ] **Step 3: Implement `publish_infra_pr.py`.**

```python
#!/usr/bin/env python3
"""Stage a generated infra change as a branch + prefilled PR-create deep-link.

Deterministic branch naming and URL construction. The actual git/gh work is
isolated behind an injectable *runner* so it is fully unit-testable, and skipped
entirely in ``dry_run`` (used by the offline demo, where the infra repo may be
fictional). Returns the ``infra_pr`` block attached to a matched obligation.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

Runner = Callable[[list[str]], str]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def branch_name(obligation_type: str, inputs: dict[str, str]) -> str:
    primary = ""
    for key in ("worker_name", "secret_name", "topic", "service"):
        if inputs.get(key):
            primary = inputs[key]
            break
    return f"mergeproof/{obligation_type}-{_slug(primary)}" if primary else f"mergeproof/{obligation_type}"


def compare_url(repo: str, base: str, branch: str, title: str, body: str) -> str:
    query = urlencode({"expand": "1", "title": title, "body": body}, quote_via=_quote_plus_keep)
    return f"https://github.com/{repo}/compare/{base}...{branch}?{query}"


def _quote_plus_keep(string_value: str, safe: str = "", encoding: str | None = None, errors: str | None = None) -> str:
    from urllib.parse import quote_plus
    return quote_plus(string_value)


def _default_runner(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git failed").strip())
    return proc.stdout


def stage_branch(
    repo: str,
    base: str,
    branch: str,
    files: dict[str, str],
    commit_message: str,
    dry_run: bool = True,
    runner: Runner = _default_runner,
) -> dict[str, Any]:
    """Stage *files* on *branch*. In dry_run, perform no git calls."""
    generated = sorted(files)
    if dry_run:
        return {"repo": repo, "base": base, "branch": branch, "pushed": False, "generated_files": generated}
    # Live path: clone the infra repo to a temp dir, write files, commit, push the branch.
    with tempfile.TemporaryDirectory() as tmp:
        runner(["clone", "--depth", "1", "--branch", base, f"https://github.com/{repo}.git", tmp])
        runner(["-C", tmp, "checkout", "-B", branch])
        for rel, content in files.items():
            dest = Path(tmp) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            runner(["-C", tmp, "add", rel])
        runner(["-C", tmp, "commit", "-m", commit_message])
        runner(["-C", tmp, "push", "--force", "origin", branch])
    return {"repo": repo, "base": base, "branch": branch, "pushed": True, "generated_files": generated}
```

(The `_quote_plus_keep` shim lets `urlencode` use `quote_plus` for spaces→`+`. If your reviewer prefers, replace with `urlencode({...}, quote_via=quote_plus)` importing `quote_plus` at module top — functionally identical; keep whichever passes the URL test.)

- [ ] **Step 4: Run targeted + full suite + ruff.**

- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/publish_infra_pr.py tests/test_publish_infra_pr.py
git commit -m "feat: stage infra branch + prefilled compare deep-link (dry-run safe)"
```

---

## Task A4: `stage_obligations.py` — attach `infra_pr` to matched obligations

**Files:**
- Create: `plugins/.../scripts/stage_obligations.py`
- Test: `tests/test_stage_obligations.py`

- [ ] **Step 1: Write the failing test.**

```python
from stage_obligations import stage_obligations


def _matched_ob():
    return {
        "type": "worker_deployment", "outcome": "matched",
        "evidence_files": ["app/workers/refund_worker.py"],
        "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
        "pack": {"generates": ["worker_deployment"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None,
                 "template_map": {"worker_deployment": {"template": "templates/w.tmpl", "output": "envs/prod/payments-api/workers/${worker_name}.yaml"}}},
        "human_gate_pending": [],
    }


def test_matched_obligation_gets_infra_pr_block(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "w.tmpl").write_text("name: ${service}-${worker_name}\n", encoding="utf-8")
    obligations = [_matched_ob()]
    out = stage_obligations(
        obligations, repo="acme/platform-infra", base="main",
        allow=["envs/prod/payments-api/**"], templates_root=tmp_path,
        source_pr="https://github.com/o/r/pull/1", dry_run=True,
    )
    infra = out[0]["infra_pr"]
    assert infra["pushed"] is False
    assert infra["generated_files"] == ["envs/prod/payments-api/workers/refund_worker.yaml"]
    assert infra["branch"] == "mergeproof/worker_deployment-refund_worker"
    assert "compare/main...mergeproof/worker_deployment-refund_worker" in infra["create_url"]
    assert infra["diff"]["envs/prod/payments-api/workers/refund_worker.yaml"].startswith("name: payments-api-refund_worker")


def test_human_gated_and_blocked_are_left_unstaged(tmp_path):
    obligations = [
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": [], "inputs": {}, "pack": {}, "human_gate_pending": ["x"]},
        {"type": "worker_deployment", "outcome": "blocked", "evidence_files": [], "inputs": {}, "pack": None, "human_gate_pending": []},
    ]
    out = stage_obligations(obligations, repo="r", base="main", allow=["**"], templates_root=tmp_path,
                            source_pr="u", dry_run=True)
    assert "infra_pr" not in out[0]
    assert "infra_pr" not in out[1]
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Implement `stage_obligations.py`.**

```python
#!/usr/bin/env python3
"""Attach an ``infra_pr`` block to each ``matched`` obligation.

Glue between the detector (Phase 1), the generator, and the publisher: for every
matched obligation it generates the infra files, computes the branch + deep-link,
and (in dry_run) stages without pushing. human_gated / blocked obligations are
left untouched — there is nothing safe to stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from generate_infra_change import generate_files
from publish_infra_pr import branch_name, compare_url, stage_branch, _default_runner


def stage_obligations(
    obligations: list[dict[str, Any]],
    repo: str,
    base: str,
    allow: list[str],
    templates_root: str | Path,
    source_pr: str,
    dry_run: bool = True,
    runner: Callable[[list[str]], str] = _default_runner,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for ob in obligations:
        ob = dict(ob)
        if ob.get("outcome") == "matched":
            pack = ob.get("pack") or {}
            files = generate_files(pack, ob.get("inputs") or {}, templates_root, allow)
            branch = branch_name(ob["type"], ob.get("inputs") or {})
            approver = pack.get("approver") or ""
            title = f"MergeProof: {ob['type']} for {(ob.get('inputs') or {}).get('service', 'service')}"
            body = (
                f"Generated by MergeProof for {source_pr}\n\n"
                f"Approver: {approver}\n"
                f"Generated files:\n" + "\n".join(f"- {p}" for p in sorted(files))
            )
            staged = stage_branch(repo, base, branch, files, title, dry_run=dry_run, runner=runner)
            staged["create_url"] = compare_url(repo, base, branch, title, body)
            staged["diff"] = files
            ob["infra_pr"] = staged
        out.append(ob)
    return out
```

- [ ] **Step 4: Run targeted + full suite + ruff.**

- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/stage_obligations.py tests/test_stage_obligations.py
git commit -m "feat: stage matched obligations into infra_pr blocks"
```

---

## Task A5: Show the infra-PR link in the Markdown action panel

**Files:**
- Modify: `plugins/.../scripts/render_pr_readiness.py` (`_render_obligations`)
- Test: `tests/test_render_pr_readiness.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_markdown_action_shows_infra_pr_link_when_staged():
    obligations = [{
        "type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
        "inputs": {"worker_name": "refund_worker", "service": "payments-api"},
        "pack": {"generates": ["worker_deployment"], "checks": ["policy"], "approver": "@platform-runtime", "human_gate": None},
        "human_gate_pending": [],
        "infra_pr": {"repo": "acme/platform-infra", "branch": "mergeproof/worker_deployment-refund_worker",
                      "create_url": "https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1",
                      "pushed": False, "generated_files": ["envs/prod/payments-api/workers/refund_worker.yaml"]},
    }]
    r = build_readiness(_PASS_LOOP, _ARCH, _NO_RISK, obligations=obligations)
    md = render_markdown(r)
    assert "Open infra PR" in md
    assert "compare/main...mergeproof/worker_deployment-refund_worker" in md
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Update `_render_obligations`** — in the matched branch (the `else:` arm), when `ob.get("infra_pr", {}).get("create_url")` exists, append a markdown link. Replace the matched `else:` action assignment with:

```python
        else:
            generates = ", ".join(pack.get("generates") or [])
            infra_pr = ob.get("infra_pr") or {}
            url = infra_pr.get("create_url")
            if url:
                action = f"generates {generates} — [Open infra PR ▸]({url}) (branch `{infra_pr.get('branch', '')}`)."
            else:
                action = f"{files} — generates {generates}. (infra PR not staged)."
```

- [ ] **Step 4: Run targeted + full suite + ruff.**

- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_pr_readiness.py tests/test_render_pr_readiness.py
git commit -m "feat: link the staged infra PR from the obligation action panel"
```

---

# Part B — Tabbed static UI (priority-ordered)

`render_demo_ui.render_html` becomes a 5-tab report. Tabs are CSS-only radio toggles (no network, no JS dependency for switching — uses the classic hidden-radio + `:checked` sibling pattern, keeping the file self-contained and offline). Build in priority order; do not over-invest in Tabs 4–5.

## Task B1: Tab scaffold + Readiness tab (Priority 1)

**Files:**
- Modify: `plugins/.../scripts/render_demo_ui.py`
- Test: `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write the failing test.**

```python
from render_demo_ui import render_html

_READY = {
    "status": "HUMAN_DECISION_REQUIRED", "status_label": "HUMAN DECISION REQUIRED",
    "reason": "tests passed but production-facing", "pr_url": "https://github.com/o/r/pull/1",
    "evidence": {"findings_fixed": 5, "verification": "passed", "verification_command": "pytest", "rereview": "completed", "cycles_used": 2, "cycles_total": 3, "false_positives_skipped": 1},
    "architecture": {"service_name": "payments-api", "exposure": "public", "ingress": [], "datastores": [], "queues": ["redis:arq"], "owners": [], "runtime": "kubernetes"},
    "production_risks": [], "obligations": [], "human_decision": {"required": True, "review_points": []}, "next_options": ["Approve"],
}


def test_html_has_five_tab_nav_and_readiness_panel():
    html = render_html(_READY)
    for label in ["Readiness", "Production Flow", "Resolve", "Audit", "Capability Packs"]:
        assert label in html
    assert 'id="tab-readiness"' in html
    assert "HUMAN DECISION REQUIRED" in html  # readiness verdict still rendered
    assert "<script" not in html  # stays JS-free
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Implement the scaffold.** In `render_demo_ui.py`:
  1. Append the tab CSS to the `_CSS` string:
```css
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
```
  2. Refactor `render_html` so the existing readiness content (banner, evidence cards, arch, risks, decision) is wrapped in `<section class="tabpanel" id="tab-readiness">...</section>`, and add the radio inputs + tab nav + empty panels for the other four. The body becomes:

```python
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
```

Then assemble: header → `tabs_nav` → `<section class="tabpanel" id="tab-readiness">{existing content}</section>` → `_flow_tab(readiness)` → `_resolve_tab(readiness)` → `_audit_tab(readiness)` → `_packs_tab(readiness)` → footer. For B1, the other four `_*_tab` helpers return a minimal `<section class="tabpanel" id="tab-flow">...</section>` stub each (just the heading); B2–B5 fill them. The radio inputs MUST be siblings preceding `.tabs` and the panels (the `~` selector requires sibling order).

- [ ] **Step 4: Run targeted + full suite + ruff.** Verify no `<script` and all five labels present.

- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_demo_ui.py tests/test_render_demo_ui.py
git commit -m "feat: 5-tab static report scaffold with Readiness tab"
```

---

## Task B2: Production Flow tab — obligations on the flow (Priority 2, the wow)

**Files:**
- Modify: `plugins/.../scripts/render_demo_ui.py` (`_flow_tab`)
- Test: `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_flow_tab_places_obligations_on_flow():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker"}, "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment"]},
         "human_gate_pending": [], "infra_pr": {"branch": "mergeproof/worker_deployment-refund_worker", "create_url": "https://x", "pushed": False, "generated_files": ["a.yaml"]}},
    ]
    html = render_html(readiness)
    assert 'id="tab-flow"' in html
    assert "refund_worker" in html
    assert "@platform-runtime" in html
    assert "worker_deployment" in html
```

- [ ] **Step 2: Run, confirm FAIL** (refund_worker not yet in flow panel).

- [ ] **Step 3: Implement `_flow_tab`.** Render the architecture flow (reuse `_arch_flow`) and below it a node per obligation, tagged by outcome, showing type · primary input · approver:

```python
_FLOW_OUTCOME_CLASS = {"matched": "node service", "human_gated": "node exposure", "blocked": "node"}


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
```
Add CSS to `_CSS`:
```css
.obl-grid { display: grid; gap: 10px; }
.obl-node { background: #0f1a2e; border: 1px solid #1e293b; border-radius: 10px; padding: 12px 14px; }
.obl-meta { color: #94a3b8; font-size: 13px; margin-top: 6px; }
```

- [ ] **Step 4: Run targeted + full suite + ruff.**
- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_demo_ui.py tests/test_render_demo_ui.py
git commit -m "feat: Production Flow tab places obligations on the architecture flow"
```

---

## Task B3: Resolve tab — generated diff + proof + the one-click button (Priority 3)

**Files:**
- Modify: `plugins/.../scripts/render_demo_ui.py` (`_resolve_tab`)
- Test: `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_resolve_tab_shows_diff_proof_and_button():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"],
         "inputs": {"worker_name": "refund_worker"}, "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment"], "checks": ["helm_template", "policy"], "human_gate": None},
         "human_gate_pending": [],
         "infra_pr": {"branch": "mergeproof/worker_deployment-refund_worker",
                       "create_url": "https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1",
                       "pushed": False, "generated_files": ["envs/prod/payments-api/workers/refund_worker.yaml"],
                       "diff": {"envs/prod/payments-api/workers/refund_worker.yaml": "kind: Deployment\nname: payments-api-refund_worker\n"}}},
    ]
    html = render_html(readiness)
    assert 'id="tab-resolve"' in html
    assert "kind: Deployment" in html                      # generated diff shown
    assert "helm_template" in html and "policy" in html    # proof: declared checks
    assert 'href="https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?expand=1"' in html
    assert "Open infra PR" in html                          # the button
    assert html.count("Open infra PR") >= 1


def test_resolve_tab_human_gated_shows_pending_not_button():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "secret_wiring", "outcome": "human_gated", "evidence_files": ["app/secrets/stripe_webhook.py"],
         "inputs": {"secret_name": "stripe_webhook"}, "pack": {"approver": "@platform-secrets", "generates": ["external_secret"], "checks": ["policy"], "human_gate": "secret value provisioning"},
         "human_gate_pending": ["secret value provisioning", "input: env_var"]},
    ]
    html = render_html(readiness)
    assert "secret value provisioning" in html
    assert "input: env_var" in html
    # no staged infra_pr -> no button for this obligation
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Implement `_resolve_tab`.**

```python
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
```
Add CSS:
```css
.resolve-card { background: #0f1a2e; border: 1px solid #1e293b; border-radius: 12px; padding: 16px 18px; margin-bottom: 14px; }
.resolve-head { font-weight: 700; font-size: 15px; }
.diff-path { color: #7dd3fc; font-size: 13px; margin-top: 10px; font-family: ui-monospace, Menlo, monospace; }
pre.diff { background: #0a1322; border: 1px solid #1a2740; border-radius: 8px; padding: 12px 14px; overflow-x: auto; font-size: 12.5px; color: #cbd5e1; }
.btn { display: inline-block; margin-top: 12px; padding: 9px 16px; background: var(--accent); color: #0b1220; font-weight: 700; border-radius: 8px; text-decoration: none; }
.gate { margin-top: 12px; color: #fbbf24; font-weight: 650; }
```

- [ ] **Step 4: Run targeted + full suite + ruff.**
- [ ] **Step 5: Commit.**
```bash
git add plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/render_demo_ui.py tests/test_render_demo_ui.py
git commit -m "feat: Resolve tab renders generated diff, proof, and one-click infra PR button"
```

---

## Task B4: Audit tab — simple chronological list (Priority 4, keep simple)

**Files:**
- Modify: `plugins/.../scripts/render_demo_ui.py` (`_audit_tab`)
- Test: `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_audit_tab_lists_evidence_and_obligations_simply():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": ["app/workers/refund_worker.py"], "inputs": {}, "pack": {"approver": "@platform-runtime"}, "human_gate_pending": []},
    ]
    html = render_html(readiness)
    assert 'id="tab-audit"' in html
    assert "app/workers/refund_worker.py" in html
    assert "worker_deployment" in html
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Implement `_audit_tab`** — a plain ordered list of events from existing data (loop evidence summary + one row per obligation with its evidence files). Keep it deliberately minimal:

```python
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
```
Add CSS: `.audit { color: #cbd5e1; font-size: 14px; line-height: 1.9; }`

- [ ] **Step 4: Run + ruff. Step 5: Commit** `feat: simple Audit tab`.

---

## Task B5: Capability Packs tab — read-only (Priority 5, lightweight)

**Files:**
- Modify: `plugins/.../scripts/render_demo_ui.py` (`_packs_tab`)
- Test: `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write the failing test.**

```python
def test_packs_tab_lists_declared_capabilities_readonly():
    readiness = dict(_READY)
    readiness["obligations"] = [
        {"type": "worker_deployment", "outcome": "matched", "evidence_files": [], "inputs": {},
         "pack": {"approver": "@platform-runtime", "generates": ["worker_deployment", "helm_worker_values"], "checks": ["policy"], "human_gate": None}, "human_gate_pending": []},
    ]
    html = render_html(readiness)
    assert 'id="tab-packs"' in html
    assert "helm_worker_values" in html
    assert "@platform-runtime" in html
    assert "<form" not in html and "<button" not in html  # read-only, no inputs
```

- [ ] **Step 2: Run, confirm FAIL.**

- [ ] **Step 3: Implement `_packs_tab`** — a read-only table of the packs referenced by the obligations (type, generates, checks, approver, human_gate). Deduplicate by type:

```python
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
```

- [ ] **Step 4: Run + ruff. Step 5: Commit** `feat: read-only Capability Packs tab`.

---

## Task B6: End-to-end demo regenerate + investor-acceptance gate

**Files:**
- Modify: `demo/production-readiness/payments-api/` committed artifacts (new `readiness.json`, `pr_readiness_report.html`)
- Test: one integration test in `tests/test_render_demo_ui.py`

- [ ] **Step 1: Write an integration test** that runs detector → stage → readiness → HTML end-to-end on the payments-api fixtures and asserts the five-point investor story is present in the HTML (status, obligations seen, on the flow, generated diff, human-owned gate):

```python
def test_payments_api_demo_html_tells_the_five_point_story(tmp_path):
    import json
    from pathlib import Path
    from pr_obligations import load_capabilities_and_packs, _read_changed, detect_obligations
    from stage_obligations import stage_obligations
    from render_pr_readiness import build_readiness
    root = Path(__file__).resolve().parent.parent
    F = root / "demo" / "production-readiness" / "payments-api" / "fixtures"
    caps, packs, service = load_capabilities_and_packs(F / "mergeproof.yaml")
    changed = _read_changed(str(F / "changed_files.json"))
    obligations = detect_obligations(changed, caps, packs, service=service)
    obligations = stage_obligations(
        obligations, repo="acme/platform-infra", base="main",
        allow=["envs/prod/payments-api/**", "helm/payments-api/**"],
        templates_root=F / "capabilities", source_pr="https://github.com/acme/payments-api/pull/428", dry_run=True,
    )
    arch = {"service_name": service, "exposure": "public", "queues": ["redis:arq"], "ingress": [], "datastores": [], "owners": [], "runtime": "kubernetes"}
    risks = {"production_risks": [], "summary": {"highest_severity": "none", "human_decision_required": False, "risk_count": 0}}
    loop = json.loads((F / "loop_summary.json").read_text())
    readiness = build_readiness(loop, arch, risks, obligations=obligations)
    html = render_html(readiness)
    assert readiness["status"] == "HUMAN_DECISION_REQUIRED"   # 1: green PR still unsafe
    assert "worker_deployment" in html                         # 2 & 3: obligation seen + on flow
    assert "kind: Deployment" in html                          # 4: generated change
    assert "Open infra PR" in html                             # 4: one-click
    assert "secret value provisioning" in html                 # 5: human owns risk
```

- [ ] **Step 2: Run it (PASS).** Run: `/opt/homebrew/bin/pytest tests/test_render_demo_ui.py::test_payments_api_demo_html_tells_the_five_point_story -v`

- [ ] **Step 3: Regenerate the committed demo artifacts** (run the same pipeline via a short script or the CLIs) and write `demo/production-readiness/payments-api/readiness.json` + `pr_readiness_report.html`. Open the HTML to eyeball all five tabs render.

- [ ] **Step 4: Full gates.** Run: `/opt/homebrew/bin/pytest -q` (fully green) ; `ruff check plugins/ tests/` (exit 0).

- [ ] **Step 5: Commit.**
```bash
git add demo/production-readiness/payments-api tests/test_render_demo_ui.py
git commit -m "test: end-to-end payments-api demo tells the five-point story"
```

---

## Phase 2 Definition of Done

- [ ] `generate_infra_change.py` renders pack `generates:` outputs via `${input}` substitution; missing required input errors; `${HUMAN_GATE:...}` left as `TODO-HUMAN`; out-of-allowlist path errors.
- [ ] `publish_infra_pr.py` produces a deterministic branch + prefilled compare deep-link; dry-run performs no git calls; live push goes through the injectable runner; idempotent (force branch).
- [ ] `stage_obligations.py` attaches `infra_pr` (incl. `diff`, `create_url`) to matched obligations only.
- [ ] Markdown action panel links the staged infra PR.
- [ ] `render_demo_ui.py` is a JS-free 5-tab report: Readiness, Production Flow (obligations on the flow), Resolve (diff + proof + one-click button), simple Audit, read-only Capability Packs.
- [ ] Investor acceptance: the demo HTML lets a viewer narrate the five-point story unaided.
- [ ] Demo reproducible offline from payments-api fixtures; full suite green; ruff exit 0.

**After Phase 2:** the spec's Phase 3 (GitHub-App wedge — live mutation, comments on app PRs, opens linked infra PRs) remains future work and is intentionally NOT in this plan.

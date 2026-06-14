# Production-Aware PR Readiness

> The AI runs the loop. The human owns production risk and merge judgment.

GGRL already drives the AI review loop — fetch Gemini findings, classify them,
fix the actionable ones, run repo-aware verification, re-request review, and
summarize the terminal state. This feature adds the missing production context:

> Did this PR touch production-facing surfaces, and should that change the
> merge-readiness decision?

It produces three artifacts from the same data:

1. a **PR Readiness Card** (GitHub Markdown)
2. an optional **GitHub PR comment** (posted/updated in place)
3. a **polished static HTML report** for demos and screen shares

Everything is **advisory-only** and runs with **no cloud credentials**. The
architecture scan reads static files already in the repo; nothing connects to a
cloud account, runs Terraform, or merges anything.

---

## MergeProof cross-repo MVP (familia-ai PR #106)

Production reality usually lives in a **separate infra repo**, not the app repo.
MergeProof bridges them with a trusted `mergeproof.yaml` in the app repo
(read from the base branch, never PR head) that declares the infra repo + an
allowlist of paths. The app-declared config is **token-governed**: the GitHub
token / App installation decides whether those paths can actually be read.

Reference demo:

| | |
|---|---|
| App repo | `OrenAshkenazy/familia-ai` |
| Mock PR | `https://github.com/OrenAshkenazy/familia-ai/pull/106` |
| Service | `familia-ai` |
| Infra source | `OrenAshkenazy/familia-ai-infra` |

PR #106 changes `backend/app/routers/scraper_connectors.py` (public API /
connector surface → **high**) and `backend/app/jobs/worker.py` (ARQ async worker
→ **medium**). Because tests pass but both surfaces are production-facing, the
expected outcome is **`HUMAN_DECISION_REQUIRED`** (or `VERIFICATION_FAILED` if
verification failed). A committed render of this exact outcome lives in
[`familia/`](familia) (`readiness.md`, `readiness.json`,
`production_context_pack.json`, `pr_readiness_report.html`).

### 1. Generate config (onboarding, not PR runtime)

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts

python3 $SCRIPTS/init_mergeproof.py \
  --service familia-ai \
  --infra-repo OrenAshkenazy/familia-ai-infra \
  --env prod \
  --output /tmp/mergeproof.yaml
```

`init_mergeproof.py` only *proposes* a config. A human reviews it and commits it
to the app repo's trusted base branch before readiness uses it.

### 2. Run readiness against PR #106 (after the CR loop's terminal summary)

```bash
python3 $SCRIPTS/mergeproof_readiness.py \
  --pr https://github.com/OrenAshkenazy/familia-ai/pull/106 \
  --loop-summary /tmp/loop_summary.json \
  --mergeproof /tmp/mergeproof.yaml \
  --json > /tmp/mergeproof_readiness.json
```

`--markdown` renders the GitHub-native card instead; `--publish` posts/updates a
single PR comment keyed by the hidden marker `<!-- mergeproof-pr-readiness -->`
(never duplicates). Publishing is opt-in.

`--mergeproof <file>` supplies a trusted config explicitly (handy offline). Omit
it to read `mergeproof.yaml`/`.json` from the PR's base branch. If the PR itself
edits the config, MergeProof reports `CONFIG_CHANGED_REVIEW_REQUIRED` and uses
the base-branch config anyway; `--trust-pr-config` (off by default) overrides
that.

> If the infra repo is not accessible to your token, infra fetch degrades to a
> partial pack (recorded in `safety.failed_sources`) rather than crashing. The
> committed `familia/` artifacts are rendered from a fixture-backed run so the
> demo outcome is reproducible without infra access.

---

## Real PR flow (primary)

This is the path teams use. It runs against a real repository and a real PR.

### Prerequisites

- Claude Code with this plugin installed.
- `gh` CLI authenticated with read access to the app repo and any configured
  infra repo. For same-repo demos, one repo permission is enough.
- Gemini Code Assist configured on the target PR.
- A real GitHub PR URL.
- A trusted `mergeproof.yaml` or `mergeproof.json` on the base branch. In the
  demo, generate it live with `mergeproof init`, open a small bootstrap PR,
  merge that PR, then run normal readiness on the app-only PR.
- A terminal CR loop summary, produced by the existing Gemini loop. In normal
  agent use this is created during steps 1-9. For manual readiness-only replay,
  use either `--loop-summary /path/to/loop_summary.json` or
  `--runs-jsonl ~/.config/gh-gemini-review-loop/runs.jsonl`.
- Python 3.10+.

From Claude Code, the product-level command is:

```text
mergeproof run --pr https://github.com/OWNER/REPO/pull/123
```

That command means: run the existing Gemini CR loop to a terminal summary, then
run the MergeProof readiness phase. The Python readiness scripts are internal
phase tools; users should not have to start from `render_pr_readiness.py`.

Demo stage 0: generate the trusted config:

```text
mergeproof init --repo-root /path/to/repo --repo OWNER/REPO --service SERVICE
```

For the `familia-ai` same-repo demo, the shell helper is:

```bash
python3 $SCRIPTS/mergeproof.py init \
  --repo-root /Users/orenashkenazy/dev/familia-ai \
  --repo OrenAshkenazy/familia-ai \
  --service familia-ai
```

Open and merge that generated config PR first. This config PR is the only demo
step that adds `mergeproof.yaml`. The follow-up app PR should not include
`mergeproof.yaml` or infra changes just to make readiness work; MergeProof reads
infra context from the trusted base config and reports which infra aspects the
app change may affect.

Live demo order:

```text
Config PR:
mergeproof init -> commit mergeproof.yaml -> open PR -> merge to main

App PR:
change app code only -> mergeproof run --pr <APP_PR_URL>
```

End-to-end sequence:

```text
1. Fetch PR metadata
2. Fetch Gemini review threads
3. Judge/classify findings
4. Claude fixes actionable findings
5. Run repo verification
6. Push fixes if needed
7. Request Gemini re-review
8. Wait/check re-review result
9. Stop at CR loop terminal summary
10. If mergeproof.yaml exists, start readiness phase
11. Read trusted mergeproof.yaml from base branch
12. Detect if PR changed mergeproof.yaml
13. Resolve infra sources from config
14. Resolve infra refs to immutable SHAs
15. Fetch only allowlisted infra files
16. Enforce file count, size, binary, and truncation safety
17. Extract normalized architecture facts
18. Build Production Context Pack
19. Map PR changed files to production risks
20. Combine loop evidence + production risks
21. Compute readiness status
22. Render PR Readiness Card
23. Publish/update single GitHub PR comment
24. Optionally render static HTML report
```

The boundaries are:

```text
CR loop:
Did AI address review feedback and pass verification?

Production Context Pack:
What is this service in production?

Risk overlay:
What did this PR touch that matters in production?

Readiness Card:
Can this PR safely merge, or does a human need to decide?
```

## Cross-repo model (mergeproof.yaml)

Production reality usually lives in an infra repo, not the app repo. The app
repo carries a `mergeproof.yaml` (read from the trusted base branch, never PR
head) that declares the infra repo and an allowlist of paths. A monorepo works
the same way: point `repo` at the app repo itself and allowlist the production
Helm/Terraform paths.

The committed demo uses `https://github.com/OrenAshkenazy/familia-ai`, where the
app code and production stack live in the same repository:

```yaml
version: 1
service: familia-ai
architecture_sources:
  - repo: OrenAshkenazy/familia-ai
    ref: main
    allow:
      - helm/familia-ai/values.yaml
      - helm/familia-ai/templates/backend/**
      - helm/familia-ai/templates/worker/**
      - helm/familia-ai/templates/redis/**
      - helm/familia-ai/templates/ingress.yaml
      - infra/terraform/environments/beta/**
      - infra/terraform/modules/alb/**
      - infra/terraform/modules/ecs/**
      - infra/terraform/modules/rds/**
      - infra/terraform/modules/efs/**
      - backend/app/jobs/**
```

The trusted-input model is:

```text
PR changed files -> from PR head
mergeproof.yaml  -> trusted base branch by default
infra files      -> allowlisted paths from trusted config
```

The readiness phase runs after the review loop reaches a terminal state. If you
are driving the internals manually, it consumes existing loop output; it does
not rerun the CR loop:

| Command | End-to-end step(s) | What it does |
|---|---:|---|
| `mergeproof init --repo-root <repo> --repo <OWNER/REPO> --service <name>` | pre-req | Generates the trusted `mergeproof.yaml` bootstrap config. |
| `mergeproof run --pr <PR_URL>` | 1-24 | Product flow: Claude runs the CR loop, then starts readiness. |
| `python3 $SCRIPTS/mergeproof.py run --pr <PR_URL> --runs-jsonl ... --publish` | 10-23 | Manual replay of readiness after a terminal loop record already exists. |
| `python3 $SCRIPTS/mergeproof.py run --pr <PR_URL> --loop-summary ... --json-output ...` | 10-22 | Builds readiness JSON from an explicit terminal loop summary. |
| `python3 $SCRIPTS/render_demo_ui.py --readiness ...` | 24 | Optional static HTML rendering from readiness JSON. |

Set paths once:

```bash
GGRL_ROOT=/Users/orenashkenazy/dev/gh-gemini-review-loop
SCRIPTS=$GGRL_ROOT/plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
```

Manual readiness replay from a recorded terminal run:

```bash
python3 $SCRIPTS/mergeproof.py run \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --runs-jsonl ~/.config/gh-gemini-review-loop/runs.jsonl \
  --publish
```

Manual readiness replay from an explicit terminal summary:

```bash
python3 $SCRIPTS/mergeproof.py run \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --loop-summary /path/to/loop_summary.json \
  --json-output /tmp/readiness.json \
  --markdown-output /tmp/readiness.md \
  --html-output /tmp/readiness.html
```

If `mergeproof.yaml` is absent, readiness is skipped without failing the loop:

```text
[mergeproof] readiness skipped
Reason: mergeproof.yaml not found
```

For the default live flow, `mergeproof.yaml` must already exist on the trusted
base branch. If you are bootstrapping the first config PR, use `--trust-pr-config`
only for that explicit demo/review run; it is not the default trust model.

`--pr` accepts a PR URL (`https://github.com/OWNER/REPO/pull/123`) or the
`OWNER/REPO#123` shorthand. Changed files are fetched with `gh api`, so you need
`gh` authenticated — the same dependency the rest of GGRL already uses.

### The loop summary

`render_pr_readiness.py` reads a small JSON describing the AI loop's terminal
state. All fields are optional and degrade gracefully:

```json
{
  "pr_url": "https://github.com/OWNER/REPO/pull/123",
  "fixed_count": 7,
  "false_positives_skipped": 1,
  "verification": "passed",
  "verification_command": "uv run pytest",
  "rereview": "completed",
  "cycles_used": 2,
  "cycles_total": 3,
  "semantic_risk": false,
  "pending_confirmation": false
}
```

### Status logic

```
verification failed                 -> VERIFICATION_FAILED
MergeProof config changed in PR     -> CONFIG_CHANGED_REVIEW_REQUIRED
any production risk needs a human   -> HUMAN_DECISION_REQUIRED
semantic_risk flagged               -> HUMAN_DECISION_REQUIRED
fixes applied, not yet re-confirmed -> PENDING_CONFIRMATION
otherwise                           -> READY
```

---

## Offline fixture flow (demos and tests only)

When you do not have a live PR (a recorded demo, a screenshot, CI), use the
committed fixtures under [`fixtures/`](fixtures). Fixtures are a fallback — the
real PR flow above is the product.

These commands regenerate the checked-in report from fixture data; they do not
run the CR loop, fetch PR metadata, fetch changed files, read `mergeproof.yaml`,
or call GitHub. That is why they do **not** include `--pr`; the PR-shaped data is
already baked into the fixture files.

Fixture command mapping:

| Command | End-to-end step(s) | What it does |
|---|---:|---|
| `render_pr_readiness.py --json` | 20-22 | Combines fixture loop evidence, fixture context pack, and fixture risk overlay into readiness JSON. |
| `render_pr_readiness.py --markdown` | 20-22 | Renders the fixture PR Readiness Card markdown. |
| `render_demo_ui.py` | 24 | Renders the optional static HTML report. |

Set `GGRL_ROOT` to this repository so the commands work from any shell
directory:

```bash
GGRL_ROOT=/Users/orenashkenazy/dev/gh-gemini-review-loop
SCRIPTS=$GGRL_ROOT/plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
F=$GGRL_ROOT/demo/production-readiness/fixtures
OUT=$GGRL_ROOT/demo/production-readiness
mkdir -p "$OUT"

python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary $F/loop_summary.json \
  --architecture-context $F/production_context_pack.json \
  --production-risks $F/production_risks.json \
  --json > $OUT/readiness.json

python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary $F/loop_summary.json \
  --architecture-context $F/production_context_pack.json \
  --production-risks $F/production_risks.json \
  --markdown > $OUT/readiness.md

python3 $SCRIPTS/render_demo_ui.py \
  --readiness $OUT/readiness.json \
  --output $OUT/pr_readiness_report.html
```

Open `pr_readiness_report.html` in any browser — it is a single self-contained
file with embedded CSS, no JavaScript, and no external network assets.

---

## Artifacts in this directory

| File | What it is |
|---|---|
| `pr_readiness_report.html` | Polished static HTML report (open in a browser) |
| `readiness.md` | The GitHub Markdown readiness card |
| `readiness.json` | Canonical readiness data the HTML is rendered from |
| `fixtures/` | Inputs for the offline flow and tests |

---

## Scripts

| Script | Role |
|---|---|
| `mergeproof.py` | Command surface for `mergeproof run` after terminal loop output |
| `mergeproof_readiness.py` | Run the final readiness phase after a terminal loop summary |
| `build_context_pack.py` | Build the cross-repo Production Context Pack |
| `resolve_mergeproof.py` | Read trusted `mergeproof.yaml` / `.json` config from the base ref |
| `fetch_infra_files.py` | Fetch allowlisted infra files with size/count/binary safety limits |
| `mergeproof_config.py` | Parse and validate zero-dependency MergeProof config |
| `architecture_context.py` | Scan static repo files for architecture facts |
| `pr_architecture_risk.py` | Map a PR's changed files to production risks |
| `render_pr_readiness.py` | Render the readiness card (Markdown or JSON) |
| `publish_pr_readiness.py` | Post/update the single readiness PR comment |
| `render_demo_ui.py` | Render the static HTML report |

## Tests

```bash
/opt/homebrew/bin/pytest -q
```

Covers the scanner, risk mapper, card renderer, publisher (GitHub calls are
isolated behind an injectable client), and the HTML renderer.

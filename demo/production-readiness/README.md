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

## Real PR flow (primary)

This is the path teams use. It runs against a real repository and a real PR.

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts

# 1. Scan real static architecture files in the repo.
python3 $SCRIPTS/architecture_context.py --repo . --json > /tmp/architecture_context.json

# 2. Map the PR's real changed files to production risks.
python3 $SCRIPTS/pr_architecture_risk.py \
  --architecture-context /tmp/architecture_context.json \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --json > /tmp/production_risks.json

# 3. Render the readiness card (Markdown for GitHub, JSON for the UI).
python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary /path/to/loop_summary.json \
  --architecture-context /tmp/architecture_context.json \
  --production-risks /tmp/production_risks.json \
  --markdown > /tmp/readiness.md

# 4. Post/update a single readiness comment on the PR (only mutates when run).
python3 $SCRIPTS/publish_pr_readiness.py \
  --pr https://github.com/OWNER/REPO/pull/123 \
  --readiness /tmp/readiness.md

# 5. Render the polished static HTML report.
python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary /path/to/loop_summary.json \
  --architecture-context /tmp/architecture_context.json \
  --production-risks /tmp/production_risks.json \
  --json > /tmp/readiness.json
python3 $SCRIPTS/render_demo_ui.py \
  --readiness /tmp/readiness.json \
  --output demo/production-readiness/pr_readiness_report.html
```

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
any production risk needs a human   -> HUMAN_DECISION_REQUIRED
semantic_risk flagged               -> HUMAN_DECISION_REQUIRED
fixes applied, not yet re-confirmed -> PENDING_CONFIRMATION
otherwise                           -> READY
```

---

## Offline / fixture flow (demos and tests only)

When you do not have a live PR (a recorded demo, a screenshot, CI), use the
local changed-file list instead of `--pr`, and the committed fixtures under
[`fixtures/`](fixtures). Fixtures are a fallback — the real PR flow above is the
product.

```bash
SCRIPTS=plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts
F=demo/production-readiness/fixtures

python3 $SCRIPTS/pr_architecture_risk.py \
  --architecture-context $F/architecture_context.json \
  --changed-files $F/changed_files.txt \
  --json > $F/production_risks.json

python3 $SCRIPTS/render_pr_readiness.py \
  --loop-summary $F/loop_summary.json \
  --architecture-context $F/architecture_context.json \
  --production-risks $F/production_risks.json \
  --json > demo/production-readiness/readiness.json

python3 $SCRIPTS/render_demo_ui.py \
  --readiness demo/production-readiness/readiness.json \
  --output demo/production-readiness/pr_readiness_report.html
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

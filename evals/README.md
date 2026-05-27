# evals/ — Layer B finding-quality eval

LLM-as-judge that rates Gemini Code Assist findings as `useful` / `false-positive` / `borderline` / `dup`, then compares the judge against hand-labeled fixtures to compute agreement. The goal is to answer the practical question: **is running this loop on a given repo actually worth it, or is Gemini's signal-to-noise too low here?**

## Why a cross-vendor judge

We use OpenAI's `gpt-4o-mini` (override via `OPENAI_JUDGE_MODEL`) to judge Gemini's output. Using the same vendor for both reviewer and judge tends to produce overly lenient evals. Cross-vendor reduces that bias.

## Directory layout

```
evals/
├── __init__.py
├── README.md                # this file
├── judge.py                 # JudgeClient — wraps OpenAI chat completions
├── run_eval.py              # CLI runner — loads fixtures, calls judge, prints metrics
├── test_eval.py             # pytest — runner logic with mocked judge, no API calls
└── fixtures/
    ├── pr-<N>.json          # Gemini findings extracted from PR #N
    └── pr-<N>.label.json    # human ground-truth labels per finding
```

## Fixture format

Each fixture is two files: the raw findings and the human labels keyed by `comment_id` (the GitHub GraphQL node id, e.g. `PRRC_kwDOShyq-M7E9664`).

```jsonc
// pr-6.json
{
  "pr": 6,
  "findings": [
    {
      "thread_id": "...",
      "comment_id": "PRRC_kwDOShyq-M7E9664",
      "path": "plugins/.../scripts/fetch_gemini_threads.py",
      "line": 352,
      "severity": "medium",      // sniffed from the ![severity](...) image marker
      "is_resolved": true,
      "is_outdated": false,
      "body": "The filter_by_min_severity function...",
      "diff_hunk": "@@ ...",
      "url": "https://github.com/..."
    }
  ]
}

// pr-6.label.json — co-located, same stem
{
  "pr": 6,
  "human_labels": [
    {
      "comment_id": "PRRC_kwDOShyq-M7E9664",
      "label": "useful",         // useful | false-positive | borderline | dup
      "reason": "Real feature gap..."
    }
  ]
}
```

## Adding a new fixture

When you've processed another PR with the gh-gemini-review-loop skill, capture its findings:

```bash
N=42  # PR number
SCRIPT=$(find ~/.claude/plugins/cache -name fetch_gemini_threads.py | head -1)
python3 "$SCRIPT" \
    --pr "https://github.com/OrenAshkenazy/gh-gemini-review-loop/pull/$N" \
    --format json \
    --no-resolve-outdated --no-resolve-addressed-by-reply \
    --include-resolved --include-outdated --include-addressed-by-reply \
    2>/dev/null \
  | python3 evals/extract_findings.py > evals/fixtures/pr-$N.json
```

Then hand-label by editing `evals/fixtures/pr-$N.label.json`. Label each finding from your lived experience with the PR. Aim for 5-10 minutes of effort per PR.

## Running locally

```bash
# Self-test with the fake judge — no API calls, sanity check the runner
python3 -m evals.run_eval --judge fake

# Real eval, 1 sample per finding
OPENAI_API_KEY=sk-... python3 -m evals.run_eval

# Variance check — 3 samples per finding with non-zero temperature.
# At --temperature 0.0 (default), every sample returns the same label, so
# variance is always 0. Bump temperature to get a real ambiguity signal.
OPENAI_API_KEY=sk-... python3 -m evals.run_eval --samples 3 --temperature 0.5

# Restrict to one fixture
python3 -m evals.run_eval --fixture pr-8

# Write a JSON report for downstream tooling
python3 -m evals.run_eval --report /tmp/eval-report.json

# CI gate: exit non-zero if agreement drops below 80%
python3 -m evals.run_eval --exit-nonzero-on-disagreement
```

Pytest:

```bash
pytest evals/                  # runs the hermetic tests (no API calls)
```

## Cost

`gpt-4o-mini` at current OpenAI pricing: roughly **$0.02 per full eval run** (11 findings × 1 sample × ~$0.002 input/output combined). Variance runs at `--samples 5` are still under $0.10. Weekly CI for a year ≈ $1. Negligible.

To use a stronger judge (when calibration is poor), set `OPENAI_JUDGE_MODEL=gpt-4o` — about 20× the cost, but better at borderline cases.

## Weekly CI

`.github/workflows/eval-weekly.yml` runs the eval every Sunday and posts the summary as a GitHub Issue. Needs `OPENAI_API_KEY` set as a repo secret. Drift in Gemini's output format (or in `gpt-4o-mini`'s behavior) shows up as agreement-rate regressions.

## Calibration: when to trust the judge

The judge is calibrated for this repo's fixtures **once judge↔human agreement is ≥85%**. Until then:

1. Run the eval, look at the **Disagreements** section of the summary.
2. For each disagreement, decide: was the human label wrong, or was the judge wrong?
3. If the human label was wrong → fix the label file. If the judge was wrong → refine the system prompt in `judge.py` and re-run.

A few useful sanity checks if the judge looks miscalibrated:

- Are the fixtures truly representative, or biased toward "useful" labels? (If you've only labeled obviously-good findings, the judge can hit 100% by saying "useful" everywhere.)
- Is the diff hunk getting included in the prompt? `build_user_prompt` skips empty hunks; missing context kills the judge's accuracy.
- Is `temperature=0.0` set? Variance across samples should be near-zero if so; if not, the SDK call is wrong.

# Layer B finding-quality eval — pending first run

The weekly eval workflow has not yet run on this repo. Once it does, this file will be overwritten with the rendered report and the README's "Skill calibration" section will start pulling real numbers from here.

To trigger the first run:

1. Set the `OPENAI_API_KEY` repository secret in Settings → Secrets and variables → Actions.
2. Optionally set the `OPENAI_JUDGE_MODEL` repository variable (defaults to `gpt-4o-mini`; use `gpt-4o` for ~20× cost and tighter calibration).
3. Run **Weekly finding-quality eval** manually via the Actions tab, or wait for the next Sunday 00:00 UTC cron tick.

## What this file will contain after the first run

- Judge ↔ human agreement rate (overall + per-severity).
- Confusion matrix (human label rows × judge label columns).
- The disagreement list — each finding the judge labeled differently from the human, with the judge's stated reason.
- Raw JSON report for downstream tooling.

## Why we publish it here

The eval is the maintainer's tool for calibrating the skill. Publishing the rendered output to the repo (instead of only into a private tracking Issue) makes the trust signal visible to anyone evaluating whether to install the plugin. The README's "Skill calibration" section links to this file.

## Hand-labeled ground truth

The corpus today is 11 findings across 4 PRs of this repo (PRs #6, #7, #8, #9): **10 useful + 1 false positive** (~91% useful by hand-label). The judge's job is to come within 80% agreement of that ground truth; below that, the run fails the calibration gate and we know either the judge or the human labels need a look.

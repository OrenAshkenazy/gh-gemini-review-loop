---
name: gh-gemini-review-loop
description: Use when Claude creates or opens a PR, after a PR is created, or when the user asks to handle gemini-code-assist PR comments, fix Gemini review feedback, wait for Gemini, run a Gemini review loop, commit fixes to the remote branch, or request Gemini re-review on the current branch PR or a specified PR.
---

# Gemini Code Assist PR Review Loop

## Overview

Use this skill to run the full GitHub PR loop: after PR creation, wait for `gemini-code-assist` to finish reviewing, fetch unresolved actionable review threads, acknowledge the requested fixes, implement clear fixes, verify them, commit and push to the PR branch, and ask Gemini Code Assist to re-review the latest revision.

Prefer thread-aware review data over flat PR comments. GitHub review threads preserve `isResolved`, `isOutdated`, file paths, line anchors, and diff hunks, which are necessary for reliable automation.

## Stopping Conditions

Stop the loop and report status instead of pushing or asking Gemini again when any condition is true:

- Gemini has already been asked to re-review the PR 3 times.
- There are no unresolved, non-outdated, actionable Gemini threads after stale-thread cleanup.
- The remaining Gemini threads are informational, duplicate, contradictory, or require a human product/design/security decision.
- Tests fail after a fix attempt and the failure is not clearly caused by the latest Gemini-addressing change.
- The same actionable thread fingerprint appears after a fix attempt, indicating no progress.

Do not run more than 3 fix/re-review cycles per PR. If the loop stops because the cap is reached, summarize the latest unresolved actionable comments and leave the PR for a human decision.

## Workflow

1. Trigger the loop by default after PR creation.
   - When Claude creates or opens a PR and this skill is available, continue into this workflow automatically unless the user explicitly says not to.
   - Treat "create the PR", "open a PR", "yeet this", "ship this PR", and "run the Gemini loop" as permission to complete the full loop: wait, fetch, acknowledge, fix, verify, commit, push, and request Gemini re-review.

2. Resolve the PR.
   - If the user provides a PR URL, repo, or PR number, use it directly.
   - Otherwise, use the current git repository and branch:
     - `gh auth status`
     - `gh pr view --json number,url,headRefName,baseRefName`
   - If no PR exists for the branch, report that blocker.

3. Wait for Gemini to finish its first review.
   - Run `scripts/fetch_gemini_threads.py --wait` from this skill.
   - The script polls GitHub until Gemini review activity is present and stable for a quiet period.
   - By default, the script resolves unresolved outdated Gemini threads after the wait and before returning current feedback.
   - If the wait times out, report that Gemini did not finish within the timeout and do not invent feedback.

4. Check loop status and clean stale threads.
   - Count prior PR comments that ask `@gemini-code-assist` to review again.
   - If the count is already 3 or more, stop before making changes, pushing, posting comments, or resolving threads.
   - If the count is below 3, unresolved outdated Gemini review threads are resolved automatically; outdated threads are stale and should not drive new fixes.
   - For read-only inspection, pass `--no-resolve-outdated`.

5. Fetch Gemini review threads.
   - Default author filter: `gemini-code-assist`.
   - Default thread filter: unresolved and not outdated.
   - Use JSON output when another tool or script will consume the result; use Markdown for human triage.

6. Acknowledge what needs to be fixed.
   - Before editing, briefly summarize the actionable Gemini findings grouped by file or behavior.
   - If there are no actionable unresolved threads, say so and stop after reporting the clean result.

7. Classify comments.
   - Group by file and behavioral area.
   - Treat clear requested changes as actionable.
   - Ignore already resolved threads, outdated threads, approvals, duplicates, and informational comments.
   - If a thread asks for explanation rather than a code change, draft a response instead of forcing a code edit.
   - If comments conflict or could cause a behavioral regression, stop and surface the tradeoff.

8. Implement fixes.
   - Keep changes scoped to the Gemini feedback.
   - Read the relevant code before editing.
   - Preserve unrelated local changes.
   - Make each change traceable to a feedback cluster.

9. Verify.
   - Run the narrowest meaningful checks first.
   - Broaden tests when shared logic or user-facing behavior changes.
   - If checks cannot run, report why and what remains unverified.

10. Commit, push, and request re-review.
    - For this skill's full loop, commit fixes to the PR branch and push to the remote branch.
    - Use a clear commit message such as `fix: address Gemini Code Assist review`.
    - Post the re-review request after a successful push only if this would not exceed 3 total re-review requests.
    - Default comment:
      - `@gemini-code-assist please review the latest changes.`
    - If the repository uses a different Gemini trigger phrase, use the repo-specific phrase when known.

## Script Usage

From any repository with a GitHub PR:

```bash
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py
```

By default this resolves unresolved outdated Gemini threads before printing current feedback, unless the PR has already reached the 3 re-review request cap.

Useful options:

```bash
# Wait for Gemini review activity to appear and settle
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --wait

# Read-only fetch without resolving outdated Gemini threads
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --no-resolve-outdated

# Specific PR URL
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --pr https://github.com/OWNER/REPO/pull/123

# JSON for automation
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --format json

# Include outdated or resolved threads while investigating history
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --no-resolve-outdated --include-outdated --include-resolved

# Use a different bot login
python3 ~/.claude/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py --author google-gemini-code-assist
```

## GitHub Write Safety

This skill's default full loop includes committing, pushing, asking Gemini for re-review, and resolving outdated Gemini threads after PR creation or when the user asks for the Gemini loop. Do not resolve non-outdated review threads or submit GitHub reviews unless explicitly asked. Stop before publishing if the fixes are ambiguous, tests expose a regression, local unrelated changes make it unsafe to commit cleanly, or the PR has already reached the 3 re-review request cap.

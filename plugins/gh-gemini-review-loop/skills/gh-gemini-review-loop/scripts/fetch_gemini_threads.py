#!/usr/bin/env python3
"""Fetch thread-aware Gemini Code Assist review comments for a GitHub PR."""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_AUTHOR = "gemini-code-assist"
DEFAULT_REREVIEW_LIMIT = 3
REREVIEW_TRIGGER_RE = re.compile(r"@gemini-code-assist\b.*\breview\b", re.IGNORECASE | re.DOTALL)

# Gemini prefixes inline review comments with a priority image whose alt text
# is the severity. Example: ![high](https://www.gstatic.com/codereviewagent/high-priority.svg)
SEVERITY_RE = re.compile(r"!\[(critical|high|medium|low)\]", re.IGNORECASE)
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

# Page sizes embedded in QUERY below. Used by the pagination guard to detect
# when we may be silently dropping data.
PAGE_LIMIT_REVIEW_THREADS = 100
PAGE_LIMIT_REVIEWS = 100
PAGE_LIMIT_PR_COMMENTS = 100
PAGE_LIMIT_THREAD_COMMENTS = 50

# Minimum reply length to count a non-bot comment as a substantive reply.
# Filters out token acks like "ok", "ack", "thanks", "👍".
ADDRESSED_BY_REPLY_MIN_CHARS = 30


@dataclass(frozen=True)
class PullRequest:
    owner: str
    repo: str
    number: int
    url: str | None = None


def run_gh(args: list[str], cwd: str | None = None) -> Any:
    proc = subprocess.run(
        ["gh", *args],
        cwd=cwd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh {' '.join(args)} failed: {message}")
    output = proc.stdout.strip()
    if not output:
        return None
    try:
        return json.loads(output)
    except json.JSONDecodeError:
        return output


def parse_pr_url(value: str) -> PullRequest | None:
    match = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$", value)
    if not match:
        return None
    owner, repo, number = match.groups()
    return PullRequest(owner=owner, repo=repo, number=int(number), url=value)


def resolve_current_pr() -> PullRequest:
    run_gh(["auth", "status"])
    view = run_gh(["pr", "view", "--json", "number,url"])
    if not isinstance(view, dict) or "url" not in view or "number" not in view:
        raise RuntimeError("Could not resolve the current branch PR with gh pr view.")

    parsed = parse_pr_url(view["url"])
    if not parsed:
        raise RuntimeError(f"Could not parse PR URL: {view['url']}")
    return PullRequest(parsed.owner, parsed.repo, int(view["number"]), view["url"])


def resolve_pr(value: str | None) -> PullRequest:
    if not value:
        return resolve_current_pr()

    parsed = parse_pr_url(value)
    if parsed:
        return parsed

    shorthand = re.match(r"([^/\s]+)/([^#\s]+)#(\d+)$", value)
    if shorthand:
        owner, repo, number = shorthand.groups()
        return PullRequest(owner=owner, repo=repo, number=int(number))

    raise RuntimeError("Use a PR URL like https://github.com/OWNER/REPO/pull/123 or OWNER/REPO#123.")


QUERY = """
query($owner:String!, $repo:String!, $number:Int!) {
  repository(owner:$owner, name:$repo) {
    pullRequest(number:$number) {
      number
      url
      title
      comments(last:100) {
        nodes {
          id
          author { login }
          body
          createdAt
          url
        }
      }
      reviews(last:100) {
        nodes {
          id
          author { login }
          body
          state
          submittedAt
          url
        }
      }
      reviewThreads(first:100) {
        nodes {
          id
          isResolved
          isOutdated
          path
          line
          originalLine
          diffSide
          comments(first:50) {
            nodes {
              id
              author { login }
              body
              createdAt
              updatedAt
              url
              path
              line
              originalLine
              diffHunk
            }
          }
        }
      }
    }
  }
}
"""


RESOLVE_THREAD_MUTATION = """
mutation($threadId:ID!) {
  resolveReviewThread(input:{threadId:$threadId}) {
    thread {
      id
      isResolved
    }
  }
}
"""


def fetch_threads(pr: PullRequest) -> dict[str, Any]:
    result = run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={QUERY}",
            "-F",
            f"owner={pr.owner}",
            "-F",
            f"repo={pr.repo}",
            "-F",
            f"number={pr.number}",
        ]
    )
    if not isinstance(result, dict):
        raise RuntimeError("Unexpected gh GraphQL response.")
    try:
        return result["data"]["repository"]["pullRequest"]
    except KeyError as exc:
        raise RuntimeError(f"Unexpected gh GraphQL shape: missing {exc}") from exc


def resolve_thread(thread_id: str, *, dry_run: bool = False, label: str = "thread") -> None:
    if dry_run:
        print(f"[dry-run] would resolve {label} {thread_id}", file=sys.stderr)
        return
    run_gh(
        [
            "api",
            "graphql",
            "-f",
            f"query={RESOLVE_THREAD_MUTATION}",
            "-F",
            f"threadId={thread_id}",
        ]
    )


def is_addressed_by_reply(thread: dict[str, Any], bot_author: str) -> bool:
    """An unresolved thread where a non-bot human posted a substantive reply.

    The reply is treated as a deliberate deferral (defer/wontfix/explanation).
    The loop should not retry fixing the same thread cycle after cycle.
    """
    if thread.get("isResolved") or thread.get("isOutdated"):
        return False
    for comment in (thread.get("comments") or {}).get("nodes") or []:
        login = (comment.get("author") or {}).get("login") or ""
        if not login or login == bot_author or login.endswith("[bot]"):
            continue
        body = (comment.get("body") or "").strip()
        if len(body) >= ADDRESSED_BY_REPLY_MIN_CHARS:
            return True
    return False


def filter_threads(
    pull_request: dict[str, Any],
    author: str,
    include_resolved: bool,
    include_outdated: bool,
    include_addressed_by_reply: bool = False,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if thread.get("isResolved") and not include_resolved:
            continue
        if thread.get("isOutdated") and not include_outdated:
            continue
        if not include_addressed_by_reply and is_addressed_by_reply(thread, author):
            continue

        comments = thread.get("comments", {}).get("nodes", [])
        matching_comments = [
            comment
            for comment in comments
            if (comment.get("author") or {}).get("login") == author
        ]
        if not matching_comments:
            continue

        copied = dict(thread)
        copied["comments"] = matching_comments
        selected.append(copied)
    return selected


def outdated_unresolved_threads(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if thread.get("isResolved") or not thread.get("isOutdated"):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if any((comment.get("author") or {}).get("login") == author for comment in comments):
            threads.append(thread)
    return threads


def addressed_by_reply_threads(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for thread in pull_request.get("reviewThreads", {}).get("nodes", []):
        if not is_addressed_by_reply(thread, author):
            continue
        comments = thread.get("comments", {}).get("nodes", [])
        if any((comment.get("author") or {}).get("login") == author for comment in comments):
            threads.append(thread)
    return threads


def resolve_outdated_threads(
    pull_request: dict[str, Any], author: str, *, dry_run: bool = False
) -> int:
    threads = outdated_unresolved_threads(pull_request, author)
    for thread in threads:
        resolve_thread(thread["id"], dry_run=dry_run, label="outdated")
    return len(threads)


def resolve_addressed_by_reply(
    pull_request: dict[str, Any], author: str, *, dry_run: bool = False
) -> int:
    threads = addressed_by_reply_threads(pull_request, author)
    for thread in threads:
        resolve_thread(thread["id"], dry_run=dry_run, label="addressed-by-reply")
    return len(threads)


def _iter_comments(thread: dict[str, Any]) -> list[dict[str, Any]]:
    """Return thread comments regardless of pre/post-filter shape."""
    comments = thread.get("comments")
    if isinstance(comments, list):
        return comments
    if isinstance(comments, dict):
        return comments.get("nodes", []) or []
    return []


def thread_severity(thread: dict[str, Any]) -> str:
    """Return Gemini-assigned severity (critical/high/medium/low) or 'unknown'."""
    for comment in _iter_comments(thread):
        body = comment.get("body") or ""
        match = SEVERITY_RE.search(body)
        if match:
            return match.group(1).lower()
    return "unknown"


def sort_by_severity(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort threads by severity, highest first; stable for equal severities."""
    return sorted(
        threads,
        key=lambda t: SEVERITY_ORDER.get(thread_severity(t), SEVERITY_ORDER["unknown"]),
    )


def filter_by_min_severity(
    threads: list[dict[str, Any]], min_severity: str | None, *, keep_unknown: bool = True
) -> list[dict[str, Any]]:
    """Drop threads with severity strictly lower than min_severity.

    If `min_severity` is None, no minimum-severity threshold is enforced — the
    function then acts only on the `keep_unknown` axis, which lets callers use
    `--drop-unknown-severity` independently of `--min-severity`.

    `unknown` threads (no Gemini priority marker) are kept by default — dropping
    them on a strict filter would silently swallow every thread Gemini didn't
    annotate, which is the wrong default for a fast-feedback loop. Pass
    keep_unknown=False to drop them anyway.
    """
    cap = SEVERITY_ORDER[min_severity] if min_severity else None
    out: list[dict[str, Any]] = []
    for t in threads:
        sev = thread_severity(t)
        if sev == "unknown":
            if keep_unknown:
                out.append(t)
            continue
        if cap is None or SEVERITY_ORDER[sev] <= cap:
            out.append(t)
    return out


def severity_counts(threads: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for thread in threads:
        sev = thread_severity(thread)
        counts[sev] = counts.get(sev, 0) + 1
    return counts


def pagination_warnings(pull_request: dict[str, Any]) -> list[str]:
    """Return human-readable warnings when any GraphQL page hit its limit.

    Hitting the limit means the script may be silently dropping data — the
    loop should surface this so the user knows to paginate or scope the PR.
    """
    warnings: list[str] = []
    pr_comments = (pull_request.get("comments") or {}).get("nodes") or []
    reviews = (pull_request.get("reviews") or {}).get("nodes") or []
    threads = (pull_request.get("reviewThreads") or {}).get("nodes") or []
    if len(pr_comments) >= PAGE_LIMIT_PR_COMMENTS:
        warnings.append(
            f"PR comments hit page limit ({PAGE_LIMIT_PR_COMMENTS}); older comments may be missing."
        )
    if len(reviews) >= PAGE_LIMIT_REVIEWS:
        warnings.append(
            f"PR reviews hit page limit ({PAGE_LIMIT_REVIEWS}); older reviews may be missing."
        )
    if len(threads) >= PAGE_LIMIT_REVIEW_THREADS:
        warnings.append(
            f"reviewThreads hit page limit ({PAGE_LIMIT_REVIEW_THREADS}); newer threads may be missing."
        )
    for thread in threads:
        comments = (thread.get("comments") or {}).get("nodes") or []
        if len(comments) >= PAGE_LIMIT_THREAD_COMMENTS:
            thread_id = thread.get("id") or "(unknown)"
            warnings.append(
                f"thread {thread_id} comments hit page limit "
                f"({PAGE_LIMIT_THREAD_COMMENTS}); newer replies may be missing."
            )
    return warnings


def filter_reviews(pull_request: dict[str, Any], author: str) -> list[dict[str, Any]]:
    return [
        review
        for review in pull_request.get("reviews", {}).get("nodes", [])
        if (review.get("author") or {}).get("login") == author
    ]


def rereview_requests(
    pull_request: dict[str, Any], agent_login: str | None = None
) -> list[dict[str, Any]]:
    """Count comments that ping Gemini for a re-review.

    When agent_login is provided, only comments authored by that login count.
    This prevents arbitrary humans pinging Gemini from consuming the loop cap.
    """
    out = []
    for comment in pull_request.get("comments", {}).get("nodes", []):
        if not REREVIEW_TRIGGER_RE.search(comment.get("body") or ""):
            continue
        if agent_login is not None:
            login = (comment.get("author") or {}).get("login") or ""
            if login != agent_login:
                continue
        out.append(comment)
    return out


def gh_authenticated_login() -> str | None:
    """Return the gh-authenticated user login, or None if it can't be resolved."""
    try:
        result = run_gh(["api", "user", "--jq", ".login"])
    except RuntimeError:
        return None
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


def post_pr_comment(pr: PullRequest, body: str, *, dry_run: bool = False) -> None:
    """Post a comment on the PR via gh."""
    pr_ref = f"{pr.owner}/{pr.repo}#{pr.number}"
    if dry_run:
        print(f"[dry-run] would post receipt to {pr_ref}:\n{body}", file=sys.stderr)
        return
    proc = subprocess.run(
        ["gh", "pr", "comment", str(pr.number), "--repo", f"{pr.owner}/{pr.repo}", "--body", body],
        check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"gh pr comment failed: {message}")


# ---------------------------------------------------------------------------
# Sticky receipt: a single PR comment that gets edited in place across
# multiple loop invocations on the same PR. Provides background visibility
# in the GitHub UI without spamming new comments per cycle.
# ---------------------------------------------------------------------------

# Marker embedded in the rendered body so a sticky receipt can be identified
# even if the local state file is wiped. Used as a fallback discovery key.
STICKY_RECEIPT_MARKER = "<!-- gh-gemini-review-loop:sticky-receipt -->"


def sticky_state_path() -> Path:
    """Return the path to the sticky-receipt state file.

    Overridable via ``GGRL_STATE_DIR`` (useful for tests). Defaults to
    ``~/.config/gh-gemini-review-loop/state.json`` per XDG conventions.
    """
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "state.json"


def _state_key(pr: PullRequest) -> str:
    return f"{pr.owner}/{pr.repo}#{pr.number}"


def load_sticky_state() -> dict[str, Any]:
    path = sticky_state_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_sticky_state(state: dict[str, Any]) -> None:
    path = sticky_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def find_existing_sticky_comment(pr: PullRequest) -> int | None:
    """Look up the sticky receipt for a PR by scanning issue comments for the marker.

    Used as a recovery path when the local state file is missing — the marker
    embedded in the receipt body is the source of truth on GitHub.
    """
    # `gh api --paginate` runs --jq on each page independently, so a per-page
    # aggregator like `[...] | last` returns the last id PER PAGE — not the
    # global last. Emit one id per line across all pages and pick the last
    # valid one in Python.
    result = run_gh(
        [
            "api",
            f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
            "--paginate",
            "--jq",
            f'.[] | select(.body | contains("{STICKY_RECEIPT_MARKER}")) | .id',
        ]
    )
    if isinstance(result, int):
        return result
    if isinstance(result, str):
        ids = [int(line.strip()) for line in result.splitlines() if line.strip().isdigit()]
        if ids:
            return ids[-1]
    return None


def post_or_update_sticky_receipt(
    pr: PullRequest, body: str, *, dry_run: bool = False
) -> int | None:
    """Post or in-place edit the sticky receipt comment for ``pr``.

    First invocation per PR posts a fresh comment and records its id in the
    state file. Subsequent invocations PATCH the same comment so the user
    sees one live status block on the PR instead of N new comments.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    comment_id = entry.get("comment_id")

    if comment_id is None:
        # Fall back to GitHub-side discovery before posting a duplicate.
        comment_id = find_existing_sticky_comment(pr)
        if comment_id and not dry_run:
            entry["comment_id"] = comment_id

    if dry_run:
        verb = "PATCH" if comment_id else "POST"
        print(f"[dry-run] would {verb} sticky receipt on {key}", file=sys.stderr)
        return comment_id

    if comment_id:
        try:
            run_gh(
                [
                    "api",
                    "-X",
                    "PATCH",
                    f"repos/{pr.owner}/{pr.repo}/issues/comments/{comment_id}",
                    "-f",
                    f"body={body}",
                ]
            )
            entry["updated_at"] = _now_iso()
        except RuntimeError as exc:
            # If the sticky comment was deleted on GitHub, PATCH returns 404.
            # Drop the stale id and fall through to POST a fresh comment so
            # the loop recovers without crashing.
            msg = str(exc)
            if "404" in msg or "Not Found" in msg:
                print(
                    f"warning: sticky receipt {comment_id} no longer exists on GitHub "
                    "(deleted?). Posting a new comment.",
                    file=sys.stderr,
                )
                comment_id = None
            else:
                raise

    if not comment_id:
        result = run_gh(
            [
                "api",
                "-X",
                "POST",
                f"repos/{pr.owner}/{pr.repo}/issues/{pr.number}/comments",
                "-f",
                f"body={body}",
            ]
        )
        if not isinstance(result, dict) or "id" not in result:
            raise RuntimeError(f"Failed to post sticky receipt: {result}")
        comment_id = int(result["id"])
        entry["comment_id"] = comment_id
        entry["started_at"] = _now_iso()
        entry["updated_at"] = entry["started_at"]

    state[key] = entry
    save_sticky_state(state)
    return comment_id


def render_receipt(
    pr: PullRequest,
    pull_request: dict[str, Any],
    threads: list[dict[str, Any]],
    *,
    author: str,
    resolved_outdated: int,
    resolved_addressed_by_reply: int,
    rereview_count: int,
    rereview_limit: int,
    status: str | None = None,
    sticky: bool = False,
) -> str:
    """Render the markdown receipt body.

    ``status`` (RUNNING / DONE / STOPPED) appears in the header when set.
    ``sticky`` embeds the discovery marker and a "last updated" timestamp.
    """
    counts = severity_counts(threads)
    deferred = len(addressed_by_reply_threads(pull_request, author))
    sev_line = ", ".join(
        f"{k}={counts[k]}" for k in ("critical", "high", "medium", "low", "unknown") if counts.get(k)
    ) or "none"
    header_suffix = f" — {status}" if status else ""
    parts = [
        f"### gh-gemini-review-loop receipt{header_suffix}",
        "",
        "| metric | value |",
        "|---|---|",
        f"| re-review cycles used | {rereview_count} / {rereview_limit} |",
        f"| outdated threads resolved | {resolved_outdated} |",
        f"| addressed-by-reply threads resolved | {resolved_addressed_by_reply} |",
        f"| addressed-by-reply still pending | {deferred} |",
        f"| actionable threads remaining | {len(threads)} |",
        f"| severity breakdown (actionable) | {sev_line} |",
        "",
    ]
    if sticky:
        parts.append(f"_Last updated: {_now_iso()}. Receipt edits in place as the loop progresses._")
        parts.append("")
        parts.append(STICKY_RECEIPT_MARKER)
    else:
        parts.append("_Generated by `scripts/fetch_gemini_threads.py --post-receipt`._")
    return "\n".join(parts) + "\n"


def thread_fingerprint(threads: list[dict[str, Any]]) -> str:
    payload = []
    for thread in threads:
        comments = thread.get("comments", [])
        payload.append(
            {
                "path": thread.get("path") or first_value(thread, "path"),
                "line": thread.get("line") or thread.get("originalLine") or first_value(thread, "line"),
                "comments": [
                    {
                        "author": (comment.get("author") or {}).get("login"),
                        "body": comment.get("body"),
                    }
                    for comment in comments
                ],
            }
        )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def review_activity_fingerprint(
    pull_request: dict[str, Any],
    author: str,
) -> str | None:
    reviews = filter_reviews(pull_request, author)
    authored_threads = filter_threads(
        pull_request,
        author=author,
        include_resolved=True,
        include_outdated=True,
    )
    if not reviews and not authored_threads:
        return None

    payload = {"reviews": reviews, "threads": authored_threads}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def wait_for_stable_review(
    pr: PullRequest,
    author: str,
    timeout_seconds: int,
    interval_seconds: int,
    quiet_seconds: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_fingerprint: str | None = None
    stable_since: float | None = None

    while True:
        pull_request = fetch_threads(pr)
        fingerprint = review_activity_fingerprint(pull_request, author)
        now = time.monotonic()

        if fingerprint is None:
            print(f"Waiting for {author} review activity...", file=sys.stderr)
        elif fingerprint != last_fingerprint:
            print(f"Detected {author} review activity; waiting for it to settle...", file=sys.stderr)
            last_fingerprint = fingerprint
            stable_since = now
        elif stable_since is not None and now - stable_since >= quiet_seconds:
            print(f"{author} review activity is stable.", file=sys.stderr)
            return pull_request

        if now >= deadline:
            raise RuntimeError(
                f"Timed out waiting for stable {author} review activity after {timeout_seconds} seconds."
            )

        time.sleep(min(interval_seconds, max(0.0, deadline - now)))


def render_markdown(pr: dict[str, Any], threads: list[dict[str, Any]], author: str) -> str:
    reviews = filter_reviews(pr, author)
    rereviews = rereview_requests(pr)
    outdated = outdated_unresolved_threads(pr, author)
    deferred = addressed_by_reply_threads(pr, author)
    lines = [
        f"# Gemini Code Assist Threads for PR #{pr.get('number')}",
        "",
        f"PR: {pr.get('url')}",
        f"Author filter: `{author}`",
        f"Reviews by author: {len(reviews)}",
        f"Re-review requests: {len(rereviews)}",
        f"Unresolved outdated threads: {len(outdated)}",
        f"Addressed-by-reply threads: {len(deferred)}",
        f"Threads: {len(threads)}",
        "",
    ]
    if reviews:
        lines.append("## Reviews")
        for review in reviews:
            state = review.get("state") or "UNKNOWN"
            submitted = review.get("submittedAt") or ""
            url = review.get("url") or ""
            lines.append(f"- {state} {submitted} {url}".strip())
        lines.append("")
    for index, thread in enumerate(threads, start=1):
        path = thread.get("path") or first_value(thread, "path") or "(unknown path)"
        line = thread.get("line") or first_value(thread, "line") or thread.get("originalLine") or ""
        status = []
        severity = thread_severity(thread)
        if severity != "unknown":
            status.append(severity)
        if thread.get("isResolved"):
            status.append("resolved")
        if thread.get("isOutdated"):
            status.append("outdated")
        status_text = f" [{' '.join(status)}]" if status else ""
        lines.append(f"## {index}. {path}:{line}{status_text}")
        for comment in thread["comments"]:
            body = (comment.get("body") or "").strip()
            url = comment.get("url") or ""
            created = comment.get("createdAt") or ""
            lines.extend(["", f"- {created} {url}".strip(), "", body, ""])
            hunk = comment.get("diffHunk")
            if hunk:
                lines.extend(["```diff", hunk.rstrip(), "```", ""])
    return "\n".join(lines).rstrip() + "\n"


def first_value(thread: dict[str, Any], key: str) -> Any:
    for comment in thread.get("comments", []):
        value = comment.get(key)
        if value is not None:
            return value
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pr", help="PR URL or OWNER/REPO#NUMBER. Defaults to current branch PR.")
    parser.add_argument("--author", default=DEFAULT_AUTHOR, help=f"Review author login. Default: {DEFAULT_AUTHOR}")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--include-resolved", action="store_true")
    parser.add_argument("--include-outdated", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Poll until Gemini review activity appears and is stable.")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait with --wait. Default: 900.")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds with --wait. Default: 20.")
    parser.add_argument("--quiet-period", type=int, default=45, help="Stable activity period in seconds with --wait. Default: 45.")
    parser.add_argument(
        "--resolve-outdated",
        dest="resolve_outdated",
        action="store_true",
        default=True,
        help="Resolve unresolved outdated Gemini review threads before printing current feedback. Enabled by default.",
    )
    parser.add_argument(
        "--no-resolve-outdated",
        dest="resolve_outdated",
        action="store_false",
        help="Do not resolve outdated Gemini review threads; use for read-only inspection.",
    )
    parser.add_argument(
        "--max-rereview-requests",
        type=int,
        default=DEFAULT_REREVIEW_LIMIT,
        help=f"Warn when prior Gemini re-review requests reach this limit. Default: {DEFAULT_REREVIEW_LIMIT}.",
    )
    parser.add_argument(
        "--resolve-past-cap",
        dest="ignore_loop_limit",
        action="store_true",
        help="Allow --resolve-outdated / --resolve-addressed-by-reply even after the re-review cap is reached.",
    )
    # Deprecated alias for --resolve-past-cap. Kept for backward compat; hidden from --help.
    parser.add_argument(
        "--ignore-loop-limit",
        dest="ignore_loop_limit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--agent-login",
        default=None,
        help=(
            "GitHub login of the agent posting re-review requests. If omitted, the script "
            "auto-detects via `gh api user`. Used to count only the agent's own re-reviews "
            "toward the cap (humans pinging Gemini do not consume cycles)."
        ),
    )
    parser.add_argument(
        "--no-agent-filter",
        action="store_true",
        help="Disable agent-login filtering; count ANY '@gemini-code-assist ... review' comment.",
    )
    parser.add_argument(
        "--post-receipt",
        action="store_true",
        help=(
            "Post a summary 'loop receipt' comment to the PR after fetch/filter. "
            "Includes cycles used, threads resolved, severity breakdown, and remaining actionable count. "
            "Posts a NEW comment each invocation; for one live comment edited in place, use --sticky-receipt."
        ),
    )
    parser.add_argument(
        "--sticky-receipt",
        action="store_true",
        help=(
            "Like --post-receipt, but maintain ONE comment per PR that is edited in place across loop "
            "invocations. State persists in ~/.config/gh-gemini-review-loop/state.json "
            "(override with GGRL_STATE_DIR env var). Provides background visibility in the PR UI."
        ),
    )
    parser.add_argument(
        "--receipt-status",
        choices=["running", "done", "stopped"],
        default=None,
        help=(
            "Tag the receipt with a status header (RUNNING / DONE / STOPPED). Used with --sticky-receipt "
            "to communicate loop phase. Defaults to RUNNING for sticky, none for one-shot."
        ),
    )
    parser.add_argument(
        "--resolve-addressed-by-reply",
        dest="resolve_addressed_by_reply",
        action="store_true",
        default=True,
        help=(
            "Resolve unresolved threads where a non-bot maintainer posted a substantive reply "
            "(>=%d chars). Enabled by default." % ADDRESSED_BY_REPLY_MIN_CHARS
        ),
    )
    parser.add_argument(
        "--no-resolve-addressed-by-reply",
        dest="resolve_addressed_by_reply",
        action="store_false",
        help="Leave addressed-by-reply threads unresolved (they remain hidden from actionable output).",
    )
    parser.add_argument(
        "--include-addressed-by-reply",
        action="store_true",
        help="Include addressed-by-reply threads in actionable output (default: hidden).",
    )
    parser.add_argument(
        "--min-severity",
        choices=["critical", "high", "medium", "low"],
        default=None,
        help=(
            "Drop actionable threads below this Gemini-assigned severity. "
            "Threads without a Gemini severity marker ('unknown') are kept regardless "
            "(use --drop-unknown-severity to remove them too)."
        ),
    )
    parser.add_argument(
        "--drop-unknown-severity",
        action="store_true",
        help="Drop threads with no severity marker (can be used alone or with --min-severity).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call any GraphQL mutations; log intended writes to stderr instead.",
    )
    args = parser.parse_args()

    try:
        pr = resolve_pr(args.pr)
        if args.wait:
            pull_request = wait_for_stable_review(
                pr,
                author=args.author,
                timeout_seconds=args.timeout,
                interval_seconds=args.interval,
                quiet_seconds=args.quiet_period,
            )
        else:
            pull_request = fetch_threads(pr)
        resolved_outdated = 0
        resolved_addressed_by_reply = 0
        if args.no_agent_filter:
            agent_login: str | None = None
        else:
            agent_login = args.agent_login or gh_authenticated_login()
            if agent_login:
                print(f"Counting only re-reviews posted by '{agent_login}' toward the cap.", file=sys.stderr)
            else:
                print(
                    "warning: could not detect agent login via `gh api user`; "
                    "falling back to counting all re-review pings.",
                    file=sys.stderr,
                )
        rereviews = rereview_requests(pull_request, agent_login)
        limit_reached = len(rereviews) >= args.max_rereview_requests
        cap_blocks_writes = limit_reached and not args.ignore_loop_limit
        if args.resolve_outdated and cap_blocks_writes:
            print(
                f"warning: {len(rereviews)} Gemini re-review request(s) already exist; "
                f"skipping outdated-thread resolution because the loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
        elif args.resolve_outdated:
            resolved_outdated = resolve_outdated_threads(
                pull_request, args.author, dry_run=args.dry_run
            )
            if resolved_outdated:
                tag = "[dry-run] would resolve" if args.dry_run else "Resolved"
                print(f"{tag} {resolved_outdated} outdated {args.author} thread(s).", file=sys.stderr)
                if not args.dry_run:
                    pull_request = fetch_threads(pr)
        if args.resolve_addressed_by_reply and cap_blocks_writes:
            print(
                f"warning: skipping addressed-by-reply resolution; "
                f"re-review cap of {args.max_rereview_requests} already reached.",
                file=sys.stderr,
            )
        elif args.resolve_addressed_by_reply:
            resolved_addressed_by_reply = resolve_addressed_by_reply(
                pull_request, args.author, dry_run=args.dry_run
            )
            if resolved_addressed_by_reply:
                tag = "[dry-run] would resolve" if args.dry_run else "Resolved"
                print(
                    f"{tag} {resolved_addressed_by_reply} addressed-by-reply "
                    f"{args.author} thread(s).",
                    file=sys.stderr,
                )
                if not args.dry_run:
                    pull_request = fetch_threads(pr)
        threads = filter_threads(
            pull_request,
            author=args.author,
            include_resolved=args.include_resolved,
            include_outdated=args.include_outdated,
            include_addressed_by_reply=args.include_addressed_by_reply,
        )
        threads = sort_by_severity(threads)
        if args.min_severity or args.drop_unknown_severity:
            before = len(threads)
            threads = filter_by_min_severity(
                threads, args.min_severity, keep_unknown=not args.drop_unknown_severity
            )
            dropped = before - len(threads)
            if dropped:
                if args.min_severity:
                    tag = " (kept unknown-severity)" if not args.drop_unknown_severity else ""
                    msg = f"--min-severity {args.min_severity}: dropped {dropped} thread(s){tag}."
                else:
                    msg = f"--drop-unknown-severity: dropped {dropped} unknown-severity thread(s)."
                print(msg, file=sys.stderr)
        page_warnings = pagination_warnings(pull_request)
        for warning in page_warnings:
            print(f"warning: {warning}", file=sys.stderr)
        rereviews = rereview_requests(pull_request, agent_login)
        if len(rereviews) >= args.max_rereview_requests:
            print(
                f"warning: {len(rereviews)} Gemini re-review request(s) already exist; "
                f"the configured loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
        if args.post_receipt or args.sticky_receipt:
            sticky = args.sticky_receipt
            status = args.receipt_status.upper() if args.receipt_status else (
                "RUNNING" if sticky else None
            )
            receipt = render_receipt(
                pr, pull_request, threads,
                author=args.author,
                resolved_outdated=resolved_outdated,
                resolved_addressed_by_reply=resolved_addressed_by_reply,
                rereview_count=len(rereviews),
                rereview_limit=args.max_rereview_requests,
                status=status,
                sticky=sticky,
            )
            if sticky:
                post_or_update_sticky_receipt(pr, receipt, dry_run=args.dry_run)
            else:
                post_pr_comment(pr, receipt, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(
            json.dumps(
                {
                    "pullRequest": pull_request,
                    "threads": threads,
                    "loopStatus": {
                        "reReviewRequests": len(rereviews),
                        "reReviewLimit": args.max_rereview_requests,
                        "limitReached": len(rereviews) >= args.max_rereview_requests,
                        "agentLogin": agent_login,
                        "unresolvedOutdatedThreads": len(outdated_unresolved_threads(pull_request, args.author)),
                        "addressedByReplyThreads": len(addressed_by_reply_threads(pull_request, args.author)),
                        "resolvedOutdatedThreads": resolved_outdated,
                        "resolvedAddressedByReplyThreads": resolved_addressed_by_reply,
                        "severityCounts": severity_counts(threads),
                        "pageLimitWarnings": page_warnings,
                        "dryRun": args.dry_run,
                        "actionableThreadFingerprint": thread_fingerprint(threads),
                    },
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(render_markdown(pull_request, threads, args.author), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

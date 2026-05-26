#!/usr/bin/env python3
"""Fetch thread-aware Gemini Code Assist review comments for a GitHub PR."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any


DEFAULT_AUTHOR = "gemini-code-assist"
DEFAULT_REREVIEW_LIMIT = 3
REREVIEW_TRIGGER_RE = re.compile(r"@gemini-code-assist\b.*\breview\b", re.IGNORECASE | re.DOTALL)

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


def pagination_warnings(pull_request: dict[str, Any]) -> list[str]:
    """Return human-readable warnings when any GraphQL page hit its limit.

    Hitting the limit means the script may be silently dropping data — the
    loop should surface this so the user knows to paginate or scope the PR.
    """
    warnings: list[str] = []
    pr_comments = pull_request.get("comments", {}).get("nodes", []) or []
    reviews = pull_request.get("reviews", {}).get("nodes", []) or []
    threads = pull_request.get("reviewThreads", {}).get("nodes", []) or []
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
            f"reviewThreads hit page limit ({PAGE_LIMIT_REVIEW_THREADS}); older threads may be missing."
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


def rereview_requests(pull_request: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        comment
        for comment in pull_request.get("comments", {}).get("nodes", [])
        if REREVIEW_TRIGGER_RE.search(comment.get("body") or "")
    ]


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
        "--ignore-loop-limit",
        action="store_true",
        help="Allow --resolve-outdated even when the re-review request cap has already been reached.",
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
        rereviews = rereview_requests(pull_request)
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
        page_warnings = pagination_warnings(pull_request)
        for warning in page_warnings:
            print(f"warning: {warning}", file=sys.stderr)
        rereviews = rereview_requests(pull_request)
        if len(rereviews) >= args.max_rereview_requests:
            print(
                f"warning: {len(rereviews)} Gemini re-review request(s) already exist; "
                f"the configured loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
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
                        "reReviewRequests": len(rereview_requests(pull_request)),
                        "reReviewLimit": args.max_rereview_requests,
                        "limitReached": len(rereview_requests(pull_request)) >= args.max_rereview_requests,
                        "unresolvedOutdatedThreads": len(outdated_unresolved_threads(pull_request, args.author)),
                        "addressedByReplyThreads": len(addressed_by_reply_threads(pull_request, args.author)),
                        "resolvedOutdatedThreads": resolved_outdated,
                        "resolvedAddressedByReplyThreads": resolved_addressed_by_reply,
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

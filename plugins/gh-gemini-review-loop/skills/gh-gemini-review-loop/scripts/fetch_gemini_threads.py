#!/usr/bin/env python3
"""Fetch thread-aware Gemini Code Assist review comments for a GitHub PR."""

from __future__ import annotations

import argparse
import dataclasses
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

# Make the script's own directory importable so `from judge import ...` works
# when this script is invoked directly (e.g. `python3 .../fetch_gemini_threads.py`).
# Under `/plugin install` both files live in the same scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics  # noqa: E402 — sibling module, pure/stdlib-only


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


def derive_record_fields(
    *,
    baseline_ids: set[str],
    current_actionable_ids: set[str],
    addressed_by_reply_ids: set[str],
    outcome: str,
    judge_ran: bool,
    judge_results: dict[str, dict[str, Any]],
) -> dict[str, int]:
    """Compute the script-derived metric counts for a run record."""
    findings_fetched = len(baseline_ids | current_actionable_ids)
    observed_fixed_count = len(
        baseline_ids - current_actionable_ids - addressed_by_reply_ids
    )
    remaining_actionable = len(current_actionable_ids)
    addressed_by_reply = len(addressed_by_reply_ids)
    if judge_ran:
        needs_human = sum(
            1
            for r in judge_results.values()
            if r.get("verdict") == "needs_human"
        )
    elif outcome == "human":
        needs_human = remaining_actionable
    else:
        needs_human = 0
    return {
        "findings_fetched": findings_fetched,
        "observed_fixed_count": observed_fixed_count,
        "remaining_actionable": remaining_actionable,
        "addressed_by_reply": addressed_by_reply,
        "needs_human": needs_human,
    }


def _derive_outcome(remaining_actionable: int, verification: str, cap_reached: bool) -> str:
    if cap_reached:
        return "capped"
    if verification == "failed":
        return "verification_failed"
    if remaining_actionable == 0 and verification == "passed":
        return "clean"
    return "human"


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


def nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as err:
        raise argparse.ArgumentTypeError(
            f"invalid int value: {value!r}"
        ) from err
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def effective_rereview_limit(cli_value: int | None, prefs: dict[str, Any]) -> int:
    if cli_value is not None:
        return cli_value
    value = prefs.get("max_rereview_requests", DEFAULT_REREVIEW_LIMIT)
    if isinstance(value, bool):
        return DEFAULT_REREVIEW_LIMIT
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdigit():
            return int(stripped)
    return DEFAULT_REREVIEW_LIMIT


def _direct_preferences_path() -> Path:
    """Mirror ``judge.prefs_path()`` for the fallback path when ``judge`` is unavailable."""
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "preferences.json"


# Defaults that match the canonical ``judge.load_preferences()`` contract. The
# fallback path layers loaded values on top of this so downstream callers can
# read any documented key (``judge_mode``, ``judge_model``, etc.) without
# guarding against KeyError when the optional ``judge`` module is missing.
_FALLBACK_PREFS_DEFAULTS: dict[str, Any] = {
    "schema_version": 1,
    "judge_mode": "off",
    "judge_model": "gpt-4o-mini",
    "judge_tip_shown": False,
    "max_rereview_requests": DEFAULT_REREVIEW_LIMIT,
    "set_at": "",
}


def load_preferences_with_fallback() -> dict[str, Any]:
    """Load user preferences, falling back to a direct JSON read.

    Primary path: ``judge.load_preferences()`` (canonical loader with full
    schema validation). If the optional ``judge`` module cannot be imported
    (minimal install, broken install, vendored layout), read
    ``preferences.json`` directly so persistent settings like
    ``max_rereview_requests`` still take effect. Any failure is surfaced to
    stderr with a hint rather than silently dropped.

    The returned dict is always populated with the documented preference keys
    (defaults from ``_FALLBACK_PREFS_DEFAULTS`` overlaid by saved values), so
    callers can read keys like ``judge_model`` without KeyError handling.
    """
    try:
        from judge import load_preferences  # noqa: PLC0415
    except ImportError:
        print(
            "warning: optional 'judge' module not importable; reading "
            "preferences.json directly. Reinstall gh-gemini-review-loop "
            "to restore full judge-eval support.",
            file=sys.stderr,
        )
    else:
        try:
            return load_preferences()
        except Exception as err:
            print(
                f"warning: judge.load_preferences() failed ({err!s}); "
                "falling back to direct preferences.json load.",
                file=sys.stderr,
            )

    path = _direct_preferences_path()
    if not path.exists():
        prefs = dict(_FALLBACK_PREFS_DEFAULTS)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(prefs, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        except OSError:
            pass
        return prefs
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as err:
        print(
            f"warning: could not read {path} ({err!s}); ignoring saved preferences.",
            file=sys.stderr,
        )
        return dict(_FALLBACK_PREFS_DEFAULTS)
    if not isinstance(data, dict):
        print(
            f"warning: {path} did not contain a JSON object; ignoring saved preferences.",
            file=sys.stderr,
        )
        return dict(_FALLBACK_PREFS_DEFAULTS)
    return {**_FALLBACK_PREFS_DEFAULTS, **data}


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


def update_run_tracking(pr: PullRequest, findings: list[tuple[str, str | None]]) -> None:
    """Merge this invocation's findings into the run's tracking state.

    ``findings`` is a list of (thread_id, path) pairs. Sets ``started_at`` on
    the first call of a run; unions ids/paths on every call. Stored under the
    existing ``owner/repo#number`` key so it rides alongside sticky state.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    run = entry.get("run", {})
    if "started_at" not in run:
        run["started_at"] = _now_iso()
    ids = set(run.get("finding_ids", []))
    paths = set(run.get("finding_paths", []))
    for thread_id, path in findings:
        if thread_id:
            ids.add(thread_id)
        if path:
            paths.add(path)
    run["finding_ids"] = sorted(ids)
    run["finding_paths"] = sorted(paths)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_run_tracking(pr: PullRequest) -> dict[str, Any]:
    return load_sticky_state().get(_state_key(pr), {}).get("run", {})


def clear_run_tracking(pr: PullRequest) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    if key in state and "run" in state[key]:
        del state[key]["run"]
        save_sticky_state(state)


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
            f'.[] | select(.body != null and (.body | contains("{STICKY_RECEIPT_MARKER}"))) | .id',
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


def render_markdown(
    pr: dict[str, Any],
    threads: list[dict[str, Any]],
    author: str,
    *,
    judge_results: dict[str, dict[str, Any]] | None = None,
) -> str:
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
        # Optional judge annotation — only present when the user opted in via
        # --judge-mode (or saved preference) AND --judge-phase matched.
        judge_line = ""
        if judge_results:
            jr = judge_results.get(thread.get("id"))
            if jr and jr.get("status") == "ok":
                judge_line = (
                    f"  \n  > **Judge:** `{jr['verdict']}` (conf {jr['confidence']:.2f}, "
                    f"severity_override={jr['severity_override']}, "
                    f"action={jr['recommended_action']}). {jr.get('reason', '')}"
                )
            elif jr and jr.get("status") == "skipped":
                judge_line = f"  \n  > **Judge:** skipped — {jr.get('skip_reason', '')}"
        lines.append(f"## {index}. {path}:{line}{status_text}{judge_line}")
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
        type=nonnegative_int,
        default=None,
        help=(
            "Warn when prior Gemini re-review requests reach this limit. "
            "Overrides max_rereview_requests in ~/.config/gh-gemini-review-loop/preferences.json "
            f"(default: {DEFAULT_REREVIEW_LIMIT})."
        ),
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
    parser.add_argument(
        "--judge-mode",
        choices=["off", "on_cycle", "on_complete", "once"],
        default=None,
        help=(
            "Override the saved OpenAI-judge preference for this invocation. "
            "Without this flag the script reads ~/.config/gh-gemini-review-loop/preferences.json "
            "(default: 'off'). Requires an OpenAI API key resolved by key_resolver.py "
            "(env var / dotfile / OS keystore); gracefully skips otherwise."
        ),
    )
    parser.add_argument(
        "--judge-phase",
        choices=["cycle", "complete"],
        default=None,
        help=(
            "Which loop phase this invocation represents (the agent supplies this). "
            "'cycle' = before fixes each cycle; 'complete' = after the loop stops. "
            "Together with --judge-mode, controls whether the judge actually runs."
        ),
    )
    parser.add_argument(
        "--judge-model",
        default=None,
        help="Override the OpenAI model used by the judge for this invocation.",
    )
    parser.add_argument(
        "--record-run",
        action="store_true",
        help=(
            "Write one run-metrics record to runs.jsonl and print the [loop] Summary. "
            "Use once at loop end. Combine with --fixed-count and --verification."
        ),
    )
    parser.add_argument("--fixed-count", type=int, default=0, help="Agent-claimed fixes this run.")
    parser.add_argument(
        "--verification",
        choices=["passed", "failed", "skipped"],
        default="skipped",
        help="Result of the verification step this run.",
    )
    parser.add_argument(
        "--verification-details",
        default=None,
        help="Optional JSON object with structured verification context.",
    )
    parser.add_argument(
        "--outcome",
        choices=list(metrics.VALID_OUTCOMES),
        default=None,
        help="Terminal outcome of the loop. If omitted, derived from state.",
    )
    parser.add_argument("--outcome-reason", default=None, help="One-line reason for --outcome.")
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print local Gemini-loop stats for this repo from runs.jsonl and exit.",
    )
    parser.add_argument(
        "--stats-window",
        type=int,
        default=metrics.DEFAULT_WINDOW,
        help=f"Number of most-recent runs to aggregate. Default: {metrics.DEFAULT_WINDOW}.",
    )
    parser.add_argument(
        "--stats-all-repos",
        action="store_true",
        help="Aggregate across all repos instead of only the current one.",
    )
    args = parser.parse_args()

    try:
        prefs = load_preferences_with_fallback()
        args.max_rereview_requests = effective_rereview_limit(
            args.max_rereview_requests, prefs
        )
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

        # ---- Judge (optional, opt-in via prefs file or --judge-mode) -------
        # The script is the single source of truth for whether the judge
        # runs. The agent supplies --judge-phase; we read the saved mode
        # from ~/.config/gh-gemini-review-loop/preferences.json (or
        # --judge-mode override) and decide.
        try:
            from judge import (  # noqa: PLC0415
                JudgeClient, JudgeError, should_judge_run, skipped_result,
            )
            judge_available = True
        except ImportError:
            judge_available = False
            judge_results: dict[str, dict[str, Any]] = {}
            judge_status = {
                "ran": False,
                "skip_reason": "judge.py not importable (plugin install path issue)",
            }
        if judge_available:
            effective_mode = (
                args.judge_mode
                if args.judge_mode is not None
                else prefs.get("judge_mode", "off")
            )
            judge_results = {}
            if should_judge_run(mode=effective_mode, phase=args.judge_phase):
                client = JudgeClient(model=args.judge_model or prefs["judge_model"])
                ready, skip_reason = client.is_ready()
                if not ready:
                    judge_status = {
                        "ran": False,
                        "mode": effective_mode,
                        "phase": args.judge_phase,
                        "skip_reason": skip_reason,
                    }
                    print(
                        f"info: judge skipped — {skip_reason}. "
                        "Loop continues unchanged.",
                        file=sys.stderr,
                    )
                else:
                    judge_status = {
                        "ran": True,
                        "mode": effective_mode,
                        "phase": args.judge_phase,
                        "model": client.model,
                        "judged_count": 0,
                        "errors": 0,
                    }
                    for thread in threads:
                        # Build a single judge input from the first matching
                        # bot comment in this filtered thread.
                        body = (thread.get("comments") or [{}])[0].get("body") or ""
                        finding_payload = {
                            "severity": thread_severity(thread),
                            "path": thread.get("path"),
                            "line": thread.get("line") or thread.get("originalLine"),
                            "body": body,
                            "diff_hunk": (thread.get("comments") or [{}])[0].get("diffHunk") or "",
                        }
                        try:
                            jr = client.judge(finding_payload)
                            judge_results[thread["id"]] = dataclasses.asdict(jr)
                            if jr.status == "ok":
                                judge_status["judged_count"] += 1
                        except JudgeError as exc:
                            judge_results[thread["id"]] = dataclasses.asdict(
                                skipped_result(f"judge error: {exc}")
                            )
                            judge_status["errors"] += 1
            else:
                judge_status = {
                    "ran": False,
                    "mode": effective_mode,
                    "phase": args.judge_phase,
                    "skip_reason": (
                        f"mode={effective_mode!r} + phase={args.judge_phase!r} → no-op"
                    ),
                }
        # --------------------------------------------------------------------

        rereviews = rereview_requests(pull_request, agent_login)
        if len(rereviews) >= args.max_rereview_requests:
            print(
                f"warning: {len(rereviews)} Gemini re-review request(s) already exist; "
                f"the configured loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
        if args.record_run:
            update_run_tracking(
                pr, [(t["id"], t.get("path")) for t in threads]
            )
            run = read_run_tracking(pr)
            baseline_ids = set(run.get("finding_ids", []))
            finding_paths = run.get("finding_paths", [])
            current_actionable_ids = {t["id"] for t in threads}
            addressed_by_reply_ids = {
                t["id"] for t in addressed_by_reply_threads(pull_request, args.author)
            }
            judge_ran = bool(judge_status.get("ran"))
            cap_reached = len(rereviews) >= args.max_rereview_requests
            outcome = args.outcome or _derive_outcome(
                len(current_actionable_ids), args.verification, cap_reached
            )
            derived = derive_record_fields(
                baseline_ids=baseline_ids,
                current_actionable_ids=current_actionable_ids,
                addressed_by_reply_ids=addressed_by_reply_ids,
                outcome=outcome,
                judge_ran=judge_ran,
                judge_results=judge_results,
            )
            verification_details: dict[str, Any] = {}
            if args.verification_details:
                try:
                    verification_details = json.loads(args.verification_details)
                except json.JSONDecodeError:
                    print(
                        "warning: --verification-details is not valid JSON; storing {}.",
                        file=sys.stderr,
                    )
            record = metrics.build_record(
                repo=f"{pr.owner}/{pr.repo}",
                pr=pr.number,
                provider=args.author,
                fixed_count=args.fixed_count,
                cycles_used=len(rereviews),
                cycle_cap=args.max_rereview_requests,
                verification=args.verification,
                verification_details=verification_details,
                outcome=outcome,
                outcome_reason=args.outcome_reason or f"outcome: {outcome}",
                started_at=run.get("started_at"),
                finding_paths=finding_paths,
                judge=metrics.build_judge_block(judge_ran, judge_results),
                **derived,
            )
            try:
                metrics.append_record(record)
            except OSError as exc:
                print(f"warning: could not record run metrics: {exc}", file=sys.stderr)
            else:
                clear_run_tracking(pr)
            print(metrics.format_run_summary(record))
            return 0
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
                        "judge": judge_status,
                    },
                    "judgeResults": judge_results,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            render_markdown(
                pull_request, threads, args.author, judge_results=judge_results
            ),
            end="",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

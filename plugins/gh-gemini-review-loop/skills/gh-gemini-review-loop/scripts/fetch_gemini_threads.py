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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Make the script's own directory importable so `from judge import ...` works
# when this script is invoked directly (e.g. `python3 .../fetch_gemini_threads.py`).
# Under `/plugin install` both files live in the same scripts/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import metrics  # noqa: E402 — sibling module, pure/stdlib-only
from loop_color import color_loop, colors_enabled  # noqa: E402 — sibling module


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


def select_stats_records(
    records: list[dict[str, Any]], *, repo: str, window: int, all_repos: bool
) -> list[dict[str, Any]]:
    if not all_repos:
        records = [r for r in records if r.get("repo") == repo]
    return records[-window:] if window > 0 else records


def resolve_current_repo() -> str:
    """Return 'owner/repo' for the current dir without needing an open PR."""
    view = run_gh(["repo", "view", "--json", "nameWithOwner"])
    if not isinstance(view, dict) or "nameWithOwner" not in view:
        raise RuntimeError("Could not resolve the current repo with gh repo view.")
    return view["nameWithOwner"]


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
            for tid, r in judge_results.items()
            if tid in current_actionable_ids and r.get("verdict") == "needs_human"
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


def _derive_outcome(
    remaining_actionable: int,
    verification: str,
    cap_reached: bool,
    *,
    gemini_confirmed: bool = True,
    likely_fixed_remaining: int = 0,
) -> str:
    """Best-effort outcome when the agent does not pass --outcome explicitly.

    Produces the outcomes inferable from script-visible state plus the two
    agent-supplied signals that disambiguate the terminal cycle:

    - ``gemini_confirmed`` — the final re-review actually responded after the
      re-review request (False when the final wait timed out). Defaults True so
      legacy callers are unaffected; ``clean`` is only ever returned when True.
    - ``likely_fixed_remaining`` — count of still-open threads that are
      multi-signal "likely fixed" (see metrics.classify_finding_state).

    Logic (corrections folded in):
      - verification failed                                  -> verification_failed
      - unconfirmed final wait + everything resolved/likely-fixed
                                                             -> fixed_pending_confirmation
      - cap reached (genuine unfixed remaining)              -> capped
      - no remaining + passed + gemini_confirmed             -> clean
      - else                                                 -> human

    ``regression`` / ``no_progress`` encode an agent judgment the script cannot
    see, so the agent must pass them via --outcome.
    """
    if verification == "failed":
        return "verification_failed"

    # Everything still open is multi-signal "likely fixed".
    likely_all_fixed = (
        remaining_actionable > 0 and likely_fixed_remaining >= remaining_actionable
    )
    # No open threads and the suite passed — it *looks* resolved.
    looks_resolved = remaining_actionable == 0 and verification == "passed"

    # Final wait timed out (Gemini never re-confirmed) but state otherwise looks
    # fixed: never guess clean or capped — report pending confirmation.
    if not gemini_confirmed and (likely_all_fixed or looks_resolved):
        return "fixed_pending_confirmation"

    if looks_resolved and gemini_confirmed:
        return "clean"
    if cap_reached:
        return "capped"
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
    "schema_version": 2,
    "judge_mode": "off",
    "judge_model": "gpt-4o-mini",
    "judge_tip_shown": False,
    "max_rereview_requests": DEFAULT_REREVIEW_LIMIT,
    "profiles": {},
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
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    # state.json must be a JSON object. A valid-but-non-dict payload (a list or
    # scalar from corruption or hand-editing) would crash callers that do
    # .values()/.items() on the result. Guard centrally here so every caller —
    # any_active_run, find_active_run, and the rest — is safe.
    return data if isinstance(data, dict) else {}


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
    # Monotonic counter bumped on every fetch. The loop's Stop-hook backstop
    # compares it against last_summary_seq to tell whether the run advanced
    # since the agent last emitted a summary (see summary_is_stale).
    run["update_seq"] = _safe_int(run.get("update_seq", 0)) + 1
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


def begin_wait_chunk(pr: PullRequest, after_iso: str | None) -> dict[str, Any]:
    """Open one wait chunk: load cross-chunk wait state, applying the reset rule.

    Reset rule (prevents cross-cycle leakage): if the stored anchor differs
    from ``after_iso``, all wait progress (started_at, checks, settle state)
    belongs to a previous cycle's wait and is discarded. ``checks`` counts
    chunk invocations, incremented once per call. Fails open: state I/O
    errors yield a fresh single-chunk state rather than crashing the wait.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    if wait.get("after") != after_iso:
        wait = {"after": after_iso}
    if not isinstance(wait.get("started_at"), str) or not wait.get("started_at"):
        wait["started_at"] = metrics.now_iso()
    wait["checks"] = _safe_int(wait.get("checks")) + 1
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)
    return wait


def read_wait_state(pr: PullRequest) -> dict[str, Any]:
    run = read_run_tracking(pr)
    wait = run.get("wait") if isinstance(run, dict) else None
    return dict(wait) if isinstance(wait, dict) else {}


def _update_wait_state(pr: PullRequest, updates: dict[str, Any]) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    wait = run.get("wait")
    wait = dict(wait) if isinstance(wait, dict) else {}
    wait.update(updates)
    run["wait"] = wait
    entry["run"] = run
    state[key] = entry
    try:
        save_sticky_state(state)
    except OSError as exc:
        print(f"warning: could not persist wait state: {exc}", file=sys.stderr)


def save_wait_settle(pr: PullRequest, fingerprint: str, since_iso: str) -> None:
    """Persist the settle phase so a chunk boundary never restarts the quiet period."""
    _update_wait_state(pr, {"stable_fingerprint": fingerprint, "stable_since": since_iso})


def save_wait_snapshot(pr: PullRequest, snapshot: dict[str, Any]) -> None:
    """Persist the last non-ready chunk result for --wait-heartbeat rendering."""
    _update_wait_state(pr, {"last_snapshot": dict(snapshot)})


def clear_wait_state(pr: PullRequest) -> None:
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if isinstance(entry, dict) and isinstance(entry.get("run"), dict) and "wait" in entry["run"]:
        del entry["run"]["wait"]
        try:
            save_sticky_state(state)
        except OSError as exc:
            print(f"warning: could not clear wait state: {exc}", file=sys.stderr)


def accumulate_fixed_markers(
    pr: PullRequest,
    *,
    fingerprints: list[str] | None = None,
    paths: list[str] | None = None,
) -> None:
    """Merge agent-supplied "fixed locally" markers into run tracking.

    The agent passes ``--fixed-finding <fp>`` (finding fingerprints) and/or
    fixed file paths as it remediates each cycle. They accumulate (union) under
    the run entry so the terminal classification can read which findings the
    agent claims to have fixed — one of the multi-signal inputs to
    metrics.classify_finding_state. Stored alongside, never replacing, the
    existing run-tracking ids/paths.
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    fps = {x for x in run.get("fixed_finding_fps", []) if isinstance(x, str)}
    fpaths = {x for x in run.get("fixed_paths", []) if isinstance(x, str)}
    for fp in fingerprints or []:
        if fp:
            fps.add(fp)
    for p in paths or []:
        if p:
            fpaths.add(p)
    run["fixed_finding_fps"] = sorted(fps)
    run["fixed_paths"] = sorted(fpaths)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def read_fixed_markers(pr: PullRequest) -> dict[str, set[str]]:
    """Return the accumulated fixed markers as ``{"fingerprints", "paths"}`` sets.

    Empty sets when none were recorded or the state is missing/corrupt.
    """
    run = read_run_tracking(pr)
    if not isinstance(run, dict):
        run = {}
    fps = run.get("fixed_finding_fps", [])
    fpaths = run.get("fixed_paths", [])
    return {
        "fingerprints": {x for x in fps if isinstance(x, str)} if isinstance(fps, list) else set(),
        "paths": {x for x in fpaths if isinstance(x, str)} if isinstance(fpaths, list) else set(),
    }


def changed_files_in_range(
    base: str | None, head: str | None, *, cwd: str | None = None
) -> set[str]:
    """Best-effort set of files changed in ``base..head`` via git.

    Used to compute the ``file_changed`` signal: did the finding's path actually
    change in the fixing commits? Fails OPEN — any git error (not a repo, bad
    revs, git missing) yields an empty set rather than crashing the loop.
    """
    if not base or not head:
        return set()
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base}..{head}"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


LIKELY_FIXED_FINDING_STATES = {
    "fixed_pushed_awaiting_review",
    "fixed_pending_confirmation",
    "stale_already_fixed",
}


def classify_remaining_finding_states(
    threads: list[dict[str, Any]],
    *,
    fixed_fingerprints: set[str],
    fixed_paths: set[str],
    changed_paths: set[str],
    prior_fingerprints: set[str],
    judge_results: dict[str, dict[str, Any]],
    cap_reached: bool,
) -> dict[str, str]:
    """Classify the still-actionable findings for terminal outcome derivation.

    The state machine lives in ``metrics.classify_finding_state``. This adapter
    only builds its explicit signal booleans from script-visible state and
    agent-supplied fixed markers.
    """
    states: dict[str, str] = {}
    for index, thread in enumerate(threads):
        if not isinstance(thread, dict):
            continue
        fingerprint = finding_fingerprint(thread)
        path_val = thread.get("path")
        path = path_val if isinstance(path_val, str) else ""
        fixed_by_marker = fingerprint in fixed_fingerprints or path in fixed_paths
        file_changed = path in changed_paths or path in fixed_paths
        thread_id = thread.get("id")
        result_key = thread_id if isinstance(thread_id, str) else fingerprint
        judge_result = judge_results.get(result_key, {}) if result_key else {}
        state = metrics.classify_finding_state(
            {
                "judge_needs_human": judge_result.get("verdict") == "needs_human",
                "carried_over": fingerprint in prior_fingerprints,
                "fixed_locally": fixed_by_marker,
                "file_changed": file_changed,
                "gemini_confirmed": False,
                "cap_reached": cap_reached,
            }
        )
        states[result_key or f"finding-{index}"] = state
    return states


def count_likely_fixed_remaining(states: dict[str, str]) -> int:
    return sum(1 for state in states.values() if state in LIKELY_FIXED_FINDING_STATES)


def track_finding_fingerprints(pr: PullRequest, current_fps: set[str]) -> dict[str, set[str]]:
    """Snapshot the prior cycle's finding fingerprints, then union the current.

    Call ONLY on a real agent fetch (not ``--cycle-summary`` / ``--record-run``),
    exactly once per cycle. It moves the running union into
    ``prior_seen_finding_fps`` *before* folding in this cycle's fingerprints, so
    a later read-only ``--cycle-summary`` can ask "was this finding present in a
    previous cycle?" without the current cycle's own fetch poisoning the answer.

    Returns ``{"prior": <prior union>, "new": <current − prior>}``. Fails open:
    any state I/O error yields an empty prior (everything reads as new).
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prior_val = run.get("seen_finding_fps", [])
    prior = {x for x in prior_val if isinstance(x, str)} if isinstance(prior_val, list) else set()
    run["prior_seen_finding_fps"] = sorted(prior)
    run["seen_finding_fps"] = sorted(prior | current_fps)
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return {"prior": prior, "new": current_fps - prior}


def prior_finding_fingerprints(pr: PullRequest) -> set[str]:
    """Finding fingerprints seen in cycles *before* the current one.

    Read by the receipt path to mark a current finding as carried-over. Empty
    on cycle 1 (no prior cycle) — so every cycle-1 finding reads as new.
    """
    state = load_sticky_state()
    entry = state.get(_state_key(pr), {})
    run = entry.get("run", {}) if isinstance(entry, dict) else {}
    value = run.get("prior_seen_finding_fps", []) if isinstance(run, dict) else []
    return {x for x in value if isinstance(x, str)} if isinstance(value, list) else set()


def _safe_int(value: Any) -> int:
    """Coerce a state value to int, returning 0 for missing/corrupt values.

    state.json is local and can be hand-edited or corrupted; the seq fields
    must never raise from a non-numeric value, so a bad value reads as 0.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def detect_no_progress(pr: PullRequest, current_fingerprint: str) -> bool:
    """True when the actionable thread fingerprint is unchanged from last cycle.

    Call AFTER ``update_run_tracking`` so ``update_seq`` reflects the current
    fetch. Reads the fingerprint stored by the previous call, stores the new
    one, and returns True only when both conditions hold:

    - ``update_seq >= 2``: at least two fetches have run (one per-cycle fix
      attempt has already been made without clearing the set of open threads).
    - The fingerprint is identical to the one stored on the previous call.

    A True return means no observable code change resolved any of the threads
    the agent was supposed to fix. The agent should stop with
    ``--record-run --outcome no_progress``.

    Fails OPEN: any state read/write error allows the push (correctness aid
    must never wedge the loop).
    """
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key, {})
    if not isinstance(entry, dict):
        entry = {}
    run = entry.get("run", {})
    if not isinstance(run, dict):
        run = {}
    prev_fingerprint = run.get("thread_fingerprint")
    update_seq = _safe_int(run.get("update_seq", 0))
    run["thread_fingerprint"] = current_fingerprint
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return bool(
        prev_fingerprint
        and prev_fingerprint == current_fingerprint
        and update_seq >= 2
    )


def any_active_run() -> bool:
    """True if any tracked PR (any repo) has an active loop run.

    Repo-agnostic and network-free, so the Stop hook can skip git/repo
    resolution entirely on the common case of no loop in flight.
    """
    state = load_sticky_state()
    if not isinstance(state, dict):
        return False
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        run = entry.get("run")
        # "Active" means a fetch bumped update_seq under the current code. A
        # started_at without update_seq is legacy/pre-feature cruft (or a
        # cleared run) and must not count, or stale entries from any repo would
        # look active forever.
        if isinstance(run, dict) and _safe_int(run.get("update_seq")) > 0:
            return True
    return False


def find_active_run(repo_full: str) -> tuple[int, dict[str, Any]] | None:
    """Return ``(pr_number, run)`` for the active Gemini loop in ``repo_full``.

    A loop is "active" once a cycle has accumulated into its ``run`` block
    (signalled by ``started_at``); ``--record-run`` clears that block, so a
    recorded/never-started loop reads as inactive. This is the cheap,
    network-free gate the loop hooks use to decide whether to spend a GitHub
    fetch on a summary, so it only reads local state. If several PRs in the
    repo look active, the most recently started one wins.
    """
    candidates: list[tuple[str, int, dict[str, Any]]] = []
    state = load_sticky_state()
    if not isinstance(state, dict):
        return None
    for key, entry in state.items():
        prefix, sep, suffix = key.rpartition("#")
        if not sep or prefix != repo_full:
            continue
        try:
            number = int(suffix)
        except ValueError:
            continue
        run = entry.get("run") if isinstance(entry, dict) else None
        # Active = bumped update_seq under current code (see any_active_run);
        # a started_at-only run is legacy cruft and is skipped. Order by
        # started_at so the most recently begun loop wins when several qualify.
        if isinstance(run, dict) and _safe_int(run.get("update_seq")) > 0:
            # str() the sort key so a corrupted non-str started_at can't
            # TypeError when sorting candidates of mixed types.
            candidates.append((str(run.get("started_at", "")), number, run))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _, number, run = candidates[-1]
    return number, run


def summary_is_stale(run: dict[str, Any]) -> bool:
    """True when the run has advanced since the last emitted summary.

    ``update_seq`` is bumped on every fetch; ``last_summary_seq`` is stamped
    when a summary is emitted. A run that fetched but was never summarized
    (``last_summary_seq`` absent → 0) is stale. A run with ``started_at`` but no
    fetch yet (``update_seq`` 0) has nothing to show and is not stale.
    """
    return _safe_int(run.get("update_seq")) > _safe_int(run.get("last_summary_seq"))


def stamp_summary_emitted(pr: PullRequest) -> None:
    """Record that a summary covering the current run state was just emitted.

    Sets ``last_summary_seq`` to the current ``update_seq`` so the Stop-hook
    backstop won't re-emit a summary the agent already showed. A no-op when no
    run is being tracked, so it never resurrects a cleared/absent run block.
    """
    state = load_sticky_state()
    if not isinstance(state, dict):
        return
    key = _state_key(pr)
    entry = state.get(key)
    if not isinstance(entry, dict):
        return
    run = entry.get("run")
    if not isinstance(run, dict) or "update_seq" not in run:
        return
    seq = _safe_int(run.get("update_seq"))
    if seq <= 0:
        return  # corrupt/non-numeric update_seq — nothing meaningful to stamp
    run["last_summary_seq"] = seq
    state[key]["run"] = run
    save_sticky_state(state)


def accumulate_judge_results(
    pr: PullRequest, judge_results: dict[str, dict[str, Any]]
) -> None:
    """Persist this cycle's judge verdicts into the run accumulator.

    Verdicts are keyed by thread id so a later cycle's re-judgement of the same
    thread supersedes an earlier one. Stored alongside the run's finding ids /
    paths so the terminal ``--record-run`` can build a complete judge block even
    after the findings themselves have been resolved. A no-op when there are no
    results, so a run that never judged keeps no ``judge_ran`` flag.
    """
    if not judge_results:
        return
    state = load_sticky_state()
    key = _state_key(pr)
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
    else:
        entry = dict(entry)
    run = entry.get("run")
    if not isinstance(run, dict):
        run = {}
    else:
        run = dict(run)
    stored = run.get("judge_results")
    stored_dict = dict(stored) if isinstance(stored, dict) else {}
    stored_dict.update(judge_results)
    run["judge_results"] = stored_dict
    run["judge_ran"] = True
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)


def merge_judge_results(
    accumulated: dict[str, dict[str, Any]] | None,
    current: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    """Union accumulated and current verdicts; current supersedes per thread id."""
    merged = dict(accumulated or {})
    merged.update(current or {})
    return merged


def resolve_judge_phase(explicit_phase: str | None, *, record_run: bool) -> str:
    """Infer the judge phase from invocation context when unset.

    The agent need not remember to pass --judge-phase: a terminal --record-run
    invocation is the loop's completion, everything else is a per-cycle fetch.
    An explicit flag always wins.
    """
    if explicit_phase is not None:
        return explicit_phase
    return "complete" if record_run else "cycle"


def record_cycle(
    pr: PullRequest,
    started_at: str,
    finding_count: int,
    outcome: str,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Append one active-cycle timing entry to the run accumulator.

    A *cycle* is one active remediation attempt — fetch, analyze, edit, verify,
    prepare/post the response. It measures active work only: the agent passes
    ``started_at`` as the moment active work for this cycle began, so waits
    between cycles (ScheduleWakeup, polling, rate-limit sleeps, idle time) are
    excluded by construction. ``outcome`` is "continued" for a non-terminal
    cycle or the terminal outcome label for the last one. Returns the entry.
    """
    finished_at = finished_at or _now_iso()
    duration = metrics._duration_seconds(started_at, finished_at)
    cycle = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration,
        "finding_count": int(finding_count),
        "outcome": outcome,
    }
    state = load_sticky_state()
    # The state file can be hand-edited or corrupt; verify each level is the
    # expected type before use (a non-dict state would AttributeError on .get,
    # and dict()/list() on a string/int would raise), mirroring
    # accumulate_judge_results.
    state = state if isinstance(state, dict) else {}
    key = _state_key(pr)
    entry = state.get(key)
    entry = dict(entry) if isinstance(entry, dict) else {}
    run = entry.get("run")
    run = dict(run) if isinstance(run, dict) else {}
    cycles = run.get("cycles")
    cycles = list(cycles) if isinstance(cycles, list) else []
    cycles.append(cycle)
    run["cycles"] = cycles
    entry["run"] = run
    state[key] = entry
    save_sticky_state(state)
    return cycle


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


def finding_fingerprint(thread: dict[str, Any]) -> str:
    """Content identity of a single finding, stable across re-posts.

    Gemini re-posts the same suggestion as a *new* thread (new id) on each
    re-review, so the thread id cannot tell a fresh finding from a carried-over
    one. The path plus the normalized suggestion text can: we strip the leading
    severity image markdown Gemini prepends and collapse whitespace, so cosmetic
    differences (line-number echoes, re-wrapping) don't change the fingerprint.
    """
    if not isinstance(thread, dict):
        return ""
    path_val = thread.get("path")
    path = path_val if path_val is not None else ""
    comments = _iter_comments(thread)
    first_comment = comments[0].get("body") if comments and isinstance(comments[0], dict) else None
    body = first_comment if isinstance(first_comment, str) else ""
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)  # drop severity image
    body = re.sub(r"\s+", " ", body).strip().lower()
    return hashlib.sha1(f"{path}\n{body[:1000]}".encode()).hexdigest()[:16]


def review_activity_fingerprint(
    pull_request: dict[str, Any],
    author: str,
    after_iso: str | None = None,
) -> str | None:
    """Compute a hash of all Gemini review activity in ``pull_request``.

    Returns ``None`` when no matching activity exists (caller keeps polling).

    ``after_iso`` — optional ISO-8601 lower-bound (exclusive).  When set,
    only reviews submitted after that timestamp and only threads whose newest
    comment was created after that timestamp are counted.  Pass the timestamp
    of the re-review request comment so the waiter ignores prior-cycle
    activity and only returns once Gemini has genuinely responded to the new
    cycle's push.
    """
    reviews = filter_reviews(pull_request, author)
    if after_iso:
        reviews = [r for r in reviews if (r.get("submittedAt") or "") > after_iso]

    authored_threads = filter_threads(
        pull_request,
        author=author,
        include_resolved=True,
        include_outdated=True,
    )
    if after_iso:
        # A thread counts as "new" when at least one of its comments was
        # created after the anchor.  Using the newest comment timestamp means
        # a thread where Gemini added a follow-up reply after the re-review
        # request is also captured correctly.
        authored_threads = [
            t for t in authored_threads
            if any(
                (c.get("createdAt") or "") > after_iso
                for c in t.get("comments", [])
            )
        ]

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
    after_iso: str | None = None,
) -> dict[str, Any]:
    """Poll until ``author``'s review activity on ``pr`` is present and stable.

    ``after_iso`` anchors the wait to a specific point in time (ISO-8601,
    exclusive).  When set, review activity submitted at or before that
    timestamp is ignored — the poller only returns once Gemini has posted
    activity *after* that timestamp.  This prevents a cycle-2 wait from
    returning immediately because cycle-1's review is already stable.

    Typical usage: pass the ``createdAt`` of the re-review request comment
    that triggered the new cycle, so the wait is anchored to that request.
    """
    deadline = time.monotonic() + timeout_seconds
    last_fingerprint: str | None = None
    stable_since: float | None = None

    if after_iso:
        print(
            f"Waiting for {author} review activity after {after_iso}...",
            file=sys.stderr,
        )

    while True:
        pull_request = fetch_threads(pr)
        fingerprint = review_activity_fingerprint(pull_request, author, after_iso=after_iso)
        now = time.monotonic()

        if fingerprint is None:
            if not after_iso:
                print(f"Waiting for {author} review activity...", file=sys.stderr)
        elif after_iso is None:
            # Cycle 1 / initial review: activity is present, so return at once
            # without waiting for it to settle. At the initial review there
            # either are comments or there aren't — the quiet-period settle only
            # earns its keep on cycle 2+ (after_iso set), where a freshly pushed
            # fix needs Gemini's re-review to stabilize before we fetch.
            print(f"Detected {author} review activity.", file=sys.stderr)
            return pull_request
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


def format_judge_verdict_summary(
    judge_results: dict[str, dict[str, Any]],
    phase: str,
) -> str:
    """One-line verdict breakdown for agent narration relay.

    Printed after the markdown thread list so the agent can copy it verbatim
    into its text response. Only threads where the judge actually produced a
    verdict (status == "ok") are counted; skipped threads are excluded.

    Output format (intended for literal copy-paste into agent text):
        [loop] judge (cycle): 3 thread(s) — valid_actionable: 2, false_positive: 1
    """
    verdicts: Counter[str] = Counter(
        r["verdict"]
        for r in judge_results.values()
        if r.get("status") == "ok" and r.get("verdict")
    )
    if not verdicts:
        return f"[loop] judge ({phase}): evaluated {len(judge_results)} thread(s) — all skipped/errored"
    total = sum(verdicts.values())
    breakdown = ", ".join(
        f"{verdict}: {count}"
        for verdict, count in sorted(verdicts.items(), key=lambda kv: -kv[1])
    )
    return f"[loop] judge ({phase}): {total} thread(s) evaluated — {breakdown}"


def format_judge_thread_table(
    threads: list[dict[str, Any]],
    judge_results: dict[str, dict[str, Any]],
    phase: str,
) -> str:
    """Per-thread judge decision table for agent narration relay.

    Printed after the markdown thread list so the agent can copy it verbatim
    into its text response. Each row shows the thread location, severity,
    verdict, recommended action, and confidence — all in one place, outside
    the collapsed thread markdown block.

    Format:
        [loop] judge eval (cycle): 3 thread(s)
          1 src/foo.py:42 [high] — valid_actionable · fix · conf 0.91
          2 src/bar.py:15 [medium] — needs_human · escalate · conf 0.84
          3 src/baz.py:7 [low] — skipped (no API key)
    """
    rows = []
    for i, thread in enumerate(threads, 1):
        path = thread.get("path") or "?"
        line = thread.get("line") or thread.get("originalLine") or "?"
        sev = thread_severity(thread)
        tid = thread.get("id", "")
        jr = judge_results.get(tid)
        if jr and jr.get("status") == "ok":
            verdict = jr.get("verdict", "?")
            action = jr.get("recommended_action", "?")
            conf = jr.get("confidence", 0.0)
            try:
                conf_str = f"{float(conf):.2f}"
            except (TypeError, ValueError):
                conf_str = "?"
            rows.append(
                f"  {i} {path}:{line} [{sev}] — {verdict} · {action} · conf {conf_str}"
            )
        elif jr and jr.get("status") == "skipped":
            reason = jr.get("skip_reason", "unknown")
            rows.append(f"  {i} {path}:{line} [{sev}] — skipped ({reason})")
        else:
            rows.append(f"  {i} {path}:{line} [{sev}] — not evaluated")
    n = len(threads)
    header = f"[loop] judge eval ({phase}): {n} thread(s)"
    return "\n".join([header] + rows)


def load_profile_for_repo(repo: str) -> dict[str, Any] | None:
    """Best-effort saved verification profile lookup for formatter commands."""
    try:
        from judge import get_profile  # noqa: PLC0415
    except ImportError:
        return None
    try:
        return get_profile(repo)
    except Exception:
        return None


def judge_eval_requested(judge_status: dict[str, Any]) -> bool:
    if not isinstance(judge_status, dict) or judge_status.get("ran"):
        return False
    mode = judge_status.get("mode")
    reason = str(judge_status.get("skip_reason") or "")
    return mode in {"on_cycle", "on_complete", "once"} and "no-op" not in reason


def color_loop_block(block: str, *, enabled: bool) -> str:
    """Color only human-readable loop-owned blocks."""
    if isinstance(block, str) and block.startswith("[loop]"):
        return color_loop(block, enabled=enabled)
    return block


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
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Verification profiles: the loop detects a per-repo check profile on "
            "first run (pytest/ruff, npm scripts, cargo, go) and stores it in "
            "preferences.json. Say \"set up a verification profile for this repo\" "
            "to configure, or \"skip verification profile\" to opt out."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pr", help="PR URL or OWNER/REPO#NUMBER. Defaults to current branch PR.")
    parser.add_argument("--repo", default=None, help="OWNER/REPO for formatter-only commands.")
    parser.add_argument("--author", default=DEFAULT_AUTHOR, help=f"Review author login. Default: {DEFAULT_AUTHOR}")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI color for human-readable [loop] blocks.",
    )
    parser.add_argument(
        "--profile-intro",
        action="store_true",
        help="Print the saved verification-profile intro block for --repo and exit.",
    )
    parser.add_argument(
        "--planned-verification",
        action="store_true",
        help="Print the planned repo-aware verification block for --repo and exit.",
    )
    parser.add_argument("--include-resolved", action="store_true")
    parser.add_argument("--include-outdated", action="store_true")
    parser.add_argument("--wait", action="store_true", help="Poll until Gemini review activity appears and is stable.")
    parser.add_argument("--timeout", type=int, default=900, help="Seconds to wait with --wait. Default: 900.")
    parser.add_argument("--interval", type=int, default=20, help="Polling interval in seconds with --wait. Default: 20.")
    parser.add_argument("--quiet-period", type=int, default=45, help="Stable activity period in seconds with --wait. Default: 45.")
    parser.add_argument(
        "--after",
        metavar="ISO8601",
        default=None,
        help=(
            "With --wait, ignore Gemini activity submitted at or before this "
            "ISO-8601 timestamp. Pass the createdAt of the re-review request "
            "comment so the waiter only returns once Gemini has genuinely "
            "responded to the new cycle's push, not to prior-cycle reviews."
        ),
    )
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
    # Cleanup (resolve-outdated, resolve-addressed-by-reply) now always runs
    # regardless of the re-review cap. --resolve-past-cap / --ignore-loop-limit
    # are kept as accepted no-ops for backward compatibility with older scripts.
    parser.add_argument(
        "--resolve-past-cap",
        dest="ignore_loop_limit",
        action="store_true",
        help=argparse.SUPPRESS,
    )
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
    parser.add_argument(
        "--cycle-summary",
        action="store_true",
        help=(
            "Print the [loop] Summary block for the current cycle from accumulated "
            "state, WITHOUT writing a record or clearing run tracking. Safe to call "
            "every cycle (unlike --record-run, which is terminal and destructive)."
        ),
    )
    parser.add_argument(
        "--auto-snapshot",
        action="store_true",
        help=(
            "Render the lean auto-snapshot receipt instead of the full one. For "
            "the Stop-hook backstop: shows only GitHub-observable state (threads "
            "seen/resolved/open, cycles) and omits agent-only fields (fixed "
            "count, verification, outcome) the hook cannot know."
        ),
    )
    parser.add_argument("--fixed-count", type=int, default=0, help="Agent-claimed fixes this run.")
    parser.add_argument(
        "--fixed-finding",
        action="append",
        default=[],
        help=(
            "Finding fingerprint the agent fixed locally. Repeatable; stored in "
            "run tracking and used for terminal classification."
        ),
    )
    parser.add_argument(
        "--fixed-path",
        action="append",
        default=[],
        help=(
            "Path the agent fixed locally. Repeatable; stored in run tracking and "
            "used as path-level fixed/change evidence."
        ),
    )
    parser.add_argument(
        "--changed-base",
        default=None,
        help="Optional git base ref for changed-file evidence (`git diff --name-only base..head`).",
    )
    parser.add_argument(
        "--changed-head",
        default=None,
        help="Optional git head ref for changed-file evidence (`git diff --name-only base..head`).",
    )
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
        "--gemini-confirmed",
        dest="gemini_confirmed",
        action="store_true",
        default=True,
        help="The final Gemini wait/re-review completed. Default for legacy compatibility.",
    )
    parser.add_argument(
        "--gemini-unconfirmed",
        "--no-gemini-confirmed",
        dest="gemini_confirmed",
        action="store_false",
        help="The final Gemini wait timed out or otherwise did not confirm the latest fixes.",
    )
    parser.add_argument(
        "--semantic-risk",
        action="append",
        default=[],
        help=(
            "Manual/heuristic semantic risk note for this cycle. Repeatable; "
            "rendered only in cycle/run summary output."
        ),
    )
    parser.add_argument(
        "--record-cycle",
        action="store_true",
        help=(
            "Append one active-cycle timing entry to the run accumulator and exit. "
            "Pass --cycle-started-at (when active work for the cycle began), "
            "--finding-count, and --cycle-outcome. Measures active work only; waits "
            "between cycles are excluded. Does not touch runs.jsonl."
        ),
    )
    parser.add_argument(
        "--cycle-started-at",
        default=None,
        help="ISO-8601 UTC timestamp (YYYY-MM-DDThh:mm:ssZ) when this cycle's active work began.",
    )
    parser.add_argument(
        "--finding-count",
        type=int,
        default=0,
        help="Number of actionable findings handled in this cycle.",
    )
    parser.add_argument(
        "--cycle-outcome",
        default="continued",
        help="Cycle outcome: 'continued' for a non-terminal cycle, else the terminal label.",
    )
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
    color_enabled = colors_enabled(no_color=args.no_color)

    if args.profile_intro or args.planned_verification:
        if not args.repo:
            print("error: --profile-intro/--planned-verification require --repo OWNER/REPO.", file=sys.stderr)
            return 1
        profile = load_profile_for_repo(args.repo)
        blocks = []
        if args.profile_intro:
            blocks.append(metrics.format_profile_intro_block(profile, args.repo))
        if args.planned_verification:
            blocks.append(metrics.format_planned_verification_block(profile))
        print(color_loop_block("\n".join(blocks), enabled=color_enabled))
        return 0

    if args.stats:
        try:
            if args.pr:
                pr = resolve_pr(args.pr)
                repo_full = f"{pr.owner}/{pr.repo}"
            elif args.stats_all_repos:
                repo_full = "(all repos)"
            else:
                repo_full = resolve_current_repo()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        records, skipped = metrics.load_records()
        selected = select_stats_records(
            records, repo=repo_full, window=args.stats_window, all_repos=args.stats_all_repos
        )
        agg = metrics.aggregate(selected)
        if args.format == "json":
            print(json.dumps({"repo": repo_full, "stats": agg, "skipped": skipped}, indent=2, sort_keys=True))
        else:
            print(metrics.format_stats(repo_full, agg, skipped=skipped))
        return 0

    if args.record_cycle:
        if not args.cycle_started_at:
            print("error: --record-cycle requires --cycle-started-at.", file=sys.stderr)
            return 1
        try:
            pr = resolve_pr(args.pr)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        try:
            cycle = record_cycle(
                pr,
                started_at=args.cycle_started_at,
                finding_count=args.finding_count,
                outcome=args.cycle_outcome,
            )
        except OSError as exc:
            # Metrics state I/O is best-effort: warn and exit 0 so a failed
            # write never breaks the loop. Return here (do not fall through to
            # the success print, which would reference an unbound `cycle`).
            print(f"warning: could not record cycle timing: {exc}", file=sys.stderr)
            return 0
        print(
            color_loop_block(
                f"[loop] recorded cycle: {metrics.format_duration(cycle['duration_seconds'])} "
                f"active ({cycle['finding_count']} finding(s), outcome={cycle['outcome']}).",
                enabled=color_enabled,
            )
        )
        return 0

    try:
        prefs = load_preferences_with_fallback()
        args.max_rereview_requests = effective_rereview_limit(
            args.max_rereview_requests, prefs
        )
        pr = resolve_pr(args.pr)
        if args.fixed_finding or args.fixed_path:
            try:
                accumulate_fixed_markers(
                    pr,
                    fingerprints=args.fixed_finding,
                    paths=args.fixed_path,
                )
            except OSError as exc:
                print(f"warning: could not persist fixed markers: {exc}", file=sys.stderr)
        if args.wait:
            pull_request = wait_for_stable_review(
                pr,
                author=args.author,
                timeout_seconds=args.timeout,
                interval_seconds=args.interval,
                quiet_seconds=args.quiet_period,
                after_iso=args.after,
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
        _limit_reached = len(rereviews) >= args.max_rereview_requests
        if args.resolve_outdated:
            resolved_outdated = resolve_outdated_threads(
                pull_request, args.author, dry_run=args.dry_run
            )
            if resolved_outdated:
                tag = "[dry-run] would resolve" if args.dry_run else "Resolved"
                print(f"{tag} {resolved_outdated} outdated {args.author} thread(s).", file=sys.stderr)
                if not args.dry_run:
                    pull_request = fetch_threads(pr)
        if args.resolve_addressed_by_reply:
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
        if not args.record_run and not args.stats:
            try:
                update_run_tracking(pr, [(t["id"], t.get("path", "")) for t in threads])
            except OSError as exc:
                print(f"warning: could not update run tracking: {exc}", file=sys.stderr)
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

        # ---- No-progress detection ------------------------------------------
        # Compare the actionable thread fingerprint to the previous cycle's.
        # Only runs during a real fetch (not --cycle-summary / --record-run /
        # --stats), so the comparison reflects actual loop cycles, not
        # mid-cycle read-only calls.
        no_progress_flag = False
        if not args.record_run and not args.cycle_summary and not args.stats:
            current_fp = thread_fingerprint(threads)
            try:
                no_progress_flag = detect_no_progress(pr, current_fp)
            except OSError as exc:
                print(f"warning: could not check progress: {exc}", file=sys.stderr)
            # Snapshot prior-cycle finding fingerprints so the receipt can mark
            # carried-over findings. Real-fetch only, once per cycle.
            try:
                track_finding_fingerprints(
                    pr, {finding_fingerprint(t) for t in threads if isinstance(t, dict)}
                )
            except OSError as exc:
                print(f"warning: could not track finding fingerprints: {exc}", file=sys.stderr)

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
            effective_phase = resolve_judge_phase(
                args.judge_phase, record_run=bool(args.record_run)
            )
            judge_results = {}
            if should_judge_run(mode=effective_mode, phase=effective_phase):
                client = JudgeClient(model=args.judge_model or prefs["judge_model"])
                ready, skip_reason = client.is_ready()
                if not ready:
                    judge_status = {
                        "ran": False,
                        "mode": effective_mode,
                        "phase": effective_phase,
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
                        "phase": effective_phase,
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
                    "phase": effective_phase,
                    "skip_reason": (
                        f"mode={effective_mode!r} + phase={effective_phase!r} → no-op"
                    ),
                }
        # --------------------------------------------------------------------

        # Persist this cycle's verdicts into the run accumulator so the terminal
        # --record-run can report the eval even after findings are resolved.
        if not args.record_run and not args.stats and judge_results:
            try:
                accumulate_judge_results(pr, judge_results)
            except OSError as exc:
                print(f"warning: could not persist judge results: {exc}", file=sys.stderr)

        rereviews = rereview_requests(pull_request, agent_login)
        if len(rereviews) >= args.max_rereview_requests:
            print(
                f"warning: {len(rereviews)} Gemini re-review request(s) already exist; "
                f"the configured loop cap is {args.max_rereview_requests}.",
                file=sys.stderr,
            )
        if args.record_run or args.cycle_summary:
            try:
                # --record-run owns accumulation here because it skips the
                # per-fetch tracking above; --cycle-summary just reads what that
                # path already accumulated this cycle.
                if args.record_run:
                    update_run_tracking(pr, [(t["id"], t.get("path", "")) for t in threads])
                run = read_run_tracking(pr)
            except OSError as exc:
                print(f"warning: could not update or read run tracking: {exc}", file=sys.stderr)
                run = {}
            baseline_ids = set(run.get("finding_ids", []))
            finding_paths = run.get("finding_paths", [])
            current_actionable_ids = {t["id"] for t in threads}
            addressed_by_reply_ids = {
                t["id"] for t in addressed_by_reply_threads(pull_request, args.author)
            }
            # Merge verdicts accumulated across cycles with this invocation's,
            # so the record reflects the whole run even when the terminal pass
            # has no live findings to re-judge. Current invocation supersedes.
            if not isinstance(run, dict):
                run = {}
            accumulated_results = run.get("judge_results")
            if not isinstance(accumulated_results, dict):
                accumulated_results = {}
            merged_judge_results = merge_judge_results(accumulated_results, judge_results)
            judge_ran = bool(run.get("judge_ran")) or bool(judge_status.get("ran"))
            cap_reached = len(rereviews) >= args.max_rereview_requests
            fixed_markers = read_fixed_markers(pr)
            changed_paths = changed_files_in_range(args.changed_base, args.changed_head)
            remaining_states = classify_remaining_finding_states(
                threads,
                fixed_fingerprints=fixed_markers["fingerprints"],
                fixed_paths=fixed_markers["paths"],
                changed_paths=changed_paths,
                prior_fingerprints=prior_finding_fingerprints(pr),
                judge_results=merged_judge_results,
                cap_reached=cap_reached,
            )
            likely_fixed_remaining = count_likely_fixed_remaining(remaining_states)
            outcome = args.outcome or _derive_outcome(
                len(current_actionable_ids),
                args.verification,
                cap_reached,
                gemini_confirmed=args.gemini_confirmed,
                likely_fixed_remaining=likely_fixed_remaining,
            )
            derived = derive_record_fields(
                baseline_ids=baseline_ids,
                current_actionable_ids=current_actionable_ids,
                addressed_by_reply_ids=addressed_by_reply_ids,
                outcome=outcome,
                judge_ran=judge_ran,
                judge_results=merged_judge_results,
            )
            terminal_breakdown = {
                "confirmed_fixed_outdated": derived["observed_fixed_count"],
                "fixed_pending_confirmation": likely_fixed_remaining,
                "remaining_valid_actionable": max(
                    0,
                    derived["remaining_actionable"]
                    - derived["needs_human"]
                    - likely_fixed_remaining,
                ),
                "needs_human": derived["needs_human"],
            }
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
                judge=metrics.build_judge_block(judge_ran, merged_judge_results),
                cycles=run.get("cycles", []),
                terminal_breakdown=terminal_breakdown,
                **derived,
            )
            # --cycle-summary is read-only: print the block but never append a
            # record or clear the accumulator, so it is safe to call every cycle.
            if args.record_run:
                try:
                    metrics.append_record(record)
                except OSError as exc:
                    print(f"warning: could not record run metrics: {exc}", file=sys.stderr)
                else:
                    try:
                        clear_run_tracking(pr)
                    except OSError as exc:
                        print(f"warning: could not clear run tracking: {exc}", file=sys.stderr)
            else:
                # --cycle-summary leaves the accumulator intact but stamps that a
                # summary covering the current run state was emitted, so the
                # Stop-hook backstop won't duplicate it.
                try:
                    stamp_summary_emitted(pr)
                except OSError as exc:
                    print(f"warning: could not stamp summary state: {exc}", file=sys.stderr)
            if args.auto_snapshot:
                print(color_loop_block(metrics.format_auto_snapshot(record), enabled=color_enabled))
            else:
                print(
                    color_loop_block(
                        metrics.format_run_summary(record, terminal=bool(args.record_run)),
                        enabled=color_enabled,
                    )
                )
                if judge_eval_requested(judge_status):
                    print(
                        color_loop_block(
                            metrics.format_judge_skip(judge_status.get("skip_reason", "")),
                            enabled=color_enabled,
                        )
                    )
                # Surface the detected test toolset and the deterministic finding
                # list inline, so neither stays buried in the collapsed profile
                # JSON / fetch output. Carried-over findings are marked using the
                # prior-cycle fingerprint snapshot.
                semantic_risk_block = metrics.format_semantic_risk_block(args.semantic_risk)
                if semantic_risk_block:
                    print(color_loop_block(semantic_risk_block, enabled=color_enabled))
                suite_block = metrics.format_suite_block(verification_details)
                if suite_block:
                    print(suite_block)
                prior_fps = prior_finding_fingerprints(pr)
                findings_view = []
                for thread in threads:
                    comments = _iter_comments(thread)
                    line_val = thread.get("line")
                    findings_view.append({
                        "path": thread.get("path"),
                        "line": line_val if line_val is not None else thread.get("originalLine"),
                        "severity": thread_severity(thread),
                        "url": comments[0].get("url") if comments and isinstance(comments[0], dict) else None,
                        "carried": finding_fingerprint(thread) in prior_fps,
                    })
                findings_block = metrics.format_findings_block(findings_view)
                if findings_block:
                    print(findings_block)
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
                        "noProgressDetected": no_progress_flag,
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
        if judge_status.get("ran") and judge_results:
            print(
                color_loop_block(
                    format_judge_thread_table(
                        threads, judge_results, judge_status.get("phase", "cycle")
                    ),
                    enabled=color_enabled,
                ),
            )
        elif judge_eval_requested(judge_status):
            print(
                color_loop_block(
                    metrics.format_judge_skip(judge_status.get("skip_reason", "")),
                    enabled=color_enabled,
                )
            )
        if no_progress_flag:
            print(
                color_loop_block(
                    "[loop] no_progress: actionable thread set is unchanged since the previous "
                    "cycle — no fix landed. Stop with: "
                    "--record-run --outcome no_progress "
                    "--outcome-reason 'no code change resolved any open thread'",
                    enabled=color_enabled,
                )
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

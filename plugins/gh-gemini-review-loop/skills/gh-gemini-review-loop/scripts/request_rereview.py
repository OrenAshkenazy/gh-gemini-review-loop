#!/usr/bin/env python3
"""Post an AI reviewer re-review request comment for a PR."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import reviewer_resolver


DEFAULT_REVIEWER_MENTION = "@gemini-code-assist"
DEFAULT_REVIEWER_LOGIN = "gemini-code-assist"


def build_default_phrase(reviewer_mention: str = DEFAULT_REVIEWER_MENTION) -> str:
    reviewer_mention = reviewer_mention.strip() or DEFAULT_REVIEWER_MENTION
    if not reviewer_mention.startswith("@"):
        reviewer_mention = f"@{reviewer_mention}"
    return f"{reviewer_mention} please review the latest changes."


DEFAULT_PHRASE = build_default_phrase()


def no_safe_trigger_payload(reviewer_login: str) -> dict[str, Any]:
    reviewer = reviewer_login.strip() or "unknown reviewer"
    return {
        "status": "no_safe_trigger",
        "posted": False,
        "reviewer": reviewer,
        "message": (
            f"No safe re-review trigger known for `{reviewer}`; "
            "pass --review-trigger-mention to enable re-review requests."
        ),
    }


def state_path() -> Path:
    base = os.environ.get("GGRL_STATE_DIR") or os.path.expanduser(
        "~/.config/gh-gemini-review-loop"
    )
    return Path(base) / "state.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def read_persisted_reviewer(repo: str, pr: int) -> dict[str, Any] | None:
    entry = load_state().get(f"{repo}#{pr}")
    reviewer = entry.get("reviewer") if isinstance(entry, dict) else None
    if not isinstance(reviewer, dict):
        return None
    login = reviewer.get("login")
    if not isinstance(login, str) or not login:
        return None
    return reviewer


def resolve_review_trigger(
    *,
    repo: str,
    pr: int,
    reviewer_login: str | None,
    reviewer_mention: str | None,
) -> tuple[str | None, str]:
    if reviewer_mention:
        login = reviewer_login or reviewer_mention.lstrip("@")
        return reviewer_mention, login

    persisted = read_persisted_reviewer(repo, pr)
    if persisted:
        login = str(persisted["login"])
        trigger = persisted.get("review_trigger")
        if isinstance(trigger, str) and trigger:
            return trigger, login
        return reviewer_resolver.trigger_for(login), login

    if reviewer_login:
        return reviewer_resolver.trigger_for(reviewer_login), reviewer_login

    return DEFAULT_REVIEWER_MENTION, DEFAULT_REVIEWER_LOGIN


def parse_repo(value: str) -> tuple[str, str]:
    """Validate and split an ``OWNER/REPO`` value."""
    if not isinstance(value, str):
        raise ValueError("--repo must be a string")
    if value.strip() != value or value.count("/") != 1:
        raise ValueError("--repo must be in OWNER/REPO format")
    owner, repo = value.split("/", 1)
    if not owner or not repo or any(ch.isspace() for ch in value):
        raise ValueError("--repo must be in OWNER/REPO format")
    return owner, repo


def positive_pr(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("--pr must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--pr must be a positive integer")
    return parsed


def post_rereview(
    repo: str,
    pr: int,
    phrase: str,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    """Post the top-level PR comment and return the normalized result payload."""
    owner, repo_name = parse_repo(repo)
    if not isinstance(pr, int) or pr <= 0:
        raise ValueError("--pr must be a positive integer")

    cmd = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/issues/{pr}/comments",
        "--method",
        "POST",
        "--raw-field",
        f"body={phrase}",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run gh api: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"gh api failed{suffix}")

    try:
        response = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh api returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise RuntimeError("gh api returned non-object JSON")

    created_at = response.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise RuntimeError("gh api response missing created_at")

    return {
        "created_at": created_at,
        "repo": repo,
        "pr": pr,
        "phrase": phrase,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Request an AI reviewer re-review by posting a PR comment."
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in OWNER/REPO format.")
    parser.add_argument("--pr", required=True, type=positive_pr, help="Positive PR number.")
    parser.add_argument(
        "--phrase",
        default=None,
        help=f"Review request phrase. Default: {DEFAULT_PHRASE!r}",
    )
    parser.add_argument(
        "--reviewer-mention",
        "--review-trigger-mention",
        dest="reviewer_mention",
        default=None,
        help=(
            "Reviewer mention used when --phrase is omitted. "
            f"Default: {DEFAULT_REVIEWER_MENTION!r}."
        ),
    )
    parser.add_argument(
        "--reviewer-login",
        default=None,
        help="Reviewer login used in controlled stop messages.",
    )
    parser.add_argument(
        "--no-safe-trigger",
        action="store_true",
        help="Do not post; emit a controlled no_safe_trigger result instead.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Print JSON only on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 2
        return code

    try:
        parse_repo(args.repo)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.no_safe_trigger:
        reviewer_login = args.reviewer_login or (
            args.reviewer_mention.lstrip("@") if args.reviewer_mention else "unknown reviewer"
        )
        payload = no_safe_trigger_payload(reviewer_login)
        if args.json_output:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"[loop] {payload['message']}")
        return 0

    try:
        if args.phrase is not None:
            phrase = args.phrase
        else:
            trigger, reviewer_login = resolve_review_trigger(
                repo=args.repo,
                pr=args.pr,
                reviewer_login=args.reviewer_login,
                reviewer_mention=args.reviewer_mention,
            )
            if trigger is None:
                payload = no_safe_trigger_payload(reviewer_login)
                if args.json_output:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"[loop] {payload['message']}")
                return 0
            phrase = build_default_phrase(trigger)
        payload = post_rereview(args.repo, args.pr, phrase)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"[loop] Re-review requested at {payload['created_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

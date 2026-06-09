#!/usr/bin/env python3
"""Post a Gemini Code Assist re-review request comment for a PR."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any


DEFAULT_PHRASE = "@gemini-code-assist please review the latest changes."


def parse_repo(value: str) -> tuple[str, str]:
    """Validate and split an ``OWNER/REPO`` value."""
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
        "--field",
        f"body={phrase}",
    ]
    try:
        result = runner(cmd, capture_output=True, text=True, check=False)
    except (OSError, ValueError) as exc:
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
        description="Request a Gemini Code Assist re-review by posting a PR comment."
    )
    parser.add_argument("--repo", required=True, help="GitHub repository in OWNER/REPO format.")
    parser.add_argument("--pr", required=True, type=positive_pr, help="Positive PR number.")
    parser.add_argument(
        "--phrase",
        default=DEFAULT_PHRASE,
        help=f"Review request phrase. Default: {DEFAULT_PHRASE!r}",
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

    try:
        payload = post_rereview(args.repo, args.pr, args.phrase)
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

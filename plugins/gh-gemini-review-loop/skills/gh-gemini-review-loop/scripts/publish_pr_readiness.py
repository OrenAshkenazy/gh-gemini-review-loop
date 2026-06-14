#!/usr/bin/env python3
"""Publish (or update) the PR Readiness Card as a single GitHub PR comment.

Uses a stable hidden HTML marker so repeated runs update the *same* comment
instead of spamming the PR with duplicates. The GitHub API surface is isolated
behind a small client object so the publish logic is fully unit-testable
without the network, and so this script only ever mutates a PR when explicitly
invoked.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from pr_architecture_risk import parse_pr
from render_pr_readiness import READINESS_MARKER

MARKER = READINESS_MARKER


def build_comment_body(card_markdown: str) -> str:
    """Ensure the readiness card carries exactly one stable hidden marker."""
    card = card_markdown.rstrip()
    if MARKER in card:
        return card + "\n"
    return f"{MARKER}\n{card}\n"


class GitHubClient:
    """Thin wrapper over ``gh api`` for PR issue comments."""

    def __init__(self, runner: Any = subprocess.run) -> None:
        self._runner = runner

    def _api(self, args: list[str], parse: bool = True) -> Any:
        try:
            proc = self._runner(
                ["gh", "api", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"gh api failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"gh api failed: {detail}" if detail else "gh api failed")
        if not parse:
            return None
        try:
            return json.loads(proc.stdout or "null")
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh api returned invalid JSON") from exc

    def list_comments(self, repo: str, pr: int) -> list[dict[str, Any]]:
        payload = self._api(
            [f"repos/{repo}/issues/{pr}/comments", "--paginate"]
        )
        return payload if isinstance(payload, list) else []

    def create_comment(self, repo: str, pr: int, body: str) -> dict[str, Any]:
        return self._api(
            [
                f"repos/{repo}/issues/{pr}/comments",
                "--method",
                "POST",
                "--raw-field",
                f"body={body}",
            ]
        )

    def update_comment(self, repo: str, comment_id: int, body: str) -> dict[str, Any]:
        return self._api(
            [
                f"repos/{repo}/issues/comments/{comment_id}",
                "--method",
                "PATCH",
                "--raw-field",
                f"body={body}",
            ]
        )


def _find_marked_comment(comments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for comment in comments:
        if isinstance(comment, dict) and MARKER in (comment.get("body") or ""):
            return comment
    return None


def publish(repo: str, pr: int, card_markdown: str, github: Any = None) -> dict[str, Any]:
    """Create or update the single marked readiness comment on a PR."""
    github = github or GitHubClient()
    body = build_comment_body(card_markdown)

    existing = _find_marked_comment(github.list_comments(repo, pr))
    if existing is not None:
        response = github.update_comment(repo, existing["id"], body)
        action = "updated"
    else:
        response = github.create_comment(repo, pr, body)
        action = "created"

    response = response or {}
    return {
        "action": action,
        "comment_id": response.get("id"),
        "html_url": response.get("html_url"),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Post or update the PR Readiness Card as a single PR comment."
    )
    parser.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    parser.add_argument(
        "--readiness", required=True, help="Path to the rendered readiness Markdown."
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Print JSON result on stdout."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        repo, number = parse_pr(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        card = Path(args.readiness).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError) as exc:
        print(f"error: could not read readiness file: {exc}", file=sys.stderr)
        return 2

    try:
        result = publish(repo, number, card)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        where = result.get("html_url") or f"PR #{number}"
        print(f"Readiness comment {result['action']}: {where}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

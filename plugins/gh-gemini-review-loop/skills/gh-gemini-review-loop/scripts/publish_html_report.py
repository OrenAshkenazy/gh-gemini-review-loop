#!/usr/bin/env python3
"""Publish the static readiness HTML report to GitHub Pages and link it.

Pushes the rendered report to the app repo's ``gh-pages`` branch at
``pr-<N>/index.html`` via the GitHub Contents API (idempotent: create or update
in place), best-effort enables Pages, and returns the public Pages URL. All
GitHub calls go through an injectable runner, so this is unit-testable and never
touches the network in tests.
"""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path
from typing import Any, Callable

from publish_infra_pr import default_gh_runner

GitHubRunner = Callable[[list[str]], Any]

_BRANCH = "gh-pages"


def pages_url(repo: str, pr: int) -> str:
    """The public GitHub Pages URL for a PR's report directory."""
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/pr-{pr}/"


def _report_path(pr: int) -> str:
    return f"pr-{pr}/index.html"


def _branch_head_sha(repo: str, branch: str, runner: GitHubRunner) -> str | None:
    try:
        ref = runner([f"repos/{repo}/git/ref/heads/{branch}"])
    except RuntimeError:
        return None
    obj = ref.get("object") if isinstance(ref, dict) else None
    if isinstance(obj, dict) and isinstance(obj.get("sha"), str):
        return obj["sha"]
    return None


def _default_branch(repo: str, runner: GitHubRunner) -> str:
    info = runner([f"repos/{repo}"])
    if isinstance(info, dict) and isinstance(info.get("default_branch"), str):
        return info["default_branch"]
    return "main"


def _ensure_branch(repo: str, branch: str, runner: GitHubRunner) -> None:
    """Create *branch* from the default branch's HEAD when it does not exist."""
    if _branch_head_sha(repo, branch, runner) is not None:
        return
    base = _default_branch(repo, runner)
    sha = _branch_head_sha(repo, base, runner)
    if sha is None:
        raise RuntimeError(f"cannot resolve {repo}@{base} to seed the {branch} branch")
    runner([
        "--method", "POST", f"repos/{repo}/git/refs",
        "-f", f"ref=refs/heads/{branch}",
        "-f", f"sha={sha}",
    ])


def _existing_file_sha(repo: str, path: str, branch: str, runner: GitHubRunner) -> str | None:
    try:
        info = runner([f"repos/{repo}/contents/{path}?ref={branch}"])
    except RuntimeError:
        return None
    if isinstance(info, dict) and isinstance(info.get("sha"), str):
        return info["sha"]
    return None


def _put_file(
    repo: str, path: str, content_b64: str, branch: str, runner: GitHubRunner, message: str
) -> None:
    """Create or update *path* on *branch* (sha-aware, idempotent)."""
    sha = _existing_file_sha(repo, path, branch, runner)
    args = [
        "--method", "PUT", f"repos/{repo}/contents/{path}",
        "-f", f"message={message}",
        "-f", f"content={content_b64}",
        "-f", f"branch={branch}",
    ]
    if sha:
        args += ["-f", f"sha={sha}"]
    runner(args)


def _ensure_nojekyll(repo: str, branch: str, runner: GitHubRunner) -> None:
    """A ``.nojekyll`` marker disables Jekyll so the static report serves as-is
    (the branch is seeded from the default branch, whose source would otherwise
    make the Jekyll build error)."""
    if _existing_file_sha(repo, ".nojekyll", branch, runner) is None:
        _put_file(repo, ".nojekyll", "", branch, runner,
                  "mergeproof: disable Jekyll for static report serving")


def _enable_pages(repo: str, branch: str, runner: GitHubRunner) -> bool:
    # Best-effort: enabling Pages can fail for reasons outside our control
    # (already enabled, missing scope, or — most commonly for demos — a private
    # repo on a plan without private Pages). Never fail the run; warn instead so
    # the linked URL isn't a silent 404.
    try:
        runner([
            "--method", "POST", f"repos/{repo}/pages",
            "-f", f"source[branch]={branch}",
            "-f", "source[path]=/",
        ])
        return True
    except RuntimeError as exc:
        if "already enabled" in str(exc).lower() or "http 409" in str(exc).lower():
            return True
        print(
            f"[mergeproof] note: could not enable GitHub Pages for {repo}: {exc}\n"
            f"  The report was pushed to the '{branch}' branch, but the linked URL "
            f"won't render until Pages is enabled. Free-plan Pages requires a public "
            f"repo (Settings -> Pages, source: {branch} / root).",
            file=sys.stderr,
        )
        return False


def publish_report(
    repo: str,
    pr: int,
    html: str,
    *,
    runner: GitHubRunner = default_gh_runner,
    message: str | None = None,
) -> str:
    """Push *html* to gh-pages at ``pr-<N>/index.html``; return the Pages URL."""
    _ensure_branch(repo, _BRANCH, runner)
    _ensure_nojekyll(repo, _BRANCH, runner)
    content = base64.b64encode(html.encode("utf-8")).decode("ascii")
    _put_file(
        repo,
        _report_path(pr),
        content,
        _BRANCH,
        runner,
        message or f"mergeproof: readiness report for PR #{pr}",
    )
    _enable_pages(repo, _BRANCH, runner)
    return pages_url(repo, pr)


def with_report_link(markdown: str, url: str) -> str:
    """Insert a prominent report link just under the readiness card title."""
    link = f"**\U0001f4ca [Open the full readiness report]({url})**"
    lines = markdown.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## MergeProof PR Readiness"):
            lines[i + 1 : i + 1] = ["", link]
            return "\n".join(lines)
    return markdown.rstrip() + "\n\n" + link + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish the readiness HTML report to GitHub Pages."
    )
    parser.add_argument("--repo", required=True, help="App repo OWNER/REPO.")
    parser.add_argument("--pr", type=int, required=True, help="PR number.")
    parser.add_argument("--html", required=True, help="Path to the rendered HTML report.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        html = Path(args.html).read_text(encoding="utf-8")
        url = publish_report(args.repo, args.pr, html)
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

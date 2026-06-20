#!/usr/bin/env python3
"""Stage a generated infra change as a branch + prefilled PR-create deep-link.

Deterministic branch naming and URL construction. The actual git/gh work is
isolated behind an injectable *runner* so it is fully unit-testable, and skipped
entirely in ``dry_run`` (used by the offline demo, where the infra repo may be
fictional). Returns the ``infra_pr`` block attached to a matched obligation.
"""

from __future__ import annotations

import re
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlencode

Runner = Callable[[list[str]], str]
GitHubRunner = Callable[[list[str]], Any]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def branch_name(obligation_type: str, inputs: dict[str, str]) -> str:
    primary = ""
    # env_name precedes service so fanned-out env obligations (runtime_config /
    # queue_topic) get one branch per variable instead of colliding on service.
    for key in ("worker_name", "secret_name", "topic", "env_name", "service"):
        if inputs.get(key):
            primary = inputs[key]
            break
    slug = _slug(primary)
    return f"mergeproof/{obligation_type}-{slug}" if slug else f"mergeproof/{obligation_type}"


def compare_url(repo: str, base: str, branch: str, title: str, body: str) -> str:
    query = urlencode({"expand": "1", "title": title, "body": body}, quote_via=quote_plus)
    return f"https://github.com/{repo}/compare/{base}...{branch}?{query}"


def default_runner(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "git failed").strip())
    return proc.stdout


def default_gh_runner(args: list[str]) -> Any:
    proc = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "gh api failed").strip())
    try:
        return json.loads(proc.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh api returned invalid JSON") from exc


def open_or_create_pr(
    repo: str,
    base: str,
    branch: str,
    title: str,
    body: str,
    runner: GitHubRunner = default_gh_runner,
) -> dict[str, Any]:
    owner = repo.split("/", 1)[0]
    query = urlencode(
        {"head": f"{owner}:{branch}", "base": base, "state": "open"},
        quote_via=quote_plus,
    )
    existing = runner([f"repos/{repo}/pulls?{query}"])
    if isinstance(existing, list) and existing:
        pr = existing[0]
        return {
            "action": "existing",
            "number": pr.get("number"),
            "html_url": pr.get("html_url"),
        }

    created = runner(
        [
            f"repos/{repo}/pulls",
            "--method",
            "POST",
            "--raw-field",
            f"title={title}",
            "--raw-field",
            f"body={body}",
            "--raw-field",
            f"head={branch}",
            "--raw-field",
            f"base={base}",
        ]
    )
    created = created if isinstance(created, dict) else {}
    return {
        "action": "created",
        "number": created.get("number"),
        "html_url": created.get("html_url"),
    }


def stage_branch(
    repo: str,
    base: str,
    branch: str,
    files: dict[str, str],
    commit_message: str,
    dry_run: bool = True,
    runner: Runner = default_runner,
) -> dict[str, Any]:
    """Stage *files* on *branch*. In dry_run, perform no git calls."""
    generated = sorted(files)
    if dry_run:
        return {"repo": repo, "base": base, "branch": branch, "pushed": False, "generated_files": generated}
    with tempfile.TemporaryDirectory() as tmp:
        runner(["clone", "--depth", "1", "--branch", base, f"https://github.com/{repo}.git", tmp])
        runner(["-C", tmp, "checkout", "-B", branch])
        lease = "--force-with-lease"
        try:
            runner(["-C", tmp, "fetch", "origin", f"{branch}:refs/remotes/origin/{branch}"])
            expected = runner(["-C", tmp, "rev-parse", f"refs/remotes/origin/{branch}"]).strip()
            if expected:
                lease = f"--force-with-lease=refs/heads/{branch}:{expected}"
        except RuntimeError:
            pass  # First run: the generated branch does not exist yet.
        for rel, content in files.items():
            dest = Path(tmp) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            runner(["-C", tmp, "add", rel])
        runner(["-C", tmp, "commit", "-m", commit_message])
        runner(["-C", tmp, "push", lease, "origin", branch])
    return {"repo": repo, "base": base, "branch": branch, "pushed": True, "generated_files": generated}

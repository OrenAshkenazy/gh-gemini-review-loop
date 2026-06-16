#!/usr/bin/env python3
"""Stage a generated infra change as a branch + prefilled PR-create deep-link.

Deterministic branch naming and URL construction. The actual git/gh work is
isolated behind an injectable *runner* so it is fully unit-testable, and skipped
entirely in ``dry_run`` (used by the offline demo, where the infra repo may be
fictional). Returns the ``infra_pr`` block attached to a matched obligation.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus, urlencode

Runner = Callable[[list[str]], str]


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-")


def branch_name(obligation_type: str, inputs: dict[str, str]) -> str:
    primary = ""
    for key in ("worker_name", "secret_name", "topic", "service"):
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
        for rel, content in files.items():
            dest = Path(tmp) / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            runner(["-C", tmp, "add", rel])
        runner(["-C", tmp, "commit", "-m", commit_message])
        runner(["-C", tmp, "push", "--force-with-lease", "origin", branch])
    return {"repo": repo, "base": base, "branch": branch, "pushed": True, "generated_files": generated}

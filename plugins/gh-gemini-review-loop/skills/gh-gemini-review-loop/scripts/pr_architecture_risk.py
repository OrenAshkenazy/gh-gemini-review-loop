#!/usr/bin/env python3
"""Map a PR's changed files onto production-facing risk surfaces.

Given a static architecture context (see ``architecture_context.py``) and the
list of files a PR changes, this produces a deterministic set of production
risks. Changed files can come from a real GitHub PR (``--pr`` via ``gh api``)
or from a local newline-delimited file (``--changed-files``) for offline /
test use.

The risk model is intentionally conservative and advisory: it flags surfaces a
human should review before merge. It never blocks, mutates, or merges anything.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3}

# Each rule: (surface, label, regexes, severity_fn(context) -> str | None, reason_fn)
_PUBLIC_API_RE = (
    re.compile(r"(^|/)(api|routes|routers|handlers|controllers)(/|\.)", re.IGNORECASE),
)
_AUTH_RE = (
    re.compile(r"(auth|user|permission|token|password|security)", re.IGNORECASE),
)
_WORKER_RE = (
    re.compile(r"(worker|job|consumer|queue)", re.IGNORECASE),
)
_DB_RE = (
    re.compile(r"(^|/)(db|database|migration|migrations|repository|repositories|query|queries|dao)(/|\.|s/)", re.IGNORECASE),
    re.compile(r"(migration|repository|dao)", re.IGNORECASE),
)
_INFRA_RE = (
    re.compile(r"(^terraform/|^k8s/|^helm/|\.tf$|values\.ya?ml$)", re.IGNORECASE),
)


def _matches(patterns: tuple[re.Pattern[str], ...], path: str) -> bool:
    return any(p.search(path) for p in patterns)


def assess(context: dict[str, Any], changed_files: list[str]) -> dict[str, Any]:
    """Return production risks for *changed_files* under *context*.

    Deterministic: risks are ordered public_api, auth, async, database, infra,
    and files within each risk preserve input order.
    """
    context = context or {}
    exposure = (context.get("exposure") or "unknown").lower()
    has_queues = bool(context.get("queues"))
    has_datastores = bool(context.get("datastores"))

    risks: list[dict[str, Any]] = []

    api_files = [f for f in changed_files if _matches(_PUBLIC_API_RE, f)]
    if api_files:
        public = exposure == "public"
        risks.append(
            {
                "severity": "high" if public else "medium",
                "surface": "public_api",
                "reason": (
                    "PR touches API route/handler code in a public-facing service"
                    if public
                    else "PR touches API route/handler code"
                ),
                "files": api_files,
                "human_decision_required": True,
            }
        )

    auth_files = [f for f in changed_files if _matches(_AUTH_RE, f)]
    if auth_files:
        risks.append(
            {
                "severity": "high",
                "surface": "auth_security",
                "reason": "PR changes auth / user / security-related code",
                "files": auth_files,
                "human_decision_required": True,
            }
        )

    worker_files = [f for f in changed_files if _matches(_WORKER_RE, f)]
    if worker_files and has_queues:
        risks.append(
            {
                "severity": "medium",
                "surface": "async_processing",
                "reason": "PR changes worker/consumer code and the service uses queues",
                "files": worker_files,
                "human_decision_required": True,
            }
        )

    db_files = [f for f in changed_files if _matches(_DB_RE, f)]
    if db_files:
        is_migration = any(re.search(r"migration", f, re.IGNORECASE) for f in db_files)
        severity = "high" if (is_migration and has_datastores) else "medium"
        risks.append(
            {
                "severity": severity,
                "surface": "database_behavior",
                "reason": (
                    "PR changes a database migration against a live datastore"
                    if is_migration
                    else "PR changes database/query/repository code"
                ),
                "files": db_files,
                "human_decision_required": severity == "high",
            }
        )

    infra_files = [f for f in changed_files if _matches(_INFRA_RE, f)]
    if infra_files:
        risks.append(
            {
                "severity": "high" if exposure == "public" else "medium",
                "surface": "infrastructure",
                "reason": "PR changes infrastructure-as-code (Terraform / Kubernetes / Helm)",
                "files": infra_files,
                "human_decision_required": True,
            }
        )

    highest = "none"
    for risk in risks:
        if _SEVERITY_ORDER[risk["severity"]] > _SEVERITY_ORDER[highest]:
            highest = risk["severity"]

    return {
        "changed_files": list(changed_files),
        "production_risks": risks,
        "summary": {
            "highest_severity": highest,
            "human_decision_required": any(r["human_decision_required"] for r in risks),
            "risk_count": len(risks),
        },
    }


def read_changed_files(path: str | Path) -> list[str]:
    """Read a newline-delimited changed-files listing, skipping blanks."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _default_pr_runner(args: list[str]) -> Any:
    cmd = ["gh", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"gh api failed: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"gh api failed: {detail}" if detail else "gh api failed")
    try:
        return json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh api returned invalid JSON") from exc


def fetch_pr_changed_files(
    repo: str,
    pr: int,
    runner: Callable[[list[str]], Any] = _default_pr_runner,
) -> list[str]:
    """Return the list of filenames changed by a PR via the GitHub API.

    GitHub calls are isolated behind *runner* so this stays unit-testable.
    """
    args = ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate"]
    payload = runner(args)
    if not isinstance(payload, list):
        raise RuntimeError("unexpected gh api response (expected a list of files)")
    files: list[str] = []
    for entry in payload:
        if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
            files.append(entry["filename"])
    return files


_PR_URL_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")
_PR_SHORT_RE = re.compile(r"([^/\s]+/[^#\s]+)#(\d+)$")


def parse_pr(value: str) -> tuple[str, int]:
    """Parse a PR URL or ``OWNER/REPO#N`` shorthand into ``(repo, number)``."""
    url = _PR_URL_RE.match(value)
    if url:
        return f"{url.group(1)}/{url.group(2)}", int(url.group(3))
    short = _PR_SHORT_RE.match(value)
    if short:
        return short.group(1), int(short.group(2))
    raise ValueError(
        "--pr must be a PR URL like https://github.com/OWNER/REPO/pull/123 or OWNER/REPO#123"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Map a PR's changed files to production risk surfaces."
    )
    parser.add_argument(
        "--architecture-context",
        required=True,
        help="Path to architecture_context.py --json output.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pr", help="PR URL or OWNER/REPO#N (uses gh api).")
    source.add_argument("--changed-files", help="Path to a newline-delimited changed-file list.")
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
        return exc.code if isinstance(exc.code, int) else 2

    try:
        context = json.loads(Path(args.architecture_context).read_text(encoding="utf-8"))
        if not isinstance(context, dict):
            raise ValueError("architecture context must be a JSON object")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not read architecture context: {exc}", file=sys.stderr)
        return 2

    try:
        if args.changed_files:
            changed = read_changed_files(args.changed_files)
        else:
            repo, number = parse_pr(args.pr)
            changed = fetch_pr_changed_files(repo, number)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    result = assess(context, changed)

    if args.json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        summary = result["summary"]
        print(f"Changed files: {len(result['changed_files'])}")
        print(f"Production risks: {summary['risk_count']} "
              f"(highest: {summary['highest_severity']})")
        for risk in result["production_risks"]:
            print(f"  [{risk['severity'].upper()}] {risk['surface']}: {risk['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run the MergeProof readiness phase after the CR loop reaches terminal state."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from build_context_pack import SKIP_MESSAGE, build_pack
from metrics import runs_log_path
from pr_architecture_risk import _default_pr_runner, assess, fetch_pr_changed_files, parse_pr
from publish_pr_readiness import publish
from render_pr_readiness import build_readiness, render_markdown

Runner = Callable[[list[str]], Any]


def _load_loop_summary(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("loop summary must be a JSON object")
    return data


def _summary_from_record(record: dict[str, Any], pr_url: str) -> dict[str, Any]:
    verification_details = record.get("verification_details") or {}
    command = ""
    if isinstance(verification_details, dict):
        checks = verification_details.get("checks")
        if isinstance(checks, list) and checks and isinstance(checks[0], dict):
            command = str(checks[0].get("command") or "")
        if not command:
            command = str(verification_details.get("command") or verification_details.get("label") or "")
    outcome = str(record.get("outcome") or "")
    judge = record.get("judge") or {}
    verdicts = judge.get("verdicts") if isinstance(judge, dict) else {}
    false_positives = 0
    if isinstance(verdicts, dict):
        false_positives = int(verdicts.get("false_positive") or 0)
    return {
        "pr_url": pr_url,
        "fixed_count": int(record.get("fixed_count") or record.get("observed_fixed_count") or 0),
        "false_positives_skipped": false_positives,
        "verification": str(record.get("verification") or "unknown"),
        "verification_command": command,
        "rereview": "completed" if outcome == "clean" else "unknown",
        "cycles_used": record.get("cycles_used"),
        "cycles_total": record.get("cycle_cap"),
        "pending_confirmation": outcome == "fixed_pending_confirmation",
        "semantic_risk": bool(record.get("semantic_risk")),
    }


def load_latest_run_summary(runs_jsonl: str | Path, repo: str, pr_number: int) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for line in Path(runs_jsonl).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("repo") == repo and int(record.get("pr") or 0) == pr_number:
            latest = record
    if latest is None:
        raise ValueError(
            f"no terminal CR-loop run recorded for {repo}#{pr_number} in {runs_jsonl}. "
            "Run the Gemini review loop on this PR first (it records terminal state to "
            "runs.jsonl), or pass --loop-summary with an explicit summary JSON."
        )
    return _summary_from_record(latest, f"https://github.com/{repo}/pull/{pr_number}")


def run_readiness(
    app_repo: str,
    pr_number: int,
    loop_summary: dict[str, Any],
    *,
    runner: Runner = _default_pr_runner,
    trust_pr_config: bool = False,
    do_publish: bool = False,
    config_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    changed_files = fetch_pr_changed_files(app_repo, pr_number, runner=runner)
    pack = build_pack(
        app_repo,
        pr_number,
        changed_files,
        runner=runner,
        trust_pr_config=trust_pr_config,
        config_override=config_override,
    )
    if pack is None:
        print(SKIP_MESSAGE, file=sys.stderr)
        return {"status": "skipped", "reason": "mergeproof.yaml not found"}

    # An empty pack with failed sources means every declared infra source was
    # unreadable (e.g. 404/403). Fail loudly rather than emitting a hollow card.
    failed = pack["safety"].get("failed_sources") or []
    if failed and pack["provenance"].get("file_count", 0) == 0:
        detail = "\n".join(f"  - {f['repo']}: {f.get('error', 'unreadable')}" for f in failed)
        raise RuntimeError(
            "could not read any declared infra source; production context is empty.\n"
            + detail
            + "\nCheck the infra repo exists and your GitHub token has read access, "
            "or fix architecture_sources in mergeproof.yaml."
        )
    if failed:  # partial: some sources fetched, others failed — warn but proceed
        names = ", ".join(f["repo"] for f in failed)
        print(f"[mergeproof] warning: partial production context; unreadable sources: {names}",
              file=sys.stderr)

    risks = assess(pack["facts"], changed_files)
    readiness = build_readiness(loop_summary, pack, risks)
    markdown = render_markdown(readiness)
    result = {
        "status": "rendered",
        "readiness": readiness,
        "markdown": markdown,
        "pack": pack,
        "risks": risks,
    }
    if do_publish:
        result["publish"] = publish(app_repo, pr_number, markdown)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the MergeProof readiness phase for a terminal CR loop result."
    )
    parser.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    # The real input is runs.jsonl (auto-written by the CR loop). --loop-summary
    # is an optional explicit override (fixtures / manual replay).
    parser.add_argument(
        "--runs-jsonl",
        default=str(runs_log_path()),
        help="Path to runs.jsonl (the CR loop's terminal record). Defaults to the GGRL state log.",
    )
    parser.add_argument(
        "--loop-summary",
        help="Optional explicit terminal-summary JSON; overrides --runs-jsonl.",
    )
    parser.add_argument(
        "--mergeproof",
        help="Path to a local mergeproof.yaml/.json to use instead of the base-ref config.",
    )
    parser.add_argument("--trust-pr-config", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        repo, number = parse_pr(args.pr)
        if args.loop_summary:
            loop_summary = _load_loop_summary(args.loop_summary)
        else:
            loop_summary = load_latest_run_summary(args.runs_jsonl, repo, number)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not load terminal loop summary: {exc}", file=sys.stderr)
        return 2

    config_override = None
    if args.mergeproof:
        try:
            from mergeproof_config import load_config

            text = Path(args.mergeproof).read_text(encoding="utf-8")
            fmt = "json" if args.mergeproof.endswith(".json") else "yaml"
            config_override = load_config(text, fmt=fmt)
        except (OSError, ValueError) as exc:
            print(f"error: could not load --mergeproof config: {exc}", file=sys.stderr)
            return 2

    try:
        result = run_readiness(
            repo,
            number,
            loop_summary,
            trust_pr_config=args.trust_pr_config,
            do_publish=args.publish,
            config_override=config_override,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result["status"] == "skipped":
        return 0
    if args.json_output:
        print(json.dumps(result["readiness"], indent=2, sort_keys=True))
        return 0
    if args.markdown:
        print(result["markdown"], end="")
        return 0
    print(f"[mergeproof] readiness: {result['readiness']['status']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

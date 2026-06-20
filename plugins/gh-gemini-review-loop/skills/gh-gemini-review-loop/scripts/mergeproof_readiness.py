#!/usr/bin/env python3
"""Run the MergeProof readiness phase after the CR loop reaches terminal state."""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from build_context_pack import SKIP_MESSAGE, build_pack
from capability_pack import capabilities_from_config, load_pack
from fetch_infra_files import _decode_content
from metrics import runs_log_path
from detect_env_obligations import detect_env_obligations
from pr_obligations import detect_obligations
from pr_architecture_risk import _default_pr_runner, assess, fetch_pr_changed_file_entries, parse_pr
from publish_html_report import publish_report, with_report_link
from publish_infra_pr import default_gh_runner, default_runner
from publish_pr_readiness import publish
from render_demo_ui import render_html
from render_pr_readiness import build_readiness, render_markdown
from stage_obligations import stage_obligations

Runner = Callable[[list[str]], Any]


def _load_loop_summary(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
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


def _int_prefix(value: str) -> int:
    match = re.match(r"\s*(\d+)", value)
    return int(match.group(1)) if match else 0


def _summary_from_pr_body(body: str, pr_url: str) -> dict[str, Any]:
    rows: dict[str, str] = {}
    for line in body.splitlines():
        parts = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(parts) != 2 or parts[0].lower() in {"metric", "---"}:
            continue
        rows[parts[0].lower()] = parts[1]
    if not rows:
        raise ValueError("PR body does not contain a CR-loop metrics table")
    cycles = rows.get("cycles used", "")
    used, total = 0, None
    if "/" in cycles:
        left, _, right = cycles.partition("/")
        used = _int_prefix(left)
        total = _int_prefix(right)
    elif cycles:
        used = _int_prefix(cycles)
    return {
        "pr_url": pr_url,
        "fixed_count": _int_prefix(rows.get("findings fixed", "0")),
        "false_positives_skipped": _int_prefix(rows.get("false positives skipped", "0")),
        "verification": rows.get("verification", "unknown"),
        "verification_command": rows.get("verification command", ""),
        "rereview": rows.get("re-review", rows.get("rereview", "unknown")),
        "cycles_used": used,
        "cycles_total": total,
        "pending_confirmation": False,
        "semantic_risk": False,
    }


def load_latest_run_summary(runs_jsonl: str | Path, repo: str, pr_number: int) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for line in Path(runs_jsonl).read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # skip a malformed line in the append-only log
        if record.get("repo") == repo and int(record.get("pr") or 0) == pr_number:
            latest = record
    if latest is None:
        raise ValueError(
            f"no terminal CR-loop run recorded for {repo}#{pr_number} in {runs_jsonl}. "
            "Run the Gemini review loop on this PR first (it records terminal state to "
            "runs.jsonl), or pass --loop-summary with an explicit summary JSON."
        )
    return _summary_from_record(latest, f"https://github.com/{repo}/pull/{pr_number}")


def load_pr_body_summary(
    repo: str,
    pr_number: int,
    *,
    runner: Runner = _default_pr_runner,
) -> dict[str, Any]:
    pr = runner(["api", f"repos/{repo}/pulls/{pr_number}"])
    if not isinstance(pr, dict):
        raise ValueError("PR metadata response was not an object")
    return _summary_from_pr_body(str(pr.get("body") or ""), f"https://github.com/{repo}/pull/{pr_number}")


def load_loop_summary_for_pr(
    runs_jsonl: str | Path,
    repo: str,
    pr_number: int,
    *,
    runner: Runner = _default_pr_runner,
) -> dict[str, Any]:
    try:
        return load_latest_run_summary(runs_jsonl, repo, pr_number)
    except (OSError, ValueError):
        return load_pr_body_summary(repo, pr_number, runner=runner)


def _fetch_app_text(app_repo: str, ref: str, path: str, runner: Runner) -> str:
    payload = runner(["api", f"repos/{app_repo}/contents/{path}?ref={ref}"])
    text = _decode_content(payload)
    if text is None:
        raise RuntimeError(f"could not read {path} from {app_repo}@{ref}")
    return text


def _fetch_changed_py_content(
    app_repo: str,
    pr_number: int,
    changed_entries: list[dict[str, str]],
    runner: Runner,
) -> dict[str, str]:
    """Fetch PR-head content of changed Python files as {path: content}.

    Env-var classification needs the *content* of changed code at the PR head,
    not just the path list. Deleted files and unreadable blobs are skipped so a
    single bad file never sinks the readiness run.
    """
    head = runner(["api", f"repos/{app_repo}/pulls/{pr_number}"])
    head_sha = head.get("head", {}).get("sha") if isinstance(head, dict) else None
    if not head_sha:
        return {}
    content: dict[str, str] = {}
    for entry in changed_entries:
        path = entry.get("path", "")
        if not path.endswith(".py") or entry.get("status") == "removed":
            continue
        payload = runner(["api", f"repos/{app_repo}/contents/{quote(path)}?ref={head_sha}"])
        text = _decode_content(payload)
        if text is not None:
            content[path] = text
    return content


def _load_remote_capabilities(
    app_repo: str,
    config_text: str,
    config_ref: str,
    *,
    runner: Runner,
    templates_root: Path,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    capabilities = capabilities_from_config(config_text)
    packs: dict[str, dict[str, Any]] = {}
    for cap_type, entry in capabilities.items():
        pack_path = str(entry["template"])
        pack_text = _fetch_app_text(app_repo, config_ref, pack_path, runner)
        pack = load_pack(pack_text)
        pack_dir = posixpath.dirname(pack_path)
        template_map = pack.get("template_map") or {}
        rewritten: dict[str, Any] = {}
        for key, template in template_map.items():
            if not isinstance(template, dict):
                continue
            template_path = str(template.get("template") or "")
            if not template_path:
                continue
            source_path = posixpath.normpath(posixpath.join(pack_dir, template_path))
            text = _fetch_app_text(app_repo, config_ref, source_path, runner)
            target = templates_root / source_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            item = dict(template)
            item["template"] = source_path
            rewritten[key] = item
        pack["template_map"] = rewritten
        packs[cap_type] = pack
    return capabilities, packs


def _infra_target(pack: dict[str, Any]) -> dict[str, Any] | None:
    config = pack.get("config") if isinstance(pack.get("config"), dict) else {}
    sources = config.get("architecture_sources") if isinstance(config, dict) else None
    if not isinstance(sources, list) or not sources:
        return None
    source = sources[0]
    if not isinstance(source, dict):
        return None
    return {
        "repo": source.get("repo"),
        "base": source.get("ref") or "main",
        "allow": source.get("allow") or [],
    }


def run_readiness(
    app_repo: str,
    pr_number: int,
    loop_summary: dict[str, Any],
    *,
    runner: Runner = _default_pr_runner,
    trust_pr_config: bool = False,
    do_publish: bool = False,
    publish_html: bool = False,
    config_override: dict[str, Any] | None = None,
    stage_infra: bool = False,
    create_infra_pr: bool = False,
    infra_git_runner: Any = None,
    infra_github_runner: Any = None,
    report_runner: Any = None,
) -> dict[str, Any]:
    changed_entries = fetch_pr_changed_file_entries(app_repo, pr_number, runner=runner)
    changed_files = [entry["path"] for entry in changed_entries]
    infra_files: dict[str, str] = {}
    pack = build_pack(
        app_repo,
        pr_number,
        changed_files,
        runner=runner,
        trust_pr_config=trust_pr_config,
        config_override=config_override,
        infra_sink=infra_files,
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
    obligations: list[dict[str, Any]] = []
    config = pack.get("config") if isinstance(pack.get("config"), dict) else {}
    config_text = str(config.get("text") or "")
    config_ref = str(config.get("ref") or "")
    if config_text and config_ref:
        with tempfile.TemporaryDirectory() as tmp:
            templates_root = Path(tmp)
            capabilities, packs = _load_remote_capabilities(
                app_repo,
                config_text,
                config_ref,
                runner=runner,
                templates_root=templates_root,
            )
            service = str(pack.get("service") or "")
            obligations = detect_obligations(
                changed_entries, capabilities, packs, service=service
            )
            # Env-var precedent classification: merge into the same obligations
            # list so `config` verdicts flow through the existing generation path
            # and `secret`/`unknown` verdicts surface as human-gate rows.
            changed_content = _fetch_changed_py_content(
                app_repo, pr_number, changed_entries, runner
            )
            obligations.extend(
                detect_env_obligations(
                    changed_content,
                    infra_files,
                    capabilities,
                    packs,
                    service=service,
                )
            )
            target = _infra_target(pack)
            if stage_infra or create_infra_pr:
                if target is None or not target.get("repo"):
                    raise RuntimeError("cannot stage infra: mergeproof config has no architecture source")
                obligations = stage_obligations(
                    obligations,
                    repo=str(target["repo"]),
                    base=str(target["base"]),
                    allow=list(target["allow"]),
                    templates_root=templates_root,
                    source_pr=loop_summary.get("pr_url", f"https://github.com/{app_repo}/pull/{pr_number}"),
                    dry_run=False,
                    create_pr=create_infra_pr,
                    runner=infra_git_runner or default_runner,
                    github_runner=infra_github_runner or default_gh_runner,
                )
    readiness = build_readiness(loop_summary, pack, risks, obligations=obligations)
    markdown = render_markdown(readiness)
    report_url = None
    if do_publish and publish_html:
        report_url = publish_report(
            app_repo,
            pr_number,
            render_html(readiness),
            runner=report_runner or default_gh_runner,
        )
        markdown = with_report_link(markdown, report_url)
    result = {
        "status": "rendered",
        "readiness": readiness,
        "markdown": markdown,
        "pack": pack,
        "risks": risks,
    }
    if report_url:
        result["report_url"] = report_url
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
    parser.add_argument(
        "--stage-infra",
        action="store_true",
        help="Stage and push generated infra changes for matched obligations.",
    )
    parser.add_argument(
        "--create-infra-pr",
        action="store_true",
        help="Create or reuse an infra PR after staging generated infra changes.",
    )
    parser.add_argument(
        "--publish-html",
        action="store_true",
        help="Publish the HTML report to GitHub Pages and link it in the PR comment (requires --publish).",
    )
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
            loop_summary = load_loop_summary_for_pr(args.runs_jsonl, repo, number)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not load terminal loop summary: {exc}", file=sys.stderr)
        return 2

    config_override = None
    if args.mergeproof:
        try:
            from mergeproof_config import load_config

            text = Path(args.mergeproof).read_text(encoding="utf-8", errors="replace")
            fmt = "json" if args.mergeproof.endswith(".json") else "yaml"
            config_override = load_config(text, fmt=fmt)
            config_override["_raw_text"] = text
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
            publish_html=args.publish_html,
            config_override=config_override,
            stage_infra=args.stage_infra or args.create_infra_pr,
            create_infra_pr=args.create_infra_pr,
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

#!/usr/bin/env python3
"""Bootstrap a `mergeproof.yaml` for a service (onboarding, not PR runtime).

This proposes a config; it does not enforce anything. The intended lifecycle is:

    mergeproof init   -> generate mergeproof.yaml (this script)
    human reviews     -> commit to the app repo's trusted base branch
    mergeproof run    -> consumes the trusted base config

No network access: the config is generated purely from the provided arguments.
"""

from __future__ import annotations

import argparse
import sys

DEFAULT_LIMITS = {"max_files": 200, "max_file_bytes": 262144}


def default_allow(service: str) -> list[str]:
    """Best-effort starter allowlist for a service's infra surfaces."""
    return [
        f"helm/{service}/**",
        "infra/terraform/**",
        "modules/ingress/**",
        "modules/postgresql/**",
        "modules/redis/**",
        "modules/secrets/**",
    ]


def render_config(
    *,
    service: str,
    infra_repo: str,
    ref: str,
    env: str,
    allow: list[str],
    limits: dict[str, int] | None = None,
) -> str:
    limits = limits or DEFAULT_LIMITS
    lines = [
        "version: 1",
        "",
        f"service: {service}",
        "",
        f"# Production environment: {env}",
        "architecture_sources:",
        f"  - repo: {infra_repo}",
        f"    ref: {ref}",
        "    allow:",
    ]
    lines.extend(f"      - {pattern}" for pattern in allow)
    lines.extend(
        [
            "",
            "limits:",
            f"  max_files: {limits['max_files']}",
            f"  max_file_bytes: {limits['max_file_bytes']}",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an initial mergeproof.yaml for a service."
    )
    parser.add_argument("--service", required=True, help="Service name, e.g. familia-ai.")
    parser.add_argument(
        "--infra-repo", required=True, help="Infra repo in OWNER/REPO format."
    )
    parser.add_argument("--ref", default="main", help="Infra source ref. Default: main.")
    parser.add_argument("--env", default="prod", help="Production environment. Default: prod.")
    parser.add_argument(
        "--allow",
        action="append",
        default=None,
        help="Override an allowlisted infra path glob (repeatable).",
    )
    parser.add_argument("--max-files", type=int, default=DEFAULT_LIMITS["max_files"])
    parser.add_argument("--max-file-bytes", type=int, default=DEFAULT_LIMITS["max_file_bytes"])
    parser.add_argument(
        "--output", help="Write config to this path. Prints to stdout when omitted."
    )
    parser.add_argument(
        "--force", action="store_true", help="Overwrite an existing --output file."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    allow = args.allow if args.allow else default_allow(args.service)
    payload = render_config(
        service=args.service,
        infra_repo=args.infra_repo,
        ref=args.ref,
        env=args.env,
        allow=allow,
        limits={"max_files": args.max_files, "max_file_bytes": args.max_file_bytes},
    )

    if not args.output:
        print(payload, end="")
        return 0

    from pathlib import Path

    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"error: {output} already exists; pass --force to overwrite", file=sys.stderr)
        return 1
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(payload, encoding="utf-8")
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

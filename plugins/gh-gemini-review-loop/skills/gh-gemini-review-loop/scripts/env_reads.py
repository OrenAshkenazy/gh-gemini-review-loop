#!/usr/bin/env python3
"""Detect environment-variable reads in a PR's changed Python files.

Deterministic and zero-dependency. Operates on an in-memory ``{path: content}``
mapping so it is fully unit-testable offline. An env read is one obligation
*signal*; classification into config vs secret happens in ``env_precedent``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# os.environ["NAME"] | os.environ.get("NAME") | os.getenv("NAME"); single or double quotes.
_ENV_ACCESS = re.compile(
    r"\bos\.(?:environ\s*\[\s*|environ\.get\s*\(\s*|getenv\s*\(\s*)"
    r"""['"]([A-Z][A-Z0-9_]*)['"]"""
)

_WORKER_SEGMENTS = ("/workers/", "/jobs/", "/consumers/")


def _scope_for(path: str) -> str:
    lowered = path.lower().replace("\\", "/")  # normalize Windows separators
    return "worker" if any(seg in lowered for seg in _WORKER_SEGMENTS) else "api"


def detect_env_reads(changed_content: dict[str, str]) -> list[dict[str, Any]]:
    """Return one record per distinct env name: ``{name, scope, source_file, source_line}``.

    Scope is ``worker`` or ``api`` per source path; a name seen in both becomes
    ``both``. The first occurrence supplies ``source_file``/``source_line``.
    """
    reads: dict[str, dict[str, Any]] = {}
    for path in sorted(changed_content):
        if not path.endswith(".py"):
            continue
        text = changed_content[path]
        if not isinstance(text, str):
            continue  # defend against non-str values in the content map
        scope = _scope_for(path)
        for match in _ENV_ACCESS.finditer(text):
            line_start = text.rfind("\n", 0, match.start()) + 1
            if "#" in text[line_start:match.start()]:
                continue  # commented-out read; skip rather than emit a false obligation
            name = match.group(1)
            line = text.count("\n", 0, match.start()) + 1
            existing = reads.get(name)
            if existing is None:
                reads[name] = {
                    "name": name,
                    "scope": scope,
                    "source_file": path,
                    "source_line": line,
                }
            elif existing["scope"] != scope:
                existing["scope"] = "both"
    return [reads[name] for name in sorted(reads)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect env-var reads in changed Python files.")
    parser.add_argument(
        "--changed-content",
        required=True,
        help="Path to a JSON object mapping changed file path -> file content.",
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        data = json.loads(Path(args.changed_content).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    content = data if isinstance(data, dict) else {}
    reads = detect_env_reads(content)
    if args.json_output:
        print(json.dumps(reads, indent=2, sort_keys=True))
    else:
        for read in reads:
            print(f"  {read['name']} (scope {read['scope']}) {read['source_file']}:{read['source_line']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Render a matched obligation's Capability Pack into infra files.

Deterministic, zero-dependency. Each ``generates:`` key maps (via the pack's
``template_map``) to a template file and an output path. Templates use
``string.Template`` ``${input}`` substitution. A ``${HUMAN_GATE:reason}`` marker
is never filled with a real value — it is replaced with a greppable
``TODO-HUMAN: reason`` placeholder so a human must complete it before merge.

Note: templates use ``string.Template``, so any literal ``$`` in a template that
is not a ``${input}`` placeholder must be written as ``$$`` (e.g. shell/k8s
``$(VAR)`` env refs need the ``$`` doubled). ``substitute`` (not ``safe_substitute``)
is used deliberately so unresolved placeholders fail loud.

Never writes to disk or any repo; returns ``{output_path: content}``. Writing /
pushing is the publisher's job.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import string
import sys
from pathlib import Path
from typing import Any

_HUMAN_GATE_RE = re.compile(r"\$\{HUMAN_GATE:([^}]*)\}")


class GenerateError(ValueError):
    """Raised when a template cannot be rendered safely."""


def _allowed(path: str, allow: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in allow)


def _render(template_text: str, inputs: dict[str, str]) -> str:
    # Replace human-gate markers FIRST so they survive ${...} substitution.
    text = _HUMAN_GATE_RE.sub(lambda m: f"TODO-HUMAN: {m.group(1)}", template_text)
    try:
        return string.Template(text).substitute(inputs)
    except KeyError as exc:
        raise GenerateError(f"template requires input {exc} which was not provided") from exc
    except ValueError as exc:
        raise GenerateError(f"malformed template placeholder: {exc}") from exc


def _subst_path(path_template: str, inputs: dict[str, str]) -> str:
    try:
        return string.Template(path_template).substitute(inputs)
    except KeyError as exc:
        raise GenerateError(f"output path requires input {exc} which was not provided") from exc


def generate_files(
    pack: dict[str, Any],
    inputs: dict[str, str],
    templates_root: str | Path,
    allow: list[str],
) -> dict[str, str]:
    """Return ``{output_path: rendered_content}`` for every ``generates:`` key."""
    templates_root = Path(templates_root)
    template_map = pack.get("template_map") or {}
    result: dict[str, str] = {}
    for key in pack.get("generates") or []:
        entry = template_map.get(key)
        if not isinstance(entry, dict) or "template" not in entry or "output" not in entry:
            raise GenerateError(f"pack has no template_map entry for generates key '{key}'")
        template_path = templates_root / entry["template"]
        try:
            template_text = template_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GenerateError(f"cannot read template {entry['template']}: {exc}") from exc
        output_path = _subst_path(entry["output"], inputs)
        if not _allowed(output_path, allow):
            raise GenerateError(f"generated path '{output_path}' is outside the allowlist")
        result[output_path] = _render(template_text, inputs)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render a Capability Pack's infra files (no writes).")
    parser.add_argument("--pack", required=True, help="Path to a capability pack YAML file.")
    parser.add_argument("--inputs", required=True, help="JSON object of input values.")
    parser.add_argument("--templates-root", required=True, help="Directory holding the pack's templates.")
    parser.add_argument("--allow", required=True, help="Comma-separated allowlist globs.")
    return parser


def main(argv: list[str] | None = None) -> int:
    from capability_pack import load_pack

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        pack = load_pack(Path(args.pack).read_text(encoding="utf-8"))
        inputs = json.loads(args.inputs)
        files = generate_files(pack, inputs, args.templates_root, args.allow.split(","))
    except (OSError, ValueError, GenerateError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(files, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

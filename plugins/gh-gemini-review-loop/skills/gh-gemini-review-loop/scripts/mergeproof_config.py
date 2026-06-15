#!/usr/bin/env python3
"""Zero-dependency parser and validator for MergeProof config."""

from __future__ import annotations

import json
import re
from typing import Any

_SYNTAX_ERR = (
    "Unsupported mergeproof.yaml syntax. Use the documented subset or mergeproof.json."
)


class MergeProofConfigError(ValueError):
    """Raised for malformed or unsupported MergeProof config."""


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(token: str) -> Any:
    token = token.strip()
    if token == "":
        return None
    if token[0] in "{[" or token[0] in "&*" or token.startswith("<<"):
        raise MergeProofConfigError(_SYNTAX_ERR)
    if token in ("|", ">"):
        raise MergeProofConfigError(_SYNTAX_ERR)
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    return token


def _normalize_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = _strip_comment(raw)
        if stripped.strip() == "":
            continue
        # Reject any tab in the leading whitespace. lstrip(" ") only strips
        # spaces, so a line that *starts* with a tab would otherwise compute
        # indent=0 and slip past stripped[:indent] entirely.
        leading_ws = stripped[: len(stripped) - len(stripped.lstrip())]
        if "\t" in leading_ws:
            raise MergeProofConfigError(_SYNTAX_ERR)
        indent = len(stripped) - len(stripped.lstrip(" "))
        lines.append((indent, stripped.strip()))
    return lines


def parse_yaml_subset(text: str) -> Any:
    lines = _normalize_lines(text)
    if not lines:
        return {}
    value, idx = _parse_block(lines, 0, lines[0][0])
    if idx != len(lines):
        raise MergeProofConfigError(_SYNTAX_ERR)
    return value


def _parse_block(lines: list[tuple[int, str]], idx: int, indent: int) -> tuple[Any, int]:
    content = lines[idx][1]
    if content == "-" or content.startswith("- "):
        return _parse_list(lines, idx, indent)
    return _parse_map(lines, idx, indent)


def _parse_map(lines: list[tuple[int, str]], idx: int, indent: int) -> tuple[dict, int]:
    result: dict[str, Any] = {}
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent or content.startswith("- "):
            raise MergeProofConfigError(_SYNTAX_ERR)
        if ":" not in content:
            raise MergeProofConfigError(_SYNTAX_ERR)
        key, _, rest = content.partition(":")
        key = key.strip()
        if not key or key.startswith(("<<", "&", "*")):
            raise MergeProofConfigError(_SYNTAX_ERR)
        if key in result:
            raise MergeProofConfigError(f"Duplicate key '{key}' in mergeproof config")
        rest = rest.strip()
        if rest:
            result[key] = _scalar(rest)
            idx += 1
            continue
        if idx + 1 < len(lines) and lines[idx + 1][0] > indent:
            child, idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
            result[key] = child
            continue
        result[key] = None
        idx += 1
    return result, idx


def _parse_list(lines: list[tuple[int, str]], idx: int, indent: int) -> tuple[list, int]:
    result: list[Any] = []
    while idx < len(lines):
        cur_indent, content = lines[idx]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise MergeProofConfigError(_SYNTAX_ERR)
        if not (content == "-" or content.startswith("- ")):
            break
        item = content[1:].strip()
        if item == "":
            if idx + 1 < len(lines) and lines[idx + 1][0] > indent:
                child, idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
                result.append(child)
                continue
            result.append(None)
            idx += 1
            continue
        # A quoted scalar (e.g. "envs/prod: api/**") may contain ": " but is a
        # string, not an inline map — route it to _scalar before the map check.
        if item[:1] not in ("'", '"') and re.match(r"[^:\s][^:]*:(\s|$)", item):
            lines[idx] = (indent + 2, item)
            child, idx = _parse_map(lines, idx, indent + 2)
            result.append(child)
            continue
        result.append(_scalar(item))
        idx += 1
    return result, idx


def validate_config(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise MergeProofConfigError("mergeproof config must be a mapping")
    service = data.get("service")
    if not isinstance(service, str) or not service.strip():
        raise MergeProofConfigError("mergeproof config requires a 'service' string")
    sources = data.get("architecture_sources")
    if not isinstance(sources, list) or not sources:
        raise MergeProofConfigError(
            "mergeproof config requires a non-empty 'architecture_sources' list"
        )

    normalized: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, dict):
            raise MergeProofConfigError("each architecture source must be a mapping")
        repo = source.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise MergeProofConfigError("source 'repo' must be in OWNER/REPO format")
        allow = source.get("allow")
        if not isinstance(allow, list) or not allow:
            raise MergeProofConfigError("source 'allow' must be a non-empty list of path globs")
        if not all(isinstance(pattern, str) and pattern for pattern in allow):
            raise MergeProofConfigError("source 'allow' must be a non-empty list of path globs")
        ref = source.get("ref")
        ref = str(ref) if ref is not None else "main"
        normalized.append(
            {"repo": repo, "ref": ref, "allow": list(allow)}
        )

    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise MergeProofConfigError("'limits' must be a mapping")
    return {
        "version": data.get("version", 1),
        "service": service,
        "architecture_sources": normalized,
        "limits": {
            "max_files": int(limits.get("max_files", 200)),
            "max_file_bytes": int(limits.get("max_file_bytes", 262144)),
        },
    }


def load_config(text: str, *, fmt: str) -> dict[str, Any]:
    if fmt == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MergeProofConfigError(f"invalid mergeproof.json: {exc}") from exc
        return validate_config(data)
    if fmt == "yaml":
        return validate_config(parse_yaml_subset(text))
    raise MergeProofConfigError(f"unknown config format: {fmt!r}")

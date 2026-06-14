#!/usr/bin/env python3
"""Fetch the allowlisted slice of an infra repo with hard safety limits."""

from __future__ import annotations

import base64
import re
from typing import Any, Callable

Runner = Callable[[list[str]], Any]

_BINARY_EXT = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".jar",
    ".so",
    ".bin",
)


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    idx = 0
    while idx < len(pattern):
        token = pattern[idx]
        if pattern[idx:idx + 3] == "**/":
            parts.append("(?:.*/)?")
            idx += 3
            continue
        if pattern[idx:idx + 2] == "**":
            parts.append(".*")
            idx += 2
            continue
        if token == "*":
            parts.append("[^/]*")
            idx += 1
            continue
        if token == "?":
            parts.append("[^/]")
            idx += 1
            continue
        parts.append(re.escape(token))
        idx += 1
    return re.compile("^" + "".join(parts) + "$")


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_to_regex(pattern).match(path) for pattern in patterns)


def _decode_content(payload: Any) -> str | None:
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(payload.get("content", "") or "")
    except (TypeError, ValueError):
        return None
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def fetch_infra_files(
    source: dict[str, Any], *, max_files: int, max_file_bytes: int, runner: Runner
) -> dict[str, Any]:
    repo = source["repo"]
    sha = source["resolved_sha"]
    allow = source["allow"]

    tree = runner(["api", f"repos/{repo}/git/trees/{sha}?recursive=1"])
    entries = tree.get("tree", []) if isinstance(tree, dict) else []
    blobs = [entry for entry in entries if entry.get("type") == "blob"]
    matched = [entry for entry in blobs if path_matches(entry.get("path", ""), allow)]

    files: dict[str, str] = {}
    skipped: list[dict[str, str]] = []
    for entry in matched[max_files:]:
        skipped.append({"path": entry["path"], "reason": "over_max_files"})

    for entry in matched[:max_files]:
        path = entry["path"]
        if entry.get("size", 0) and entry["size"] > max_file_bytes:
            skipped.append({"path": path, "reason": "too_large"})
            continue
        if path.lower().endswith(_BINARY_EXT):
            skipped.append({"path": path, "reason": "binary"})
            continue
        payload = runner(["api", f"repos/{repo}/contents/{path}?ref={sha}"])
        text = _decode_content(payload)
        if text is None:
            skipped.append({"path": path, "reason": "binary"})
            continue
        files[path] = text

    return {
        "files": files,
        "fetched_paths": sorted(files),
        "skipped": skipped,
        "truncated": bool(tree.get("truncated")) if isinstance(tree, dict) else False,
    }

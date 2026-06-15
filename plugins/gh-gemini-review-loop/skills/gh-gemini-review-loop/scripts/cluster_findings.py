#!/usr/bin/env python3
"""Cluster Gemini findings by a deterministic pattern signature.

Pure, stdlib-only, no network. Distinct from finding_fingerprint() in
fetch_gemini_threads.py: that keeps path + text to identify ONE finding;
this strips everything location- and instance-specific to capture the KIND
of suggestion, so two findings of the same kind in different files share a
signature.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

# Local copies of two trivial patterns so this module stays independent of the
# 2800-line fetch_gemini_threads module.
_SEVERITY_RE = re.compile(r"!\[(critical|high|medium|low)\]", re.IGNORECASE)
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "unknown": 4}

_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")          # ![alt](url) images
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)       # ```suggestion``` etc.
_INLINE_CODE_RE = re.compile(r"`[^`]*`")               # `identifier`
_LINE_ECHO_RE = re.compile(r"\b(?:lines?|cols?|columns?)\s*\d+", re.IGNORECASE)
_COLON_NUM_RE = re.compile(r":\d+\b")                  # :204
_QUOTED_RE = re.compile(r"(['\"]).*?\1")               # 'literal' / "literal"
_NUM_RE = re.compile(r"\b\d+\b")
_SINGLE_CHAR_VAR_RE = re.compile(r"\b[a-zA-Z]\b")  # bare single-letter identifiers
_WS_RE = re.compile(r"\s+")


def _first_body(thread: Any) -> str:
    if not isinstance(thread, dict):
        return ""
    comments = thread.get("comments")
    if isinstance(comments, dict):
        comments = comments.get("nodes")
    if not isinstance(comments, list) or not comments:
        return ""
    first = comments[0]
    body = first.get("body") if isinstance(first, dict) else None
    return body if isinstance(body, str) else ""


def _normalize(body: str) -> str:
    body = _IMG_RE.sub(" ", body)
    body = _FENCED_RE.sub(" ", body)
    body = _INLINE_CODE_RE.sub(" ", body)
    body = _LINE_ECHO_RE.sub(" ", body)
    body = _COLON_NUM_RE.sub(" ", body)
    body = _QUOTED_RE.sub(" ", body)
    body = _NUM_RE.sub(" ", body)
    body = _SINGLE_CHAR_VAR_RE.sub(" ", body)
    body = _WS_RE.sub(" ", body).strip().lower()
    return body


def pattern_signature(thread: Any) -> str:
    """Short, stable signature of a finding's KIND. '' for malformed input."""
    normalized = _normalize(_first_body(thread))
    if not normalized:
        return ""
    return hashlib.sha1(normalized[:1000].encode()).hexdigest()[:8]


@dataclass(frozen=True)
class Cluster:
    signature: str
    label: str
    severity: str
    sites: list[str]
    count: int


def _severity(thread: Any) -> str:
    body = _first_body(thread)
    match = _SEVERITY_RE.search(body)
    return match.group(1).lower() if match else "unknown"


def _site(thread: dict[str, Any]) -> str:
    path = thread.get("path") or "?"
    line = thread.get("line")
    if line is None:
        line = thread.get("originalLine")
    return f"{path}:{line}" if line is not None else str(path)


def _label(body: str) -> str:
    """Short human title: the normalized prose, truncated. Not polished."""
    norm = _normalize(body)
    return (norm[:60] + "…") if len(norm) > 60 else norm


def cluster(threads: list[Any]) -> list[Cluster]:
    """Group threads by pattern_signature; sort by severity desc then count desc."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for thread in threads:
        sig = pattern_signature(thread)
        if not sig:
            continue
        groups.setdefault(sig, []).append(thread)

    clusters: list[Cluster] = []
    for sig, members in groups.items():
        best = min(members, key=lambda t: _SEVERITY_ORDER[_severity(t)])
        severity = _severity(best)
        clusters.append(
            Cluster(
                signature=sig,
                label=_label(_first_body(best)),
                severity=severity,
                sites=[_site(m) for m in members],
                count=len(members),
            )
        )
    clusters.sort(key=lambda c: (_SEVERITY_ORDER[c.severity], -c.count))
    return clusters


def recurrence_stats(
    current_sigs: list[str],
    *,
    prior_sigs: set[str],
    swept_sigs: set[str],
) -> dict[str, Any]:
    """Convergence signals for one cycle.

    - distinct_patterns: number of unique signatures this cycle
    - recurrence_rate: fraction of this cycle's findings whose signature was
      seen in a prior cycle (0.0 when there are no findings)
    - recurred_after_sweep: sorted signatures that were swept yet reappeared
    """
    valid = [s for s in current_sigs if s]
    total = len(valid)
    recurred = sum(1 for s in valid if s in prior_sigs)
    recurred_after_sweep = sorted({s for s in valid if s in swept_sigs})
    return {
        "distinct_patterns": len(set(valid)),
        "recurrence_rate": (recurred / total) if total else 0.0,
        "recurred_after_sweep": recurred_after_sweep,
    }

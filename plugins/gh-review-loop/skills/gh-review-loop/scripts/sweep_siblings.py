#!/usr/bin/env python3
"""Find the unflagged sibling instances of a pattern the reviewer did flag.

A bot reviewer flags a pattern at the sites it happened to look at. Fixing only
those teaches it to flag the rest next cycle, which is where the round-three
problem comes from. This module answers one question deterministically: given
two or more sites the reviewer flagged, which *other* lines in the PR's changed
files look like the same thing?

The method is deliberately dumb and explainable, because a sweep that edits code
the reviewer never mentioned has to justify itself:

1. Read the source line at each flagged site.
2. Reduce each to a token set, dropping string and numeric literals (which vary
   per site) and structural noise.
3. Intersect those sets. What survives is the pattern's *invariant tokens* --
   the part that is the same at every flagged site.
4. Search the changed files for other lines containing every invariant token.

Step 3 is the whole safety argument. One flagged site would let any line that
shares a token look like a sibling; requiring agreement across sites means a
candidate has to match what the flagged sites have in common, not what any one
of them happens to contain.

This reports. It never edits, and it never reads a file outside the changed set,
so the blast radius cannot leave the PR's own diff.

Pure stdlib, no network. Language-agnostic: it tokenizes rather than parsing, so
it degrades to "no candidates" on syntax it does not understand instead of
guessing.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# A pattern flagged at exactly one site is not yet a pattern. Two is the minimum
# that lets us intersect, and the intersection is the safety property.
MIN_FLAGGED_SITES = 2

# Below this many invariant tokens, the intersection is not specific enough to
# call anything a sibling -- `self` and `=` are common to half a file.
MIN_INVARIANT_TOKENS = 2

# Reporting cap. A sweep that proposes fifty sites is not a sweep, it is a
# refactor, and it should go back to the human.
DEFAULT_MAX_CANDIDATES = 20

# Reading whole files is fine for source, but a vendored bundle or a lockfile
# is neither reviewable nor worth scanning.
MAX_FILE_BYTES = 2_000_000

# A reviewer finding anchors to code. Proposing a comment line as a sibling is
# never actionable, and prose happens to share ordinary words with code, which
# is the one false-positive class a token intersection cannot rule out on its
# own. Handles Python, JS/TS/Java/Go/Rust, shell, SQL and Lisp comment markers.
_COMMENT_ONLY_RE = re.compile(r"^\s*(#|//|/\*|\*|--|;)")

_STRING_RE = re.compile(r"""(['"])(?:\\.|(?!\1).)*\1""", re.DOTALL)
_NUMBER_RE = re.compile(r"\b\d[\d_.]*\b")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*|[(){}\[\].,;:=<>!+\-*/%&|^~@]+")

# Tokens too common to carry signal. Keeping them would let a candidate match on
# `self` alone. This is a stopword list, not a language model: an unknown
# language simply contributes no stopwords and the token-count floor still holds.
_STOPWORDS = frozenset({
    "self", "this", "if", "else", "elif", "for", "while", "return", "def",
    "class", "function", "const", "let", "var", "func", "fn", "in", "is",
    "not", "and", "or", "true", "false", "none", "null", "nil", "new",
    "import", "from", "as", "pass", "try", "with", "async", "await",
    "public", "private", "static", "void", "the", "a", "an",
    "=", "==", "(", ")", "{", "}", "[", "]", ",", ".", ";", ":", "+", "-",
})


@dataclass(frozen=True)
class Site:
    """A file:line location, as it appears in a cluster's ``sites`` tuple."""

    path: str
    line: int | None

    def __str__(self) -> str:
        return f"{self.path}:{self.line}" if self.line is not None else self.path


@dataclass
class SweepResult:
    signature: str
    label: str
    invariant_tokens: tuple[str, ...] = ()
    flagged: tuple[str, ...] = ()
    candidates: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    reason: str = ""
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "signature": self.signature,
            "label": self.label,
            "status": self.status,
            "reason": self.reason,
            "invariantTokens": list(self.invariant_tokens),
            "flaggedSites": list(self.flagged),
            "candidates": self.candidates,
            "truncated": self.truncated,
        }


def parse_site(raw: str) -> Site:
    """Parse ``path:line`` as emitted by cluster_findings._site().

    Windows paths and colons inside filenames are handled by splitting on the
    last colon and only treating it as a line number when it is all digits.
    """
    raw = (raw or "").strip()
    if not raw:
        return Site("", None)
    head, sep, tail = raw.rpartition(":")
    if sep and tail.isdigit():
        return Site(head, int(tail))
    return Site(raw, None)


def _strip_literals(line: str) -> str:
    """Remove string and numeric literals, which differ site to site."""
    return _NUMBER_RE.sub(" ", _STRING_RE.sub(" ", line))


def tokenize(line: str) -> set[str]:
    """Reduce a source line to its significant, literal-free tokens."""
    stripped = _strip_literals(line)
    tokens = set()
    for match in _TOKEN_RE.finditer(stripped):
        token = match.group(0)
        lowered = token.lower()
        if lowered in _STOPWORDS:
            continue
        # Single characters carry no signal on their own; operators long enough
        # to be distinctive (``->``, ``=>``, ``::``) are kept.
        if len(token) < 2 and token.isalnum():
            continue
        tokens.add(lowered)
    return tokens


def _read_lines(path: Path) -> list[str]:
    """Read a text file, or return [] for anything unreadable or oversized."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return []
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return []


def is_code_line(line: str) -> bool:
    """False for blank lines and lines that are only a comment."""
    return bool(line.strip()) and not _COMMENT_ONLY_RE.match(line)


def _line_at(lines: list[str], number: int | None) -> str:
    if number is None or number < 1 or number > len(lines):
        return ""
    return lines[number - 1]


def invariant_tokens(flagged_lines: Iterable[str]) -> set[str]:
    """Tokens present at *every* flagged site.

    This is the pattern's fingerprint. Intersecting is what keeps the sweep
    honest: a token that appears at only one flagged site describes that site,
    not the pattern.
    """
    sets = [tokenize(line) for line in flagged_lines if line.strip()]
    if not sets:
        return set()
    common = set(sets[0])
    for other in sets[1:]:
        common &= other
    return common


def sweep(
    *,
    signature: str,
    label: str,
    sites: list[str],
    changed_files: list[str],
    root: str | Path = ".",
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> SweepResult:
    """Report unflagged lines in ``changed_files`` matching the flagged pattern.

    Returns a result with ``status`` of:
      ``ok``               candidates found (possibly zero)
      ``too_few_sites``    fewer than two flagged sites, nothing to intersect
      ``no_source``        the flagged lines could not be read
      ``pattern_too_thin`` the intersection was not specific enough to search
    """
    root = Path(root)
    result = SweepResult(signature=signature, label=label,
                         flagged=tuple(str(parse_site(s)) for s in sites))

    parsed = [parse_site(s) for s in sites]
    located = [s for s in parsed if s.line is not None and s.path]
    if len(located) < MIN_FLAGGED_SITES:
        result.status = "too_few_sites"
        result.reason = (
            f"{len(located)} located site(s); a sweep needs at least "
            f"{MIN_FLAGGED_SITES} so the shared shape can be intersected."
        )
        return result

    file_cache: dict[str, list[str]] = {}

    def lines_of(rel: str) -> list[str]:
        if rel not in file_cache:
            file_cache[rel] = _read_lines(root / rel)
        return file_cache[rel]

    flagged_lines = [_line_at(lines_of(s.path), s.line) for s in located]
    flagged_lines = [line for line in flagged_lines if is_code_line(line)]
    if len(flagged_lines) < MIN_FLAGGED_SITES:
        result.status = "no_source"
        result.reason = (
            "Fewer than two flagged sites resolved to readable code. The sweep "
            "only compares real source lines, so it stops here."
        )
        return result

    common = invariant_tokens(flagged_lines)
    if len(common) < MIN_INVARIANT_TOKENS:
        result.status = "pattern_too_thin"
        result.reason = (
            f"The flagged sites share only {len(common)} distinctive token(s) "
            f"(need {MIN_INVARIANT_TOKENS}). Too broad to sweep safely."
        )
        result.invariant_tokens = tuple(sorted(common))
        return result

    result.invariant_tokens = tuple(sorted(common))
    already = {(s.path, s.line) for s in located}

    candidates: list[dict[str, Any]] = []
    for rel in changed_files:
        for index, text in enumerate(lines_of(rel), start=1):
            if (rel, index) in already:
                continue
            if not is_code_line(text):
                continue
            if common <= tokenize(text):
                candidates.append({
                    "path": rel,
                    "line": index,
                    "site": f"{rel}:{index}",
                    "text": text.strip()[:200],
                    "matchedTokens": sorted(common),
                })

    candidates.sort(key=lambda c: (c["path"], c["line"]))
    if len(candidates) > max_candidates:
        result.truncated = True
        result.reason = (
            f"{len(candidates)} candidates found; reporting the first "
            f"{max_candidates}. A pattern this wide is a refactor, not a sweep "
            "-- confirm the shape before fixing."
        )
        candidates = candidates[:max_candidates]

    result.candidates = candidates
    return result


def render_report(result: SweepResult) -> str:
    """Human-readable sweep report. Printed before anything is edited."""
    head = f"[sweep] {result.label or result.signature}"
    if result.status != "ok":
        return f"{head}\n  skipped: {result.reason}"

    lines = [
        head,
        f"  flagged:   {', '.join(result.flagged)}",
        f"  shared:    {' '.join(result.invariant_tokens)}",
    ]
    if not result.candidates:
        lines.append("  siblings:  none — the reviewer caught every instance in this diff")
        return "\n".join(lines)

    lines.append(f"  siblings:  {len(result.candidates)} unflagged site(s) match the same shape")
    for candidate in result.candidates:
        lines.append(f"    + {candidate['site']}  {candidate['text']}")
    if result.truncated:
        lines.append(f"  note:      {result.reason}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report unflagged sibling instances of a flagged pattern, within "
            "the PR's changed files. Reports only; never edits."
        )
    )
    parser.add_argument("--signature", required=True,
                        help="Cluster signature from the Patterns receipt.")
    parser.add_argument("--label", default="", help="Human label for the cluster.")
    parser.add_argument("--site", action="append", default=[], dest="sites",
                        metavar="PATH:LINE",
                        help="A flagged site. Repeat; at least two are required.")
    parser.add_argument("--changed-file", action="append", default=[],
                        dest="changed_files", metavar="PATH",
                        help="A file changed by this PR. Repeat. The search never leaves this set.")
    parser.add_argument("--root", default=".", help="Repository root. Default: cwd.")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                        help=f"Cap on reported siblings. Default: {DEFAULT_MAX_CANDIDATES}.")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Print JSON only on stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = sweep(
        signature=args.signature,
        label=args.label,
        sites=args.sites,
        changed_files=args.changed_files,
        root=args.root,
        max_candidates=args.max_candidates,
    )
    if args.json_output:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(render_report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

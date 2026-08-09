#!/usr/bin/env python3
"""Find the unflagged sibling instances of a pattern the reviewer did flag.

A bot reviewer flags a pattern at the sites it happened to look at. Fixing only
those teaches it to flag the rest next cycle, which is where the round-three
problem comes from. This module answers one question deterministically: which
*other* locations in the PR's changed files match what the reviewer flagged?

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

A separate exact-block shape handles a multi-line range from one flagged site:
comments are stripped, whitespace is collapsed, and the normalized sequence is
fingerprinted and searched across the changed files. These candidates are
reported as ``mirror`` rather than token-intersection matches.

This reports. It never edits, and it never reads a file outside the changed set,
so the blast radius cannot leave the PR's own diff.

Pure stdlib, no network. Language-agnostic: it tokenizes rather than parsing, so
it degrades to "no candidates" on syntax it does not understand instead of
guessing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# A pattern flagged at exactly one site is not yet a pattern. Two is the minimum
# that lets us intersect, and the intersection is the safety property.
MIN_FLAGGED_SITES = 2

# Below this many invariant tokens, the intersection is not specific enough to
# call anything a sibling -- `self` and `=` are common to half a file.
MIN_INVARIANT_TOKENS = 2

# Exact equality alone is unsafe for ubiquitous structural endings such as
# ``return;`` plus ``}``. A mirror block must carry identifier-like signal too.
MIN_MIRROR_SIGNIFICANT_TOKENS = 2

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

_HASH_COMMENT_SUFFIXES = frozenset({
    ".py", ".rb", ".sh", ".bash", ".zsh", ".fish",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
})
_DASH_COMMENT_SUFFIXES = frozenset({".sql"})
_HASH_COMMENT_NO_SPACE_SUFFIXES = frozenset({".py", ".rb", ".toml"})

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
    start_line: int | None = None

    def __str__(self) -> str:
        if self.start_line is not None and self.line is not None:
            return f"{self.path}:{self.start_line}-{self.line}"
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
    range_match = re.fullmatch(r"(\d+)-(\d+)", tail) if sep else None
    if range_match:
        start, end = (int(value) for value in range_match.groups())
        if start <= end:
            return Site(head, end, start)
    if sep and tail.isdigit():
        return Site(head, int(tail))
    return Site(raw, None)


def _strip_comments(
    lines: Iterable[str],
    *,
    hash_comments: bool = False,
    hash_comments_no_space: bool = True,
    slash_comments: bool = True,
    dash_comments: bool = False,
) -> list[str]:
    """Strip common line/block comments while preserving quoted markers."""
    stripped: list[str] = []
    in_block = False
    quote: str | None = None
    escaped = False
    for line in lines:
        output: list[str] = []
        index = 0
        while index < len(line):
            pair = line[index:index + 2]
            if in_block:
                if pair == "*/":
                    in_block = False
                    index += 2
                else:
                    index += 1
                continue
            character = line[index]
            if quote is not None:
                if escaped:
                    output.append(character)
                    escaped = False
                    index += 1
                    continue
                if line.startswith(quote, index):
                    output.append(quote)
                    index += len(quote)
                    quote = None
                    escaped = False
                    continue
                output.append(character)
                if character == "\\":
                    escaped = True
                index += 1
                continue
            delimiter = next(
                (
                    candidate
                    for candidate in ('"""', "'''", '"', "'", "`")
                    if line.startswith(candidate, index)
                ),
                None,
            )
            if delimiter is not None:
                quote = delimiter
                output.append(delimiter)
                index += len(delimiter)
                continue
            if pair == "/*":
                in_block = True
                index += 2
                continue
            if slash_comments and pair == "//":
                break
            if dash_comments and pair == "--":
                break
            if (
                hash_comments
                and character == "#"
                and (
                    hash_comments_no_space
                    or index == 0
                    or line[index - 1].isspace()
                )
            ):
                break
            output.append(character)
            index += 1
        stripped.append("".join(output))
    return stripped


def _collapse_whitespace(
    lines: Iterable[str], *, preserve_indentation: bool = False
) -> list[str]:
    """Collapse formatting whitespace without changing quoted text."""
    normalized_lines: list[str] = []
    quote: str | None = None
    escaped = False

    for line in lines:
        leading = ""
        if preserve_indentation:
            width = len(line) - len(line.lstrip(" \t"))
            leading = line[:width]
            line = line[width:]

        output: list[str] = []
        whitespace = False
        index = 0
        while index < len(line):
            character = line[index]
            if quote is not None:
                if escaped:
                    output.append(character)
                    escaped = False
                    index += 1
                    continue
                if line.startswith(quote, index):
                    output.append(quote)
                    index += len(quote)
                    quote = None
                    escaped = False
                    continue
                output.append(character)
                if character == "\\":
                    escaped = True
                index += 1
                continue

            delimiter = next(
                (
                    candidate
                    for candidate in ('"""', "'''", '"', "'", "`")
                    if line.startswith(candidate, index)
                ),
                None,
            )
            if delimiter is not None:
                if whitespace and output:
                    output.append(" ")
                whitespace = False
                quote = delimiter
                output.append(delimiter)
                index += len(delimiter)
            elif character.isspace():
                whitespace = True
                index += 1
            else:
                if whitespace and output:
                    output.append(" ")
                whitespace = False
                output.append(character)
                index += 1

        body = "".join(output).rstrip()
        normalized_lines.append((leading + body) if body else "")
    return normalized_lines


def normalize_block(
    lines: Iterable[str],
    *,
    hash_comments: bool = False,
    hash_comments_no_space: bool = True,
    slash_comments: bool = True,
    dash_comments: bool = False,
    preserve_indentation: bool = False,
) -> tuple[str, ...]:
    """Normalize a code block for exact mirror comparison."""
    stripped = _strip_comments(
        lines,
        hash_comments=hash_comments,
        hash_comments_no_space=hash_comments_no_space,
        slash_comments=slash_comments,
        dash_comments=dash_comments,
    )
    return tuple(
        line
        for line in _collapse_whitespace(
            stripped, preserve_indentation=preserve_indentation
        )
        if line
    )


def _block_fingerprint(block: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(block).encode("utf-8")).hexdigest()


def _comment_options(path: str) -> dict[str, bool]:
    suffix = Path(path).suffix.lower()
    hash_comments = suffix in _HASH_COMMENT_SUFFIXES
    return {
        "hash_comments": hash_comments,
        "hash_comments_no_space": suffix in _HASH_COMMENT_NO_SPACE_SUFFIXES,
        "slash_comments": not hash_comments and suffix not in _DASH_COMMENT_SUFFIXES,
        "dash_comments": suffix in _DASH_COMMENT_SUFFIXES,
        "preserve_indentation": suffix == ".py",
    }


def _normalized_code_lines(
    lines: list[str],
    *,
    path: str,
) -> list[tuple[int, str]]:
    options = _comment_options(path)
    stripped = _strip_comments(
                lines,
                hash_comments=options["hash_comments"],
                hash_comments_no_space=options["hash_comments_no_space"],
        slash_comments=options["slash_comments"],
        dash_comments=options["dash_comments"],
    )
    return [
        (number, normalized)
        for number, normalized in enumerate(
            _collapse_whitespace(
                stripped,
                preserve_indentation=options["preserve_indentation"],
            ),
            start=1,
        )
        if normalized
    ]


def _mirror_candidates(
    sites: list[Site],
    changed_files: list[str],
    lines_of: Callable[[str], list[str]],
) -> list[dict[str, Any]]:
    """Find exact normalized copies of flagged multi-line ranges."""
    changed = set(changed_files)
    seeds: list[tuple[tuple[str, ...], str]] = []
    for site in sites:
        if (
            site.path not in changed
            or site.start_line is None
            or site.line is None
            or site.start_line >= site.line
        ):
            continue
        lines = lines_of(site.path)
        if site.line > len(lines):
            continue
        normalized_source = _normalized_code_lines(lines, path=site.path)
        block = tuple(
            text
            for number, text in normalized_source
            if site.start_line <= number <= site.line
        )
        block_tokens = set().union(*(tokenize(line) for line in block), set())
        significant_count = sum(is_significant_token(token) for token in block_tokens)
        if len(block) < 2 or significant_count < MIN_MIRROR_SIGNIFICANT_TOKENS:
            continue
        seeds.append((block, _block_fingerprint(block)))

    flagged_ranges = {
        (site.path, site.start_line or site.line, site.line)
        for site in sites
        if site.line is not None
    }
    candidates: dict[tuple[str, int, int], dict[str, Any]] = {}
    normalized_files: dict[str, list[tuple[int, str]]] = {}
    for block, fingerprint in seeds:
        width = len(block)
        for rel in changed_files:
            if rel not in normalized_files:
                normalized_files[rel] = _normalized_code_lines(
                    lines_of(rel),
                    path=rel,
                )
            entries = normalized_files[rel]
            for offset in range(len(entries) - width + 1):
                window = entries[offset:offset + width]
                candidate_block = tuple(text for _, text in window)
                if (
                    _block_fingerprint(candidate_block) != fingerprint
                    or candidate_block != block
                ):
                    continue
                start, end = window[0][0], window[-1][0]
                if any(
                    rel == flagged_path
                    and start <= flagged_end
                    and flagged_start <= end
                    for flagged_path, flagged_start, flagged_end in flagged_ranges
                ):
                    continue
                candidates[(rel, start, end)] = {
                    "candidateClass": "mirror",
                    "path": rel,
                    "line": start,
                    "endLine": end,
                    "site": f"{rel}:{start}-{end}",
                    "text": " ".join(text for _, text in window)[:200],
                    "fingerprint": fingerprint,
                }
    return list(candidates.values())


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


def is_significant_token(token: str) -> bool:
    """Return whether a token contains identifier-like signal."""
    return any(character.isalpha() for character in token)


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


def _set_candidates(
    result: SweepResult,
    candidates: list[dict[str, Any]],
    max_candidates: int,
) -> None:
    candidates.sort(key=lambda candidate: (
        candidate["path"],
        candidate["line"],
        candidate.get("endLine", candidate["line"]),
        candidate["candidateClass"],
    ))
    if len(candidates) > max_candidates:
        result.truncated = True
        result.reason = (
            f"{len(candidates)} candidates found; reporting the first "
            f"{max_candidates}. A pattern this wide is a refactor, not a sweep "
            "-- confirm the shape before fixing."
        )
        candidates = candidates[:max_candidates]
    result.candidates = candidates


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
      ``too_few_sites``    no mirror and fewer than two sites to intersect
      ``no_source``        the flagged lines could not be read
      ``pattern_too_thin`` the intersection was not specific enough to search
    """
    root = Path(root)
    result = SweepResult(signature=signature, label=label,
                         flagged=tuple(str(parse_site(s)) for s in sites))

    parsed = [parse_site(s) for s in sites]
    changed = set(changed_files)
    located = [
        site
        for site in parsed
        if site.line is not None and site.path in changed
    ]

    file_cache: dict[str, list[str]] = {}

    def lines_of(rel: str) -> list[str]:
        if rel not in file_cache:
            file_cache[rel] = _read_lines(root / rel)
        return file_cache[rel]

    mirror_candidates = _mirror_candidates(located, changed_files, lines_of)
    if len(located) < MIN_FLAGGED_SITES:
        if mirror_candidates:
            _set_candidates(result, mirror_candidates, max_candidates)
            return result
        result.status = "too_few_sites"
        result.reason = (
            f"{len(located)} located site(s); a token sweep needs at least "
            f"{MIN_FLAGGED_SITES}, and no exact multi-line mirror was found."
        )
        return result

    flagged_lines = [_line_at(lines_of(s.path), s.line) for s in located]
    flagged_lines = [line for line in flagged_lines if is_code_line(line)]
    if len(flagged_lines) < MIN_FLAGGED_SITES:
        if mirror_candidates:
            _set_candidates(result, mirror_candidates, max_candidates)
            return result
        result.status = "no_source"
        result.reason = (
            f"Fewer than {MIN_FLAGGED_SITES} flagged sites resolved to readable "
            "code. The sweep only compares real source lines, so it stops here."
        )
        return result

    common = invariant_tokens(flagged_lines)
    significant_count = sum(is_significant_token(token) for token in common)
    if significant_count < MIN_INVARIANT_TOKENS:
        if mirror_candidates:
            result.reason = (
                "Token intersection was too thin; reporting exact mirror "
                "candidate(s) instead."
            )
            _set_candidates(result, mirror_candidates, max_candidates)
            return result
        result.status = "pattern_too_thin"
        result.reason = (
            f"The flagged sites share only {significant_count} significant token(s) "
            f"(need {MIN_INVARIANT_TOKENS}). Too broad to sweep safely."
        )
        result.invariant_tokens = tuple(sorted(common))
        return result

    result.invariant_tokens = tuple(sorted(common))
    already = {
        (site.path, line_number)
        for site in located
        for line_number in range(site.start_line or site.line, site.line + 1)
    }

    candidates = list(mirror_candidates)
    mirror_ranges = [
        (candidate["path"], candidate["line"], candidate["endLine"])
        for candidate in mirror_candidates
    ]
    for rel in changed_files:
        for index, text in enumerate(lines_of(rel), start=1):
            if (rel, index) in already:
                continue
            if any(
                rel == mirror_path and mirror_start <= index <= mirror_end
                for mirror_path, mirror_start, mirror_end in mirror_ranges
            ):
                continue
            if not is_code_line(text):
                continue
            if common <= tokenize(text):
                candidates.append({
                    "candidateClass": "token",
                    "path": rel,
                    "line": index,
                    "site": f"{rel}:{index}",
                    "text": text.strip()[:200],
                    "matchedTokens": sorted(common),
                })

    _set_candidates(result, candidates, max_candidates)
    return result


def render_report(result: SweepResult) -> str:
    """Human-readable sweep report. Printed before anything is edited."""
    head = f"[sweep] {result.label or result.signature}"
    if result.status != "ok":
        return f"{head}\n  skipped: {result.reason}"

    lines = [
        head,
        f"  flagged:   {', '.join(result.flagged)}",
    ]
    if result.invariant_tokens:
        lines.append(f"  shared:    {' '.join(result.invariant_tokens)}")
    if not result.candidates:
        lines.append("  siblings:  none — the reviewer caught every instance in this diff")
        return "\n".join(lines)

    lines.append(f"  siblings:  {len(result.candidates)} unflagged site(s) match the same shape")
    for candidate in result.candidates:
        candidate_class = candidate["candidateClass"]
        class_label = f"[{candidate_class}] " if candidate_class == "mirror" else ""
        lines.append(
            f"    + {class_label}{candidate['site']}  {candidate['text']}"
        )
    if result.reason:
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
                        metavar="PATH:LINE|PATH:START-END",
                        help=(
                            "A flagged site or range. Repeat for token sweeps; "
                            "one multi-line range can find exact mirrors."
                        ))
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

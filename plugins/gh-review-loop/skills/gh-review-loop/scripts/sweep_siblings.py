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
the block is fingerprinted and searched across the changed files. These
candidates are reported as ``mirror`` rather than token-intersection matches.

Mirror matching has two modes, and the choice of boundary is the design:

**Raw, for every language.** Blocks match when their text is identical. This
catches copy-paste, which is what a duplicate detector is for, and it makes
almost no semantic assumptions -- so there is no per-language normalizer to get
wrong.

**Normalized, for explicitly supported languages only.** Today that is Python
alone. Comments are removed using Python's own ``tokenize`` module, never a
hand-rolled scanner, so two blocks differing only in their comments still match.
Whitespace is *not* collapsed, because indentation is semantic in Python.

The two modes never mix, and normalized blocks are only ever compared against
normalized blocks of the same language. Raw blocks are compared within a
language family (``.ts`` against ``.js``, ``.yml`` against ``.yaml``) so that
identical text in ``build.sh`` and ``Makefile`` -- a coincidence of both
spelling ``export`` -- is not called a duplicate.

The optimization target is **precision over recall over language coverage**.
This report is advisory: a missed duplicate costs one informational finding,
while a false one makes the whole report untrustworthy. Adding a language means
adding a real tokenizer for it, not another pile of pattern rules.

Two limitations are accepted rather than fixed, both in raw mode:

- **Lexical context is invisible.** A block can match text that is identical but
  sits somewhere else entirely -- lines inside a JavaScript template literal
  that spell out the same calls made elsewhere. Detecting that needs a parser
  per language, which is the boundary this module deliberately stays behind.
  Python does not have this gap, because ``tokenize`` identifies string bodies.
- **Line endings are not compared.** ``splitlines()`` drops the terminator, so
  an LF block and a CRLF block with the same content match and are labelled
  ``exact``. Retaining terminators would have to run through the reader shared
  with the token sweep, for a case -- mixed line endings within one language
  family in one PR -- where reporting the duplicate is arguably still right.

This reports. It never edits, and it never reads a file outside the changed set,
so the blast radius cannot leave the PR's own diff.

Pure stdlib, no network.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import tokenize as _pytokenize
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

# A mirror seed is compared against every window of the same width in every
# changed file, so its length multiplies the scan cost. Past a few hundred lines
# a "duplicate block" is a duplicated file, which is a human's call anyway.
MAX_MIRROR_BLOCK_LINES = 400

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

# Mirror matching is normalized only for languages we tokenize with a real
# tokenizer. Everything else gets raw text matching, which needs no per-language
# knowledge and therefore cannot get a language wrong.
_PYTHON_SUFFIXES = frozenset({".py", ".pyi"})

# Token types whose body is string data rather than code. Populated defensively
# because the FSTRING_* types only exist on Python 3.12+.
_STRING_TOKEN_TYPES = {_pytokenize.STRING}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _type = getattr(_pytokenize, _name, None)
    if _type is not None:
        _STRING_TOKEN_TYPES.add(_type)
del _name, _type

# Comments a tool reads rather than a human. `# type: ignore[arg-type]` and
# `# type: ignore[return-value]` suppress different diagnostics, so removing
# them makes two unrelated blocks look identical.
#
# This cannot come from the tokenizer: `tokenize` reports that a token is a
# COMMENT, and Python itself has no notion of a directive -- they are
# conventions of mypy, flake8, bandit, Cython and the formatters.
#
# It is matched by *shape* rather than by a list of tools, because the list has
# no end: type, noqa, pragma, coding, cython, distutils, doctest, numba, and
# whatever ships next year. A directive is written `tool: setting` or
# `tool=value`, so a lowercase leading word followed by `:` or `=` is the rule.
#
# Lowercase is what separates a directive from prose. Directives are lowercase
# by convention and, for Cython and PEP 263, by requirement; English comments
# capitalize, so `# Defensive: ...` stays an ordinary comment. The rule still
# errs toward preserving -- a comment kept by mistake costs one mirror, while a
# directive dropped by mistake invents one -- and the known cost is prose that
# leads with a lowercase `word:`, including bare URLs.
_PY_DIRECTIVE_RE = re.compile(r"^[a-z][a-z0-9_.\-]*\s*[:=]")

# The few directives with no `:` or `=` to key on. Short and closed, unlike a
# list of tools would be.
_PY_BARE_DIRECTIVES = ("noqa", "nosec", "nocover", "nolint", "nosonar")

# A PEP 263 source-encoding cookie decides how the interpreter reads the rest
# of the bytes, so the same bytes under `coding: utf-8` and `coding: latin-1`
# are different programs. It is a property of the *file*, not of any block
# inside it, so it belongs in the match key rather than in the comment rules.
# Matched rather than prefixed because it is legal anywhere in the comment:
# `# -*- coding: utf-8 -*-` and `# vim: set fileencoding=utf-8 :` both count.
_PY_ENCODING_COOKIE_RE = re.compile(r"coding[:=]\s*([-_.a-zA-Z0-9]+)")

# A shebang is only a shebang on the first line, and only at column 0.
_PY_SHEBANG_RE = re.compile(r"^#!(.*)$")

# Suffixes that are the same language for copy-paste purposes. This is file
# extension aliasing, not language modeling: it exists so a block copied from a
# .ts file into a .js file still reads as a duplicate, while identical text in
# build.sh and Makefile does not.
_SUFFIX_FAMILIES = {
    ".js": "ecmascript", ".jsx": "ecmascript", ".mjs": "ecmascript",
    ".cjs": "ecmascript", ".ts": "ecmascript", ".tsx": "ecmascript",
    ".mts": "ecmascript", ".cts": "ecmascript",
    ".yaml": "yaml", ".yml": "yaml",
    ".md": "markdown", ".markdown": "markdown",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ksh": "shell",
}

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
        # Line numbers are 1-based. A 0 start is malformed, and accepting it
        # makes ``start_line or line`` fall through to the end line, which
        # narrows the self-overlap guard until a seed reports its own location
        # as a mirror of itself.
        if 1 <= start <= end:
            return Site(head, end, start)
    if sep and tail.isdigit():
        return Site(head, int(tail))
    return Site(raw, None)


def _block_fingerprint(block: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(block).encode("utf-8")).hexdigest()


def _family_key(path: str) -> str:
    """Which files are comparable in raw mode.

    Same suffix, or same explicitly aliased family. A file with no suffix keys
    on its own name, so ``Makefile`` only ever matches ``Makefile`` -- identical
    text in ``build.sh`` and ``Makefile`` is both files spelling ``export``, not
    a duplicated implementation.
    """
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if not suffix:
        return f"name:{name}"
    return f"family:{_SUFFIX_FAMILIES.get(suffix, suffix)}"


def _raw_block_lines(lines: list[str]) -> list[tuple[int, str, str]]:
    """Every line, verbatim. No language knowledge, so nothing to get wrong."""
    return [(number, text, "") for number, text in enumerate(lines, start=1)]


def _is_python_directive(comment: str) -> bool:
    """Whether a Python comment is read by a tool rather than by a human.

    Such a comment is content: dropping it changes what mypy, flake8, bandit,
    Cython or coverage do, so two blocks whose only difference is a directive
    are not duplicates. See ``_PY_DIRECTIVE_RE`` for why this matches a shape
    rather than a list of tools, and why the case matters.

    Encoding cookies are handled by ``_python_encoding`` instead, because they
    govern the whole file rather than the block they appear in.
    """
    body = comment.lstrip("#").strip()
    if _PY_DIRECTIVE_RE.match(body):
        return True
    return body.lower().startswith(_PY_BARE_DIRECTIVES)


def _python_encoding(lines: list[str]) -> str:
    """The file's declared source encoding, per PEP 263.

    Two files declaring different encodings are different programs even when
    their bytes are identical, so this becomes part of the match key and they
    are never compared.

    The rule is asked of the interpreter rather than restated here. PEP 263 has
    more edges than it looks: a cookie on line 2 counts only when line 1 is
    blank or a comment, so code -- or a docstring -- on line 1 ends the search,
    and `latin-1` and `iso-8859-1` name one encoding under two spellings.
    ``detect_encoding`` is the tokenizer's own answer to all of that, and it
    normalizes the aliases, so files agreeing on the encoding agree on the key
    however they spelled it.
    """
    header = "\n".join(lines[:2]) + "\n"
    stream = io.BytesIO(header.encode("utf-8", "surrogateescape"))
    try:
        return _pytokenize.detect_encoding(stream.readline)[0]
    except (SyntaxError, UnicodeError, ValueError):
        # An undeclarable encoding is still a declaration: fall back to the
        # text as written so two files claiming the same bad cookie match.
        for line in lines[:2]:
            match = _PY_ENCODING_COOKIE_RE.search(line)
            if match and line.lstrip().startswith("#"):
                return match.group(1).lower()
        return "utf-8"


def _python_shebang(lines: list[str]) -> str:
    """The file's interpreter line, if it has one.

    A shebang is a comment to the tokenizer but not to the operating system: it
    decides which interpreter runs the file, so a Python 2 script and a Python 3
    script are different programs even when their bodies are byte-identical.
    Like the encoding cookie this is a property of the whole file rather than of
    the flagged range, so it belongs in the match key and not in the comment
    rules -- a range that happens to exclude line 1 must still not match across
    interpreters.

    The line is used verbatim. Deciding that `env python3` and `/usr/bin/python3`
    name the same interpreter would mean modeling shebangs, and the only cost of
    declining to is a duplicate that goes unreported.
    """
    if not lines:
        return ""
    match = _PY_SHEBANG_RE.match(lines[0])
    return match.group(1).strip() if match else ""


def _python_block_lines(lines: list[str]) -> list[tuple[int, str, str]] | None:
    """Comment-free Python lines, via Python's own tokenizer.

    Returns ``None`` when the source does not tokenize -- a syntax error, or a
    dialect this interpreter predates -- and the caller falls back to raw
    matching rather than guessing at the source.

    Indentation and internal spacing survive untouched, because both are
    semantic in Python; only comments and the trailing whitespace their removal
    strands are dropped. Lines interior to a multi-line string are tagged, so a
    block quoted inside a docstring never mirrors the code it quotes.
    """
    source = "\n".join(lines) + "\n"
    comment_spans: dict[int, list[tuple[int, int]]] = {}
    string_interior: set[int] = set()
    try:
        tokens = list(_pytokenize.generate_tokens(io.StringIO(source).readline))
    except (_pytokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None

    for token in tokens:
        if token.type == _pytokenize.COMMENT:
            if _is_python_directive(token.string):
                continue
            comment_spans.setdefault(token.start[0], []).append(
                (token.start[1], token.end[1])
            )
        elif token.type in _STRING_TOKEN_TYPES and token.end[0] > token.start[0]:
            string_interior.update(range(token.start[0] + 1, token.end[0] + 1))

    entries: list[tuple[int, str, str]] = []
    for number, line in enumerate(lines, start=1):
        if number in string_interior:
            # Inside a multi-line string every byte is the value, including
            # trailing spaces and blank lines. Nothing here is formatting, so
            # nothing here may be normalized away.
            entries.append((number, line, "string"))
            continue
        text = line
        for start, end in sorted(comment_spans.get(number, ()), reverse=True):
            text = text[:start] + text[end:]
        # Only whitespace stranded by removing a comment, or already trailing
        # code, is dropped -- never string data.
        text = text.rstrip()
        if not text:
            continue
        entries.append((number, text, ""))
    return entries


def _block_index(
    lines: list[str], *, path: str
) -> tuple[list[tuple[int, str, str]], str]:
    """The comparable line index for a file, plus the key it matches under.

    Two files are compared only when their keys are equal, so normalized Python
    never meets raw text, and raw text never crosses a language family.
    """
    if Path(path).suffix.lower() in _PYTHON_SUFFIXES:
        entries = _python_block_lines(lines)
        if entries is not None:
            # File-level declarations that change what the program means go in
            # the key. The encoding cannot contain a colon, so the parts stay
            # unambiguous however the shebang is written.
            return entries, (
                f"python:{_python_encoding(lines)}:{_python_shebang(lines)}"
            )
    return _raw_block_lines(lines), _family_key(path)


def _mirror_candidates(
    sites: list[Site],
    changed_files: list[str],
    lines_of: Callable[[str], list[str]],
    lossy_of: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Find exact copies of flagged multi-line ranges.

    Python ranges are compared with comments removed; everything else is
    compared as raw text. A seed only ever meets files sharing its key, so the
    two modes never mix.
    """
    changed = set(changed_files)
    is_lossy = lossy_of if lossy_of is not None else (lambda _rel: False)
    index_cache: dict[str, tuple[list[tuple[int, str, str]], str]] = {}

    def index_of(rel: str) -> tuple[list[tuple[int, str, str]], str]:
        if rel not in index_cache:
            index_cache[rel] = _block_index(lines_of(rel), path=rel)
        return index_cache[rel]

    seeds: list[tuple[tuple[tuple[str, str], ...], str, str]] = []
    for site in sites:
        if (
            site.path not in changed
            or site.start_line is None
            or site.line is None
            or site.start_line >= site.line
        ):
            continue
        if is_lossy(site.path):
            continue
        lines = lines_of(site.path)
        if site.line > len(lines):
            continue
        entries, key = index_of(site.path)
        block = tuple(
            (text, state)
            for number, text, state in entries
            if site.start_line <= number <= site.line
        )
        if len(block) < 2 or len(block) > MAX_MIRROR_BLOCK_LINES:
            continue
        block_tokens = set().union(*(tokenize(text) for text, _ in block), set())
        significant_count = sum(is_significant_token(token) for token in block_tokens)
        if significant_count < MIN_MIRROR_SIGNIFICANT_TOKENS:
            continue
        seeds.append((
            block,
            _block_fingerprint(tuple(
                f"{state}\x00{text}" for text, state in block
            )),
            key,
        ))

    flagged_ranges = {
        (site.path, site.start_line or site.line, site.line)
        for site in sites
        if site.line is not None
    }
    candidates: dict[tuple[str, int, int], dict[str, Any]] = {}
    # (text, lexical state) of the first line -> ascending offsets. A window can
    # only match if its first line does, so this replaces the per-offset hash of
    # the whole block with one dict lookup, and the element-wise compare below
    # short-circuits on the first difference.
    head_offsets: dict[str, dict[tuple[str, str], list[int]]] = {}
    for block, fingerprint, key in seeds:
        width = len(block)
        head = block[0]
        for rel in changed_files:
            if is_lossy(rel):
                continue
            entries, rel_key = index_of(rel)
            if rel_key != key:
                continue
            if rel not in head_offsets:
                offsets: dict[tuple[str, str], list[int]] = {}
                for offset, (_number, text, state) in enumerate(entries):
                    offsets.setdefault((text, state), []).append(offset)
                head_offsets[rel] = offsets
            last_offset = len(entries) - width
            for offset in head_offsets[rel].get(head, ()):
                if offset > last_offset:
                    break
                if any(
                    entries[offset + index][1:] != block[index]
                    for index in range(1, width)
                ):
                    continue
                start, end = entries[offset][0], entries[offset + width - 1][0]
                if any(
                    rel == flagged_path
                    and start <= flagged_end
                    and flagged_start <= end
                    for flagged_path, flagged_start, flagged_end in flagged_ranges
                ):
                    continue
                window = entries[offset:offset + width]
                candidates[(rel, start, end)] = {
                    "candidateClass": "mirror",
                    "matchMode": (
                        "normalized" if key.startswith("python:") else "exact"
                    ),
                    "path": rel,
                    "line": start,
                    "endLine": end,
                    "site": f"{rel}:{start}-{end}",
                    "text": " ".join(text for _, text, _state in window)[:200],
                    "fingerprint": fingerprint,
                }
    return _merge_overlapping(list(candidates.values()))


def _spans_by_path(
    ranges: Iterable[tuple[str, int, int]],
) -> dict[str, list[tuple[int, int]]]:
    """Group ``(path, start, end)`` ranges per file, sorted and disjoint.

    Overlapping and adjacent ranges are merged so a line can be tested for
    membership with a single moving pointer. Callers rely on the disjointness:
    a pointer that can sit on an overlapping range would skip lines covered
    only by the one it stepped past.
    """
    grouped: dict[str, list[tuple[int, int]]] = {}
    for path, start, end in ranges:
        grouped.setdefault(path, []).append((start, end))
    for path, spans in grouped.items():
        merged: list[tuple[int, int]] = []
        for start, end in sorted(spans):
            if merged and start <= merged[-1][1] + 1:
                if end > merged[-1][1]:
                    merged[-1] = (merged[-1][0], end)
                continue
            merged.append((start, end))
        grouped[path] = merged
    return grouped


def _merge_overlapping(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse mirror hits that cover the same code.

    Seeds of different lengths sharing a prefix each match the same copy, and
    the range end is part of the candidate key, so one copy would otherwise be
    reported once per seed length. That overstates how many sites there are and
    can burn the candidate cap on a single edit.

    Overlapping hits are *unioned*, not deduplicated: seeds at ``1-3`` and
    ``2-4`` match at ``6-8`` and ``7-9``, and the duplicated region is ``6-9``.
    Keeping only the first would silently under-report it by a line.

    ``text`` and ``fingerprint`` stay those of the hit that opened the range --
    they are evidence of the match, while ``line``/``endLine`` describe how far
    the duplication reaches.
    """
    merged: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates,
        key=lambda item: (item["path"], item["line"], -item["endLine"]),
    ):
        if merged:
            kept = merged[-1]
            if (
                kept["path"] == candidate["path"]
                and candidate["line"] <= kept["endLine"]
            ):
                if candidate["endLine"] > kept["endLine"]:
                    kept["endLine"] = candidate["endLine"]
                    kept["site"] = f"{kept['path']}:{kept['line']}-{kept['endLine']}"
                continue
        merged.append(dict(candidate))
    return merged


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


def _read_text(path: Path) -> tuple[list[str], bool]:
    """Read a text file as ``(lines, lossy)``.

    ``lossy`` is True when the bytes were not valid UTF-8 and had to be decoded
    with replacement characters. A token sweep tolerates that -- it only wants
    identifiers, which are ASCII in practice. Exact mirror fingerprinting must
    not, because distinct source bytes all collapse onto U+FFFD and two
    genuinely different blocks would fingerprint alike.
    """
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return [], False
        data = path.read_bytes()
    except (OSError, ValueError):
        return [], False
    try:
        return data.decode("utf-8").splitlines(), False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace").splitlines(), True


def _read_lines(path: Path) -> list[str]:
    """Read a text file, or return [] for anything unreadable or oversized."""
    return _read_text(path)[0]


def _is_lossy_text(path: Path) -> bool:
    """Whether ``path`` needed replacement characters to decode as UTF-8."""
    return _read_text(path)[1]


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
    lossy_cache: dict[str, bool] = {}

    def lines_of(rel: str) -> list[str]:
        if rel not in file_cache:
            file_cache[rel] = _read_lines(root / rel)
        return file_cache[rel]

    def lossy_of(rel: str) -> bool:
        if rel not in lossy_cache:
            lossy_cache[rel] = _is_lossy_text(root / rel)
        return lossy_cache[rel]

    mirror_candidates = _mirror_candidates(located, changed_files, lines_of, lossy_of)
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
    candidates = list(mirror_candidates)
    # Both suppression sets are kept as line ranges rather than as sets of
    # lines. A range costs the same whether it covers three lines or a whole
    # file, so a reviewer anchor spanning 300k lines no longer costs 300k tuples
    # of memory; and because the ranges are sorted and disjoint, a line is
    # tested against them with one moving pointer instead of a scan of every
    # range -- linear rather than quadratic once a file holds thousands of them.
    mirror_spans = _spans_by_path(
        (c["path"], c["line"], c["endLine"]) for c in mirror_candidates
    )
    flagged_spans = _spans_by_path(
        (s.path, s.start_line or s.line, s.line) for s in located
    )

    for rel in changed_files:
        spans = mirror_spans.get(rel, ())
        flagged = flagged_spans.get(rel, ())
        cursor = 0
        flagged_cursor = 0
        for index, text in enumerate(lines_of(rel), start=1):
            # Advance before any skip, or a pointer desyncs from the line.
            while cursor < len(spans) and spans[cursor][1] < index:
                cursor += 1
            while (
                flagged_cursor < len(flagged)
                and flagged[flagged_cursor][1] < index
            ):
                flagged_cursor += 1
            if cursor < len(spans) and spans[cursor][0] <= index:
                continue
            if (
                flagged_cursor < len(flagged)
                and flagged[flagged_cursor][0] <= index
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
        # Say which rule matched. "exact" is text-identical and needs no
        # language knowledge; "normalized" means comments were ignored, which
        # only happens for a language we tokenize properly.
        class_label = (
            f"[mirror {candidate.get('matchMode', 'exact')}] "
            if candidate["candidateClass"] == "mirror"
            else ""
        )
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

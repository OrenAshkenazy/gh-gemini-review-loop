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

Exact matching is held to a stricter standard than the token sweep, because a
mirror claims two blocks are *the same code*. Anything that could make two
different blocks normalize alike is refused rather than guessed at: heredoc
payloads keep their ``#`` lines and their spacing, indentation-sensitive formats
keep their indentation, and a file that is not valid UTF-8 is skipped entirely
rather than fingerprinted through lossy replacement characters. Each guard can
only lose a mirror, never invent one.

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

_HASH_COMMENT_SUFFIXES = frozenset({
    ".py", ".rb", ".sh", ".bash", ".zsh", ".fish",
    ".yaml", ".yml", ".toml", ".ini", ".cfg",
})
_DASH_COMMENT_SUFFIXES = frozenset({".sql"})
_HASH_COMMENT_NO_SPACE_SUFFIXES = frozenset({".py", ".rb", ".toml"})

# ``//`` and ``/* */`` are allowlisted, never assumed. Treating every unknown
# suffix as C-family truncates a bare URL in prose at the ``//`` of ``https://``,
# which makes two documents that differ only in their links compare equal.
_SLASH_LINE_COMMENT_SUFFIXES = frozenset({
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
    ".cs", ".java", ".kt", ".kts", ".scala", ".swift",
    ".go", ".rs", ".dart", ".php", ".m", ".mm", ".zig",
    ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
    ".json5", ".jsonc", ".proto", ".gradle", ".groovy",
    ".scss", ".less",
})
# CSS and SQL have ``/* */`` but no ``//``: in both, ``url(https://x)`` and
# ``'https://x'`` are values, not comments.
_BLOCK_COMMENT_SUFFIXES = _SLASH_LINE_COMMENT_SUFFIXES | {".css", ".sql"}

_YAML_SUFFIXES = frozenset({".yaml", ".yml"})

# Make distinguishes a recipe line from a directive by a single leading tab, so
# a Makefile is indentation-sensitive. It is usually named rather than suffixed,
# which is why _comment_options looks at the file name too.
_MAKEFILE_NAMES = frozenset({"makefile", "gnumakefile", "bsdmakefile"})
_MAKEFILE_SUFFIXES = frozenset({".mk", ".make"})

# PostgreSQL dollar quoting: ``$$ ... $$`` or ``$tag$ ... $tag$``. The body is
# string data, so the SQL ``--`` handler must not reach inside it.
_DOLLAR_QUOTE_RE = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")

# Shells where ``<<WORD`` opens a heredoc. Inside the payload a leading ``#`` is
# data and the spacing is significant, so those lines bypass normalization
# entirely. (fish has no heredocs, so it is deliberately absent.)
_HEREDOC_SUFFIXES = frozenset({".sh", ".bash", ".zsh", ".ksh"})

# Formats where leading whitespace carries meaning -- YAML nesting depth, Python
# blocks, the layout rules of Haskell and F#, list/code-block nesting in prose.
# Collapsing it would map structurally different blocks onto one fingerprint.
_INDENTATION_SENSITIVE_SUFFIXES = frozenset({
    ".py", ".pyi",
    ".yaml", ".yml",
    ".sass", ".styl",
    ".haml", ".slim", ".pug", ".jade",
    ".coffee", ".nim", ".elm",
    ".hs", ".lhs", ".fs", ".fsx",
    ".md", ".markdown", ".rst",
})

# Any heredoc-looking redirect. The lookbehind and lookahead exclude ``<<<``
# (a here-string, which has no payload lines) and ``<<=``.
_HEREDOC_ANY_RE = re.compile(r"(?<!<)<<(?![<=])")

# The same redirect with its delimiter word captured. Bash performs quote
# removal but no expansion on the word, so the delimiter can be any shell word:
# ``EOF``, ``'EOF'``, ``$EOF``, ``\EOF``. Anything _HEREDOC_ANY_RE matches but
# this does not is an opener we cannot classify, and the caller declines.
_HEREDOC_OPEN_RE = re.compile(
    r"(?<!<)<<(?![<=])(?P<indicator>[-~]?)[ \t]*"
    r"(?P<word>(?:'[^']*'|\"[^\"]*\"|\\.|[^\s;&|<>()'\"`])+)"
)

# A YAML block scalar header: ``key: |``, ``- >-``, ``key: |2+``, optionally
# followed by a comment. Its body is data, so a ``#`` line in it is content.
_YAML_BLOCK_SCALAR_RE = re.compile(
    r"(?:^|[:\-])[ \t]*[|>](?:[0-9][+-]?|[+-][0-9]?)?[ \t]*(?:#.*)?$"
)

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


def _is_verbatim(flags: list[bool] | None, line_number: int) -> bool:
    """Whether this line must pass through normalization untouched."""
    return flags is not None and line_number < len(flags) and flags[line_number]


def _opening_delimiter(
    line: str, index: int, *, dollar_quotes: bool = False
) -> str | None:
    """The string delimiter opening at ``index``, if one does.

    Both scanners track quote state independently, so both consult this -- a
    delimiter one of them fails to recognize would let the other treat string
    data as code.
    """
    for candidate in ('"""', "'''", '"', "'", "`"):
        if line.startswith(candidate, index):
            return candidate
    if dollar_quotes:
        match = _DOLLAR_QUOTE_RE.match(line, index)
        if match:
            return match.group(0)
    return None


def _heredoc_delimiter(word: str) -> str:
    """Quote removal on a heredoc delimiter word, the way the shell does it.

    No expansion is performed, so ``<<$EOF`` terminates on a line reading
    ``$EOF``; ``<<'EOF'`` and ``<<\\EOF`` both terminate on ``EOF``.
    """
    out: list[str] = []
    index = 0
    while index < len(word):
        character = word[index]
        if character == "\\" and index + 1 < len(word):
            out.append(word[index + 1])
            index += 2
        elif character in "'\"":
            close = word.find(character, index + 1)
            if close == -1:
                out.append(word[index + 1:])
                break
            out.append(word[index + 1:close])
            index = close + 1
        else:
            out.append(character)
            index += 1
    return "".join(out)


def _heredoc_body_flags(lines: list[str]) -> list[bool]:
    """Mark the lines that are heredoc payload rather than shell code.

    A heredoc body is literal text: ``# ...`` in it is content, not a comment,
    and its spacing is part of the data. Marking those lines keeps two ``cat
    <<EOF`` blocks with different payloads from normalizing onto one
    fingerprint. The opener line itself is real code and stays unmarked; the
    terminator is kept verbatim, which is harmless.

    Detection runs on the raw line, so a ``<<`` inside a string literal can
    open a phantom heredoc. That only makes later lines verbatim, which loses
    mirror matches instead of inventing them -- the direction this whole shape
    has to err in.
    """
    flags = [False] * len(lines)
    pending: list[tuple[str, str]] = []
    active: tuple[str, str] | None = None
    for index, line in enumerate(lines):
        if active is not None:
            flags[index] = True
            delimiter, indicator = active
            if indicator == "~":
                candidate = line.strip()
            elif indicator == "-":
                candidate = line.lstrip("\t")
            else:
                candidate = line
            # Bash ends a heredoc only on a line containing the delimiter and
            # nothing else -- ``EOF   `` is payload, not a terminator. Matching
            # it loosely would end the heredoc early and expose the rest of the
            # payload to comment stripping.
            if candidate == delimiter:
                active = pending.pop(0) if pending else None
            continue
        openers = list(_HEREDOC_OPEN_RE.finditer(line))
        if len(openers) < len(_HEREDOC_ANY_RE.findall(line)):
            # A heredoc opens here but its delimiter word is not one we can
            # classify, so we cannot know where the payload ends. Refuse the
            # rest of the file rather than normalize data as code.
            for rest in range(index + 1, len(lines)):
                flags[rest] = True
            return flags
        for match in openers:
            pending.append((
                _heredoc_delimiter(match.group("word")),
                match.group("indicator"),
            ))
        if pending:
            active = pending.pop(0)
    return flags


def _yaml_block_scalar_flags(lines: list[str]) -> list[bool]:
    """Mark the lines that are YAML block-scalar body rather than YAML syntax.

    Under ``message: |`` an indented ``# ...`` line is scalar content, not a
    comment. The body runs until the first non-blank line indented no further
    than the header, which is the same rule the YAML spec uses.
    """
    flags = [False] * len(lines)
    header_indent: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if header_indent is not None:
            if not stripped:
                flags[index] = True
                continue
            if len(line) - len(line.lstrip(" \t")) > header_indent:
                flags[index] = True
                continue
            header_indent = None
        if not stripped or stripped.startswith("#"):
            continue
        if _YAML_BLOCK_SCALAR_RE.search(line):
            header_indent = len(line) - len(line.lstrip(" \t"))
    return flags


def _verbatim_flags(
    lines: list[str], *, heredocs: bool = False, yaml_scalars: bool = False
) -> list[bool] | None:
    """Lines that carry data rather than code, from every enabled source."""
    sources = []
    if heredocs:
        sources.append(_heredoc_body_flags(lines))
    if yaml_scalars:
        sources.append(_yaml_block_scalar_flags(lines))
    if not sources:
        return None
    return [any(flags[index] for flags in sources) for index in range(len(lines))]


def _strip_comments(
    lines: Iterable[str],
    *,
    hash_comments: bool = False,
    hash_comments_no_space: bool = True,
    slash_comments: bool = True,
    dash_comments: bool = False,
    block_comments: bool = True,
    dollar_quotes: bool = False,
    verbatim_flags: list[bool] | None = None,
) -> tuple[list[str], list[str]]:
    """Strip common line/block comments while preserving quoted markers.

    Returns ``(stripped, states)``. ``states[i]`` is the lexical state the line
    *began* in -- ``""`` for code, ``"block"`` inside a block comment,
    ``"str:<delim>"`` inside an unterminated literal, ``"verbatim"`` for data.
    Two lines with identical text but different states are not the same code,
    so mirror matching compares the pair rather than the text alone.

    Lines marked in ``verbatim_flags`` pass through untouched.
    """
    stripped: list[str] = []
    states: list[str] = []
    in_block = False
    quote: str | None = None
    escaped = False
    for line_number, line in enumerate(lines):
        if _is_verbatim(verbatim_flags, line_number):
            stripped.append(line)
            states.append("verbatim")
            continue
        states.append("block" if in_block else (f"str:{quote}" if quote else ""))
        output: list[str] = []
        index = 0
        while index < len(line):
            pair = line[index:index + 2]
            if in_block:
                if pair == "*/":
                    in_block = False
                    # A removed block comment separated two tokens. Without a
                    # replacement, ``account/**/name`` becomes ``accountname``
                    # and collides with an unrelated single identifier.
                    output.append(" ")
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
            delimiter = _opening_delimiter(line, index, dollar_quotes=dollar_quotes)
            if delimiter is not None:
                quote = delimiter
                output.append(delimiter)
                index += len(delimiter)
                continue
            if block_comments and pair == "/*":
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
    return stripped, states


def _collapse_whitespace(
    lines: Iterable[str],
    *,
    preserve_indentation: bool = False,
    dollar_quotes: bool = False,
    verbatim_flags: list[bool] | None = None,
) -> list[str]:
    """Collapse formatting whitespace without changing quoted text.

    Lines marked in ``verbatim_flags`` are data and pass through byte for byte
    -- including trailing spaces, which a heredoc emits verbatim, so two
    payloads differing only there must not compare equal.
    """
    normalized_lines: list[str] = []
    quote: str | None = None
    escaped = False

    for line_number, line in enumerate(lines):
        if _is_verbatim(verbatim_flags, line_number):
            normalized_lines.append(line)
            continue

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

            delimiter = _opening_delimiter(line, index, dollar_quotes=dollar_quotes)
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
    block_comments: bool = True,
    dollar_quotes: bool = False,
    preserve_indentation: bool = False,
    heredocs: bool = False,
    yaml_scalars: bool = False,
) -> tuple[str, ...]:
    """Normalize a code block for exact mirror comparison."""
    lines = list(lines)
    verbatim_flags = _verbatim_flags(
        lines, heredocs=heredocs, yaml_scalars=yaml_scalars
    )
    stripped, states = _strip_comments(
        lines,
        hash_comments=hash_comments,
        hash_comments_no_space=hash_comments_no_space,
        slash_comments=slash_comments,
        dash_comments=dash_comments,
        block_comments=block_comments,
        dollar_quotes=dollar_quotes,
        verbatim_flags=verbatim_flags,
    )
    collapsed = _collapse_whitespace(
        stripped,
        preserve_indentation=preserve_indentation,
        dollar_quotes=dollar_quotes,
        verbatim_flags=verbatim_flags,
    )
    # A blank line is formatting and drops out, but a blank line inside a
    # heredoc or block scalar is part of the payload and must survive.
    return tuple(
        line
        for line, state in zip(collapsed, states)
        if line or state == "verbatim"
    )


def _block_fingerprint(block: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(block).encode("utf-8")).hexdigest()


def _comment_options(path: str) -> dict[str, bool]:
    """Per-suffix normalization switches. Every comment style is allowlisted:
    an unrecognized format gets none of them, which costs mirrors on languages
    we have not enumerated but never invents one."""
    name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    is_makefile = (
        name in _MAKEFILE_NAMES
        or name.startswith("makefile.")
        or suffix in _MAKEFILE_SUFFIXES
    )
    return {
        "hash_comments": suffix in _HASH_COMMENT_SUFFIXES,
        "hash_comments_no_space": suffix in _HASH_COMMENT_NO_SPACE_SUFFIXES,
        "slash_comments": suffix in _SLASH_LINE_COMMENT_SUFFIXES,
        "dash_comments": suffix in _DASH_COMMENT_SUFFIXES,
        "block_comments": suffix in _BLOCK_COMMENT_SUFFIXES,
        "dollar_quotes": suffix in _DASH_COMMENT_SUFFIXES,
        "preserve_indentation": (
            suffix in _INDENTATION_SENSITIVE_SUFFIXES or is_makefile
        ),
        "heredocs": suffix in _HEREDOC_SUFFIXES,
        "yaml_scalars": suffix in _YAML_SUFFIXES,
    }


def _normalized_code_lines(
    lines: list[str],
    *,
    path: str,
) -> list[tuple[int, str, str]]:
    """``(line number, normalized text, lexical state)`` for each kept line."""
    options = _comment_options(path)
    verbatim_flags = _verbatim_flags(
        lines,
        heredocs=options["heredocs"],
        yaml_scalars=options["yaml_scalars"],
    )
    stripped, states = _strip_comments(
        lines,
        hash_comments=options["hash_comments"],
        hash_comments_no_space=options["hash_comments_no_space"],
        slash_comments=options["slash_comments"],
        dash_comments=options["dash_comments"],
        block_comments=options["block_comments"],
        dollar_quotes=options["dollar_quotes"],
        verbatim_flags=verbatim_flags,
    )
    collapsed = _collapse_whitespace(
        stripped,
        preserve_indentation=options["preserve_indentation"],
        dollar_quotes=options["dollar_quotes"],
        verbatim_flags=verbatim_flags,
    )
    return [
        (number, normalized, state)
        for number, (normalized, state) in enumerate(zip(collapsed, states), start=1)
        if normalized or state == "verbatim"
    ]


def _mirror_candidates(
    sites: list[Site],
    changed_files: list[str],
    lines_of: Callable[[str], list[str]],
    lossy_of: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """Find exact normalized copies of flagged multi-line ranges."""
    changed = set(changed_files)
    is_lossy = lossy_of if lossy_of is not None else (lambda _rel: False)
    seeds: list[tuple[tuple[tuple[str, str], ...], str]] = []
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
        normalized_source = _normalized_code_lines(lines, path=site.path)
        block = tuple(
            (text, state)
            for number, text, state in normalized_source
            if site.start_line <= number <= site.line
        )
        if len(block) < 2 or len(block) > MAX_MIRROR_BLOCK_LINES:
            continue
        block_tokens = set().union(*(tokenize(text) for text, _ in block), set())
        significant_count = sum(is_significant_token(token) for token in block_tokens)
        if significant_count < MIN_MIRROR_SIGNIFICANT_TOKENS:
            continue
        seeds.append((block, _block_fingerprint(tuple(
            f"{state}\x00{text}" for text, state in block
        ))))

    flagged_ranges = {
        (site.path, site.start_line or site.line, site.line)
        for site in sites
        if site.line is not None
    }
    candidates: dict[tuple[str, int, int], dict[str, Any]] = {}
    normalized_files: dict[str, list[tuple[int, str, str]]] = {}
    # normalized (text, lexical state) of the first line -> ascending offsets. A
    # window can only match if its first line does, so this replaces the
    # per-offset hash of the whole block with one dict lookup, and the
    # element-wise compare below short-circuits on the first difference.
    head_offsets: dict[str, dict[tuple[str, str], list[int]]] = {}
    for block, fingerprint in seeds:
        width = len(block)
        head = block[0]
        for rel in changed_files:
            if is_lossy(rel):
                continue
            if rel not in normalized_files:
                entries = _normalized_code_lines(lines_of(rel), path=rel)
                normalized_files[rel] = entries
                offsets: dict[tuple[str, str], list[int]] = {}
                for offset, (_number, text, state) in enumerate(entries):
                    offsets.setdefault((text, state), []).append(offset)
                head_offsets[rel] = offsets
            entries = normalized_files[rel]
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
                    "path": rel,
                    "line": start,
                    "endLine": end,
                    "site": f"{rel}:{start}-{end}",
                    "text": " ".join(text for _, text, _state in window)[:200],
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

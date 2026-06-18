# MergeProof Production Context Pack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-repo "Production Context Pack" and a readiness phase that chains automatically off the existing Gemini review loop's terminal state, answering "is this PR safe to merge in production?"

**Architecture:** `mergeproof.yaml` in the app repo (read from the trusted base ref) declares an infra repo + an allowlisted set of paths. MergeProof resolves each source ref to an immutable SHA, fetches only the allowlisted files (with file-count / size / binary limits), extracts normalized facts via the existing scanner, and assembles a stable Production Context Pack. PR changed files are overlaid as a risk overlay to render the Readiness Card. A phase orchestrator implements gating (skip when config absent, `CONFIG_CHANGED_REVIEW_REQUIRED` when the PR edits config, `VERIFICATION_FAILED` precedence) and chains the existing scripts.

**Tech Stack:** Python 3.9+ stdlib only (zero dependency — no PyYAML), `gh` CLI for GitHub access (isolated behind an injectable runner), pytest.

**Spec:** `docs/superpowers/specs/2026-06-14-mergeproof-production-context-pack-design.md`

**Conventions (match existing scripts):**
- `from __future__ import annotations` at the top of every module.
- CLI modules expose `main(argv: list[str] | None = None) -> int`; `--json` prints JSON only to stdout (no ANSI, no `[loop]`), human text to stderr or non-`--json` paths.
- All GitHub calls go through an injected `runner(args: list[str]) -> Any` that runs `gh <args>` and returns parsed JSON; tests pass fakes.
- Tests live in `tests/` and import modules by bare name (conftest.py puts the scripts dir on `sys.path`).
- Test runner: `/opt/homebrew/bin/pytest`.

**Path shorthands used below:**
- `SCRIPTS = plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts`

---

## Task 1: mergeproof config parser (`mergeproof_config.py`)

Zero-dependency parser for `mergeproof.json` and the strict `mergeproof.yaml` subset, plus schema validation. Rejects anchors, aliases, merge keys, inline/flow maps, multiline strings, and duplicate keys.

**Files:**
- Create: `SCRIPTS/mergeproof_config.py`
- Test: `tests/test_mergeproof_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mergeproof_config.py
"""Tests for the zero-dependency mergeproof config parser."""

from __future__ import annotations

import pytest

import mergeproof_config as mc

YAML_OK = """\
version: 1
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/aegislocal-api/**
      - modules/kong/**
limits:
  max_files: 50
  max_file_bytes: 1024
"""

JSON_OK = """\
{"version": 1, "service": "aegislocal-api",
 "architecture_sources": [{"repo": "acme/infra", "ref": "main",
   "allow": ["envs/prod/aegislocal-api/**", "modules/kong/**"]}],
 "limits": {"max_files": 50, "max_file_bytes": 1024}}
"""


def test_yaml_and_json_parse_to_same_structure():
    from_yaml = mc.load_config(YAML_OK, fmt="yaml")
    from_json = mc.load_config(JSON_OK, fmt="json")
    assert from_yaml == from_json
    assert from_yaml["service"] == "aegislocal-api"
    assert from_yaml["architecture_sources"][0]["repo"] == "acme/infra"
    assert from_yaml["architecture_sources"][0]["allow"] == [
        "envs/prod/aegislocal-api/**",
        "modules/kong/**",
    ]
    assert from_yaml["limits"] == {"max_files": 50, "max_file_bytes": 1024}


def test_defaults_applied_when_limits_absent():
    text = """\
service: s
architecture_sources:
  - repo: o/r
    allow:
      - a/**
"""
    cfg = mc.load_config(text, fmt="yaml")
    assert cfg["limits"] == {"max_files": 200, "max_file_bytes": 262144}
    assert cfg["architecture_sources"][0]["ref"] == "main"


def test_duplicate_keys_rejected():
    text = "service: a\nservice: b\narchitecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="[Dd]uplicate"):
        mc.load_config(text, fmt="yaml")


@pytest.mark.parametrize(
    "value",
    [
        "service: &anchor x",
        "service: *alias",
        "service: {a: 1}",
        "service: |\n  multi\n  line",
    ],
)
def test_unsupported_yaml_syntax_rejected(value):
    text = value + "\narchitecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="Unsupported mergeproof.yaml syntax"):
        mc.load_config(text, fmt="yaml")


def test_missing_service_rejected():
    text = "architecture_sources:\n  - repo: o/r\n    allow:\n      - a\n"
    with pytest.raises(mc.MergeProofConfigError, match="service"):
        mc.load_config(text, fmt="yaml")


def test_empty_or_bad_sources_rejected():
    with pytest.raises(mc.MergeProofConfigError, match="architecture_sources"):
        mc.load_config("service: s\narchitecture_sources: []\n", fmt="yaml")
    with pytest.raises(mc.MergeProofConfigError, match="allow"):
        mc.load_config(
            "service: s\narchitecture_sources:\n  - repo: o/r\n", fmt="yaml"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_mergeproof_config.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mergeproof_config'`

- [ ] **Step 3: Write the implementation**

```python
# SCRIPTS/mergeproof_config.py
#!/usr/bin/env python3
"""Zero-dependency parser/validator for mergeproof config.

Supports `mergeproof.json` (canonical machine format) and a strict subset of
`mergeproof.yaml` (human-friendly). The YAML subset covers block maps, block
lists, and simple scalars (strings, ints, booleans, null) with comments.
Anchors, aliases, merge keys, inline/flow collections, multiline strings, and
duplicate keys are rejected so config stays predictable and safe.
"""

from __future__ import annotations

import json
import re
from typing import Any

_SYNTAX_ERR = (
    "Unsupported mergeproof.yaml syntax. Use the documented subset or mergeproof.json."
)


class MergeProofConfigError(ValueError):
    """Raised for malformed or unsupported mergeproof config."""


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


def _scalar(token: str) -> Any:
    token = token.strip()
    if token == "":
        return None
    if token[0] in "{[" or token[0] in "&*" or token.startswith("<<") or token in ("|", ">"):
        raise MergeProofConfigError(_SYNTAX_ERR)
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    low = token.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~"):
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
        indent = len(stripped) - len(stripped.lstrip(" "))
        if "\t" in stripped[:indent]:
            raise MergeProofConfigError(_SYNTAX_ERR)
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
        if rest == "":
            if idx + 1 < len(lines) and lines[idx + 1][0] > indent:
                child, idx = _parse_block(lines, idx + 1, lines[idx + 1][0])
                result[key] = child
            else:
                result[key] = None
                idx += 1
        else:
            result[key] = _scalar(rest)
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
            else:
                result.append(None)
                idx += 1
        elif re.match(r"[^:\s][^:]*:(\s|$)", item):
            lines[idx] = (indent + 2, item)
            child, idx = _parse_map(lines, idx, indent + 2)
            result.append(child)
        else:
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
    norm: list[dict[str, Any]] = []
    for src in sources:
        if not isinstance(src, dict):
            raise MergeProofConfigError("each architecture source must be a mapping")
        repo = src.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise MergeProofConfigError("source 'repo' must be in OWNER/REPO format")
        allow = src.get("allow")
        if not isinstance(allow, list) or not allow or not all(
            isinstance(a, str) and a for a in allow
        ):
            raise MergeProofConfigError("source 'allow' must be a non-empty list of path globs")
        norm.append({"repo": repo, "ref": str(src.get("ref", "main")), "allow": list(allow)})
    limits = data.get("limits") or {}
    if not isinstance(limits, dict):
        raise MergeProofConfigError("'limits' must be a mapping")
    return {
        "version": data.get("version", 1),
        "service": service,
        "architecture_sources": norm,
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
    elif fmt == "yaml":
        data = parse_yaml_subset(text)
    else:
        raise MergeProofConfigError(f"unknown config format: {fmt!r}")
    return validate_config(data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_mergeproof_config.py -q`
Expected: PASS (all tests green)

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/mergeproof_config.py tests/test_mergeproof_config.py
git commit -m "feat: zero-dependency mergeproof config parser"
```
(Replace `SCRIPTS` with the full path above; same for every task.)

---

## Task 2: extract facts from an in-memory file set (`architecture_context.py` refactor)

Split `scan(dir)` so the fact extraction can run over fetched infra files (a `{path: text}` mapping), not just a local directory. Behavior for the existing local path must not change.

**Files:**
- Modify: `SCRIPTS/architecture_context.py` (the `scan` function near the bottom)
- Test: `tests/test_architecture_context.py` (add cases)

- [ ] **Step 1: Write the failing tests (append to existing file)**

```python
def test_extract_facts_from_mapping():
    files = {
        "k8s/deployment.yaml": "kind: Deployment\n",
        "k8s/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
        "terraform/sqs.tf": 'resource "aws_sqs_queue" "scan" {\n  name = "scan-events"\n}\n',
    }

    facts = ac.extract_facts(files, files_found=sorted(files))

    assert facts["runtime"] == "kubernetes"
    assert facts["exposure"] == "public"
    assert "sqs:scan-events" in facts["queues"]
    assert facts["architecture_files_found"] == sorted(files)


def test_scan_still_matches_extract_facts(tmp_path):
    _write(tmp_path, "k8s/deployment.yaml", "kind: Deployment\n")
    from_scan = ac.scan(tmp_path)
    from_extract = ac.extract_facts(
        {"k8s/deployment.yaml": "kind: Deployment\n"},
        files_found=["k8s/deployment.yaml"],
    )
    assert from_scan == from_extract
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_architecture_context.py -q`
Expected: FAIL — `AttributeError: module 'architecture_context' has no attribute 'extract_facts'`

- [ ] **Step 3: Refactor `scan` into `scan` + `extract_facts`**

Replace the existing `def scan(repo_root...)` function body with the two functions below (the per-detector helper calls are unchanged; only the split is new):

```python
def extract_facts(
    files: dict[str, str], *, files_found: list[str] | None = None
) -> dict[str, Any]:
    """Derive the architecture fact sheet from a ``{relpath: text}`` mapping."""
    blob = "\n".join(files.values())

    service_name = _detect_service_name(files)
    owners = _detect_owners(files)
    runtime = _detect_runtime(files)
    deployment_type = _detect_deployment_type(files, blob)
    ingress = _detect_ingress(blob)
    exposure = _detect_exposure(blob, ingress)
    datastores = _detect_datastores(blob)
    queues = _detect_queues(files, blob)
    external = _detect_external_dependencies(blob)
    secrets = _detect_secrets_or_env(blob)
    resource_limits = _detect_resource_limits(files)
    verification_commands = _detect_verification_commands(files)
    sensitive_surfaces = _derive_sensitive_surfaces(
        exposure, queues, datastores, secrets, blob
    )

    strong_fields = sum(
        [
            service_name != "unknown",
            bool(owners),
            runtime != "unknown",
            exposure != "unknown",
            bool(datastores),
        ]
    )
    found = list(files_found) if files_found is not None else sorted(files)

    return {
        "service_name": service_name,
        "owners": owners,
        "runtime": runtime,
        "deployment_type": deployment_type,
        "exposure": exposure,
        "ingress": ingress,
        "datastores": datastores,
        "queues": queues,
        "external_dependencies": external,
        "secrets_or_env": secrets,
        "resource_limits": resource_limits,
        "verification_commands": verification_commands,
        "sensitive_surfaces": sensitive_surfaces,
        "architecture_files_found": found,
        "confidence": _derive_confidence(len(found), strong_fields),
    }


def scan(repo_root: str | Path) -> dict[str, Any]:
    """Scan *repo_root* and return a best-effort architecture context dict."""
    root = Path(repo_root)
    collected = _collect_files(root) if root.is_dir() else []
    files = dict(collected)
    return extract_facts(files, files_found=[rel for rel, _ in collected])
```

- [ ] **Step 4: Run the full architecture_context suite**

Run: `/opt/homebrew/bin/pytest tests/test_architecture_context.py -q`
Expected: PASS (original 8 + 2 new)

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/architecture_context.py tests/test_architecture_context.py
git commit -m "refactor: expose extract_facts over an in-memory file set"
```

---

## Task 3: fetch allowlisted infra files (`fetch_infra_files.py`)

Glob-filter a recursively listed tree, enforce `max_files`/`max_file_bytes`, skip binaries, and decode base64 contents. GitHub calls go through an injected runner.

**Files:**
- Create: `SCRIPTS/fetch_infra_files.py`
- Test: `tests/test_fetch_infra_files.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fetch_infra_files.py
"""Tests for allowlisted infra file fetching."""

from __future__ import annotations

import base64

import fetch_infra_files as fif


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class FakeGH:
    """Returns canned tree + contents responses; records calls."""

    def __init__(self, tree, contents, truncated=False):
        self.tree = tree            # list of {"path","type","size"}
        self.contents = contents    # {path: payload dict}
        self.truncated = truncated
        self.calls = []

    def __call__(self, args):
        self.calls.append(args)
        url = args[-1]
        if "/git/trees/" in url:
            return {"tree": self.tree, "truncated": self.truncated}
        for path, payload in self.contents.items():
            if f"/contents/{path}?" in url:
                return payload
        raise RuntimeError(f"not found: {url}")


def test_glob_matches_double_star():
    assert fif.path_matches("envs/prod/api/deploy.yaml", ["envs/prod/**"])
    assert fif.path_matches("modules/kong/main.tf", ["modules/kong/**"])
    assert not fif.path_matches("modules/redis/main.tf", ["modules/kong/**"])
    assert fif.path_matches("a/b.tf", ["**/*.tf"])


def test_fetches_only_allowlisted_files():
    gh = FakeGH(
        tree=[
            {"path": "envs/prod/api/deploy.yaml", "type": "blob", "size": 20},
            {"path": "secrets/private.txt", "type": "blob", "size": 20},
        ],
        contents={"envs/prod/api/deploy.yaml": {"encoding": "base64", "content": _b64("kind: Deployment")}},
    )
    src = {"repo": "o/infra", "resolved_sha": "sha1", "allow": ["envs/prod/**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    assert list(result["files"]) == ["envs/prod/api/deploy.yaml"]
    assert result["fetched_paths"] == ["envs/prod/api/deploy.yaml"]


def test_max_files_caps_and_records_overflow():
    tree = [{"path": f"envs/prod/f{i}.yaml", "type": "blob", "size": 5} for i in range(3)]
    contents = {f"envs/prod/f{i}.yaml": {"encoding": "base64", "content": _b64("x")} for i in range(3)}
    gh = FakeGH(tree=tree, contents=contents)
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["envs/prod/**"]}
    result = fif.fetch_infra_files(src, max_files=2, max_file_bytes=1024, runner=gh)
    assert len(result["files"]) == 2
    assert any(s["reason"] == "over_max_files" for s in result["skipped"])


def test_too_large_and_binary_skipped():
    gh = FakeGH(
        tree=[
            {"path": "envs/big.yaml", "type": "blob", "size": 99999},
            {"path": "envs/bin.bin", "type": "blob", "size": 5},
        ],
        contents={"envs/bin.bin": {"encoding": "base64", "content": base64.b64encode(b"a\x00b").decode("ascii")}},
    )
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["envs/**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    reasons = {s["path"]: s["reason"] for s in result["skipped"]}
    assert reasons["envs/big.yaml"] == "too_large"
    assert reasons["envs/bin.bin"] == "binary"
    assert result["files"] == {}


def test_truncated_tree_flagged():
    gh = FakeGH(tree=[], contents={}, truncated=True)
    src = {"repo": "o/infra", "resolved_sha": "s", "allow": ["**"]}
    result = fif.fetch_infra_files(src, max_files=200, max_file_bytes=1024, runner=gh)
    assert result["truncated"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_infra_files.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'fetch_infra_files'`

- [ ] **Step 3: Write the implementation**

```python
# SCRIPTS/fetch_infra_files.py
#!/usr/bin/env python3
"""Fetch the allowlisted slice of an infra repo, with hard safety limits.

Lists the repo tree once at a resolved SHA, filters paths by the config's
allow globs, then fetches each matching blob — enforcing a max file count, a
max per-file byte size, and skipping binaries. Returns only decoded text plus a
skip report. GitHub access is isolated behind an injected ``runner``.
"""

from __future__ import annotations

import base64
import re
from typing import Any, Callable

Runner = Callable[[list[str]], Any]

_BINARY_EXT = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".tar",
               ".ico", ".woff", ".woff2", ".ttf", ".jar", ".so", ".bin")


def _glob_to_regex(glob: str) -> re.Pattern[str]:
    parts: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        if glob[i:i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif glob[i:i + 2] == "**":
            parts.append(".*")
            i += 2
        elif glob[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(glob[i]))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def path_matches(path: str, patterns: list[str]) -> bool:
    return any(_glob_to_regex(p).match(path) for p in patterns)


def _decode_content(payload: Any) -> str | None:
    """Decode a GitHub contents payload to text, or None if binary/unusable."""
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None
    try:
        raw = base64.b64decode(payload.get("content", "") or "")
    except (ValueError, TypeError):
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
    truncated = bool(tree.get("truncated")) if isinstance(tree, dict) else False
    blobs = [
        t for t in (tree.get("tree", []) if isinstance(tree, dict) else [])
        if t.get("type") == "blob"
    ]
    matched = [t for t in blobs if path_matches(t.get("path", ""), allow)]

    files: dict[str, str] = {}
    skipped: list[dict[str, str]] = []

    for t in matched[max_files:]:
        skipped.append({"path": t["path"], "reason": "over_max_files"})

    for t in matched[:max_files]:
        path = t["path"]
        if t.get("size", 0) and t["size"] > max_file_bytes:
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
        "truncated": truncated,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_fetch_infra_files.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/fetch_infra_files.py tests/test_fetch_infra_files.py
git commit -m "feat: fetch allowlisted infra files with safety limits"
```

---

## Task 4: resolve config from the trusted base ref (`resolve_mergeproof.py`)

Resolve the PR's base SHA, fetch `mergeproof.{json,yaml,yml}` at that trusted ref, detect PR-modified config, and resolve each source ref to an immutable SHA.

**Files:**
- Create: `SCRIPTS/resolve_mergeproof.py`
- Test: `tests/test_resolve_mergeproof.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_resolve_mergeproof.py
"""Tests for trusted-ref config resolution."""

from __future__ import annotations

import base64

import resolve_mergeproof as rm


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""


class FakeGH:
    def __init__(self, base_sha="basesha", config=("mergeproof.yaml", CONFIG_YAML),
                 source_sha="resolvedsha", missing=False):
        self.base_sha = base_sha
        self.config = config
        self.source_sha = source_sha
        self.missing = missing
        self.config_ref_seen = None

    def __call__(self, args):
        url = args[-1]
        if "/pulls/" in url:
            return {"base": {"ref": "main", "sha": self.base_sha}}
        if "/contents/mergeproof" in url:
            self.config_ref_seen = url.split("ref=")[-1]
            path = url.split("/contents/")[1].split("?")[0]
            if self.missing or path != self.config[0]:
                raise RuntimeError("404 Not Found")
            return {"encoding": "base64", "content": _b64(self.config[1])}
        if "/commits/" in url:
            return {"sha": self.source_sha}
        raise RuntimeError(f"unexpected: {url}")


def test_reads_config_from_base_sha():
    gh = FakeGH()
    result = rm.resolve("acme/app", 7, changed_files=[], runner=gh)
    assert result["status"] == "OK"
    assert gh.config_ref_seen == "basesha"
    assert result["config"]["service"] == "aegislocal-api"
    assert result["config"]["architecture_sources"][0]["resolved_sha"] == "resolvedsha"
    assert result["config_changed"] is False


def test_pr_modified_config_uses_base_and_flags():
    gh = FakeGH()
    result = rm.resolve("acme/app", 7, changed_files=["mergeproof.yaml", "src/x.py"], runner=gh)
    assert result["status"] == "CONFIG_CHANGED_REVIEW_REQUIRED"
    assert result["config_changed"] is True
    assert gh.config_ref_seen == "basesha"  # still base, not PR head


def test_trust_pr_config_reads_pr_head():
    gh = FakeGH()
    result = rm.resolve(
        "acme/app", 7, changed_files=["mergeproof.yaml"], runner=gh, trust_pr_config=True,
        pr_head_sha="prheadsha",
    )
    assert gh.config_ref_seen == "prheadsha"
    assert result["status"] == "OK"


def test_missing_config_returns_missing_status():
    gh = FakeGH(missing=True)
    result = rm.resolve("acme/app", 7, changed_files=[], runner=gh)
    assert result["status"] == "MISSING"
    assert result["config"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_resolve_mergeproof.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'resolve_mergeproof'`

- [ ] **Step 3: Write the implementation**

```python
# SCRIPTS/resolve_mergeproof.py
#!/usr/bin/env python3
"""Resolve mergeproof config from the trusted base ref of a PR.

A PR can edit mergeproof config to widen what infra MergeProof reads, so the
config is read from the PR's immutable base SHA by default. If the PR's changed
files include a config file, the run is flagged CONFIG_CHANGED_REVIEW_REQUIRED
and still uses the base config. `--trust-pr-config` (off by default) reads from
the PR head instead. Each source ref is resolved to an immutable commit SHA.
"""

from __future__ import annotations

from typing import Any, Callable

from fetch_infra_files import _decode_content
from mergeproof_config import load_config

Runner = Callable[[list[str]], Any]

CONFIG_PATHS = ("mergeproof.json", "mergeproof.yaml", "mergeproof.yml")


def _fmt_for(path: str) -> str:
    return "json" if path.endswith(".json") else "yaml"


def fetch_config(repo: str, ref: str, runner: Runner) -> tuple[dict[str, Any], str] | None:
    """Return (validated_config, path) for the first config file found at *ref*."""
    for path in CONFIG_PATHS:
        try:
            payload = runner(["api", f"repos/{repo}/contents/{path}?ref={ref}"])
        except RuntimeError:
            continue
        text = _decode_content(payload)
        if text is None:
            continue
        return load_config(text, fmt=_fmt_for(path)), path
    return None


def resolve(
    app_repo: str,
    pr_number: int,
    changed_files: list[str],
    *,
    runner: Runner,
    trust_pr_config: bool = False,
    pr_head_sha: str | None = None,
) -> dict[str, Any]:
    pr = runner(["api", f"repos/{app_repo}/pulls/{pr_number}"])
    base_sha = pr["base"]["sha"]
    config_ref = pr_head_sha if trust_pr_config else base_sha

    config_changed = any(f in CONFIG_PATHS for f in changed_files)

    found = fetch_config(app_repo, config_ref, runner)
    if found is None:
        return {"status": "MISSING", "config": None, "config_changed": config_changed,
                "config_path": None, "config_ref": config_ref}

    config, path = found
    for src in config["architecture_sources"]:
        commit = runner(["api", f"repos/{src['repo']}/commits/{src['ref']}"])
        src["resolved_sha"] = commit["sha"]

    if config_changed and not trust_pr_config:
        status = "CONFIG_CHANGED_REVIEW_REQUIRED"
    else:
        status = "OK"

    return {"status": status, "config": config, "config_changed": config_changed,
            "config_path": path, "config_ref": config_ref}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_resolve_mergeproof.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/resolve_mergeproof.py tests/test_resolve_mergeproof.py
git commit -m "feat: resolve mergeproof config from trusted base ref"
```

---

## Task 5: assemble the Production Context Pack (`build_context_pack.py`)

Orchestrate resolve → fetch → extract into a stable pack. Emits the pack JSON only (no markdown, no changed files inside the pack). Partial/inaccessible sources are recorded, never fatal.

**Files:**
- Create: `SCRIPTS/build_context_pack.py`
- Test: `tests/test_build_context_pack.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_build_context_pack.py
"""Tests for Production Context Pack assembly."""

from __future__ import annotations

import base64

import build_context_pack as bcp


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""


class FakeGH:
    def __init__(self, infra_files, base_sha="base", source_sha="isha", fail_infra=False):
        self.infra_files = infra_files
        self.base_sha = base_sha
        self.source_sha = source_sha
        self.fail_infra = fail_infra

    def __call__(self, args):
        url = args[-1]
        if "/pulls/" in url:
            return {"base": {"ref": "main", "sha": self.base_sha}}
        if "/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(CONFIG_YAML)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": self.source_sha}
        if "/git/trees/" in url:
            if self.fail_infra:
                raise RuntimeError("403 Forbidden")
            return {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in self.infra_files.items()
            ]}
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra_files[path])}
        raise RuntimeError(f"unexpected {url}")


INFRA = {
    "envs/prod/deploy.yaml": "kind: Deployment\n",
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
    "envs/prod/sqs.tf": 'resource "aws_sqs_queue" "scan" {\n  name = "scan-events"\n}\n',
}


def test_pack_has_facts_provenance_and_safety():
    gh = FakeGH(INFRA)
    pack = bcp.build_pack("acme/app", 7, changed_files=[], runner=gh, now_iso="2026-06-14T00:00:00Z")
    assert pack["service"] == "aegislocal-api"
    assert pack["facts"]["runtime"] == "kubernetes"
    assert "sqs:scan-events" in pack["facts"]["queues"]
    assert pack["provenance"]["file_count"] == 3
    assert pack["provenance"]["sources"][0]["resolved_sha"] == "isha"
    assert pack["safety"]["secrets_redacted"] is True
    assert pack["safety"]["config_changed"] is False


def test_pack_never_contains_raw_file_contents_or_changed_files():
    gh = FakeGH(INFRA)
    pack = bcp.build_pack("acme/app", 7, changed_files=["core/api/routes.py"], runner=gh)
    text = repr(pack)
    assert "kind: Deployment" not in text          # no raw infra contents
    assert "aws_sqs_queue" not in text             # no raw terraform
    assert "core/api/routes.py" not in text        # changed files not in pack


def test_inaccessible_source_records_failure_not_crash():
    gh = FakeGH(INFRA, fail_infra=True)
    pack = bcp.build_pack("acme/app", 7, changed_files=[], runner=gh)
    assert pack["provenance"]["file_count"] == 0
    assert pack["safety"]["failed_sources"]


def test_missing_config_returns_none():
    class NoConfig(FakeGH):
        def __call__(self, args):
            if "/contents/mergeproof" in args[-1]:
                raise RuntimeError("404")
            return super().__call__(args)
    pack = bcp.build_pack("acme/app", 7, changed_files=[], runner=NoConfig(INFRA))
    assert pack is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_build_context_pack.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'build_context_pack'`

- [ ] **Step 3: Write the implementation**

```python
# SCRIPTS/build_context_pack.py
#!/usr/bin/env python3
"""Assemble a stable Production Context Pack (service nutrition label).

Resolves trusted config, fetches the allowlisted infra slice, and extracts
normalized facts. Emits the pack JSON only — never raw file contents, never
secret values, never the PR's changed files (those drive the separate risk
overlay). Inaccessible sources are recorded in the safety report, not fatal.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from architecture_context import extract_facts
from fetch_infra_files import fetch_infra_files
from pr_architecture_risk import _default_pr_runner, fetch_pr_changed_files, parse_pr
from resolve_mergeproof import resolve

Runner = Callable[[list[str]], Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_pack(
    app_repo: str,
    pr_number: int,
    changed_files: list[str],
    *,
    runner: Runner = _default_pr_runner,
    trust_pr_config: bool = False,
    now_iso: str | None = None,
) -> dict[str, Any] | None:
    """Return the Production Context Pack, or None if config is absent."""
    resolution = resolve(
        app_repo, pr_number, changed_files, runner=runner, trust_pr_config=trust_pr_config
    )
    if resolution["status"] == "MISSING":
        return None

    config = resolution["config"]
    limits = config["limits"]
    all_files: dict[str, str] = {}
    sources_meta: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    truncated = False

    for src in config["architecture_sources"]:
        try:
            res = fetch_infra_files(
                src,
                max_files=limits["max_files"],
                max_file_bytes=limits["max_file_bytes"],
                runner=runner,
            )
        except RuntimeError as exc:
            failed.append({"repo": src["repo"], "error": str(exc)})
            continue
        all_files.update(res["files"])
        skipped.extend(res["skipped"])
        truncated = truncated or res["truncated"]
        sources_meta.append({
            "repo": src["repo"],
            "ref": src["ref"],
            "resolved_sha": src["resolved_sha"],
            "files": res["fetched_paths"],
        })

    facts = extract_facts(all_files, files_found=sorted(all_files))

    return {
        "service": config["service"],
        "facts": facts,
        "provenance": {
            "sources": sources_meta,
            "fetched_at": now_iso or _now_iso(),
            "file_count": len(all_files),
        },
        "safety": {
            "limits": limits,
            "skipped": skipped,
            "tree_truncated": truncated,
            "failed_sources": failed,
            "secrets_redacted": True,
            "config_changed": resolution["config_changed"],
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a Production Context Pack for a PR.")
    parser.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    parser.add_argument("--trust-pr-config", action="store_true",
                        help="Read mergeproof config from PR head instead of the trusted base ref.")
    parser.add_argument("--json", action="store_true", dest="json_output",
                        help="Print the pack JSON on stdout.")
    parser.add_argument("--output", help="Write the pack JSON to this path.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        repo, number = parse_pr(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        changed = fetch_pr_changed_files(repo, number)
        pack = build_pack(repo, number, changed, trust_pr_config=args.trust_pr_config)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if pack is None:
        print("[mergeproof] readiness skipped\nReason: mergeproof.yaml not found", file=sys.stderr)
        return 0

    payload = json.dumps(pack, indent=2, sort_keys=True)
    if args.output:
        from pathlib import Path
        Path(args.output).write_text(payload + "\n", encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_build_context_pack.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/build_context_pack.py tests/test_build_context_pack.py
git commit -m "feat: assemble Production Context Pack from allowlisted infra"
```

---

## Task 6: pack-aware readiness card (`render_pr_readiness.py` changes)

Let `build_readiness` accept a pack (facts + provenance + safety), add the `CONFIG_CHANGED_REVIEW_REQUIRED` status (precedence just below `VERIFICATION_FAILED`), and render a provenance line. Existing callers passing a flat facts dict are unaffected.

**Files:**
- Modify: `SCRIPTS/render_pr_readiness.py`
- Test: `tests/test_render_pr_readiness.py` (add cases)

- [ ] **Step 1: Write the failing tests (append)**

```python
PACK = {
    "service": "aegislocal-api",
    "facts": ARCH,
    "provenance": {"sources": [{"repo": "acme/infra", "resolved_sha": "abc1234",
                                "files": ["envs/prod/deploy.yaml"]}],
                   "fetched_at": "2026-06-14T00:00:00Z", "file_count": 1},
    "safety": {"config_changed": False, "secrets_redacted": True},
}


def test_pack_input_is_unwrapped_for_architecture():
    data = rpr.build_readiness(LOOP_SUMMARY, PACK, RISKS_HIGH)
    assert data["architecture"]["service_name"] == "aegislocal-api"
    assert data["status"] == "HUMAN_DECISION_REQUIRED"


def test_config_changed_sets_review_required_status():
    pack = {**PACK, "safety": {"config_changed": True}}
    data = rpr.build_readiness(LOOP_SUMMARY, pack, RISKS_NONE)
    assert data["status"] == "CONFIG_CHANGED_REVIEW_REQUIRED"


def test_verification_failed_outranks_config_changed():
    pack = {**PACK, "safety": {"config_changed": True}}
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    data = rpr.build_readiness(summary, pack, RISKS_HIGH)
    assert data["status"] == "VERIFICATION_FAILED"


def test_markdown_shows_provenance_line():
    md = rpr.render_markdown(rpr.build_readiness(LOOP_SUMMARY, PACK, RISKS_HIGH))
    assert "acme/infra@abc1234" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -q`
Expected: FAIL — `test_config_changed_sets_review_required_status` (status is HUMAN_DECISION_REQUIRED / READY) and `test_markdown_shows_provenance_line`.

- [ ] **Step 3: Add status entries (constants near the top of the module)**

Add to `STATUS_LABELS`:
```python
    "CONFIG_CHANGED_REVIEW_REQUIRED": "CONFIG CHANGED — REVIEW REQUIRED",
```
Add to `_NEXT_OPTIONS`:
```python
    "CONFIG_CHANGED_REVIEW_REQUIRED": [
        "Review the mergeproof config change before merge",
        "Confirm the new infra paths are intended",
        "Merge only after the config change is approved",
    ],
```
Add to `_REASONS`:
```python
    "CONFIG_CHANGED_REVIEW_REQUIRED": (
        "This PR modifies the MergeProof config. The base-branch config was used "
        "for this run; review the config change before it affects production "
        "context resolution."
    ),
```

- [ ] **Step 4: Unwrap the pack and apply the new status in `build_readiness`**

At the top of `build_readiness`, after the `production_risks = production_risks or {}` line, add:
```python
    architecture, provenance, safety = _unwrap_architecture(architecture)
```
Add this helper above `build_readiness`:
```python
def _unwrap_architecture(architecture: dict[str, Any]) -> tuple[dict, dict | None, dict]:
    """Accept either a flat facts dict or a full Production Context Pack."""
    if isinstance(architecture, dict) and "facts" in architecture and "provenance" in architecture:
        return (
            architecture.get("facts") or {},
            architecture.get("provenance"),
            architecture.get("safety") or {},
        )
    return architecture or {}, None, {}
```
Replace the status `if/elif` chain with (inserting the config-changed branch):
```python
    if verification == "failed":
        status = "VERIFICATION_FAILED"
        reason = _REASONS["VERIFICATION_FAILED"]
    elif safety.get("config_changed"):
        status = "CONFIG_CHANGED_REVIEW_REQUIRED"
        reason = _REASONS["CONFIG_CHANGED_REVIEW_REQUIRED"]
    elif human_required:
        status = "HUMAN_DECISION_REQUIRED"
        reason = _REASONS["HUMAN_DECISION_REQUIRED_RISK"]
    elif semantic_risk:
        status = "HUMAN_DECISION_REQUIRED"
        reason = _REASONS["HUMAN_DECISION_REQUIRED_SEMANTIC"]
    elif pending:
        status = "PENDING_CONFIRMATION"
        reason = _REASONS["PENDING_CONFIRMATION"]
    else:
        status = "READY"
        reason = _REASONS["READY"]
```
In the returned dict, change `"required": status == "HUMAN_DECISION_REQUIRED"` to:
```python
            "required": status in ("HUMAN_DECISION_REQUIRED", "CONFIG_CHANGED_REVIEW_REQUIRED"),
```
and add a top-level key to the returned dict:
```python
        "provenance": provenance,
```

- [ ] **Step 5: Render the provenance line in `render_markdown`**

In `render_markdown`, immediately after the `| Owner | … |` row append:
```python
    provenance = readiness.get("provenance")
    if provenance and provenance.get("sources"):
        src = provenance["sources"][0]
        sha = (src.get("resolved_sha") or "")[:7]
        lines.append(
            f"| Production context | {provenance.get('file_count', 0)} files from "
            f"`{src.get('repo','')}@{sha}` |"
        )
```

- [ ] **Step 6: Run the readiness suite**

Run: `/opt/homebrew/bin/pytest tests/test_render_pr_readiness.py -q`
Expected: PASS (original 13 + 4 new)

- [ ] **Step 7: Commit**

```bash
git add SCRIPTS/render_pr_readiness.py tests/test_render_pr_readiness.py
git commit -m "feat: pack-aware readiness card with config-changed status"
```

---

## Task 7: readiness phase orchestrator (`mergeproof_readiness.py`)

The testable phase entry point the CR loop terminal phase calls. Implements gating (skip when config absent, render with `VERIFICATION_FAILED` when verification failed) and chains build_pack → assess → build_readiness → render. Optionally publishes.

**Files:**
- Create: `SCRIPTS/mergeproof_readiness.py`
- Test: `tests/test_mergeproof_readiness.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_mergeproof_readiness.py
"""Tests for the readiness phase orchestrator (CR-loop terminal phase)."""

from __future__ import annotations

import base64

import mergeproof_readiness as mr


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG_YAML = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
"""

INFRA = {
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    kubernetes.io/ingress.class: alb\n",
}

LOOP_SUMMARY = {
    "pr_url": "https://github.com/acme/app/pull/7",
    "fixed_count": 7, "false_positives_skipped": 1,
    "verification": "passed", "verification_command": "uv run pytest",
    "rereview": "completed", "cycles_used": 2, "cycles_total": 3,
}


class FakeGH:
    def __init__(self, changed, has_config=True, infra=None):
        self.changed = changed
        self.has_config = has_config
        self.infra = infra or INFRA

    def __call__(self, args):
        url = args[-1]
        if "/pulls/7/files" in url:
            return [{"filename": f} for f in self.changed]
        if "/pulls/7" in url:
            return {"base": {"ref": "main", "sha": "base"}}
        if "/contents/mergeproof.yaml" in url:
            if not self.has_config:
                raise RuntimeError("404")
            return {"encoding": "base64", "content": _b64(CONFIG_YAML)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": "isha"}
        if "/git/trees/" in url:
            return {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in self.infra.items()]}
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(self.infra[path])}
        raise RuntimeError(f"unexpected {url}")


def test_terminal_phase_renders_when_config_exists():
    gh = FakeGH(changed=["core/api/routes.py"])
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "HUMAN_DECISION_REQUIRED"
    assert "## GGRL PR Readiness" in result["markdown"]


def test_missing_config_skips_without_failing(capsys):
    gh = FakeGH(changed=["core/api/routes.py"], has_config=False)
    result = mr.run_readiness("acme/app", 7, LOOP_SUMMARY, runner=gh)
    assert result["status"] == "skipped"
    err = capsys.readouterr().err
    assert "[mergeproof] readiness skipped" in err
    assert "Reason: mergeproof.yaml not found" in err


def test_verification_failed_still_renders_with_failed_status():
    gh = FakeGH(changed=["core/api/routes.py"])
    summary = {**LOOP_SUMMARY, "verification": "failed"}
    result = mr.run_readiness("acme/app", 7, summary, runner=gh)
    assert result["status"] == "rendered"
    assert result["readiness"]["status"] == "VERIFICATION_FAILED"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/opt/homebrew/bin/pytest tests/test_mergeproof_readiness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mergeproof_readiness'`

- [ ] **Step 3: Write the implementation**

```python
# SCRIPTS/mergeproof_readiness.py
#!/usr/bin/env python3
"""Readiness phase: the final merge-readiness layer on top of the CR loop.

Runs after the review loop reaches terminal state. If mergeproof config exists
on the trusted base ref, it builds the Production Context Pack, overlays PR risk,
renders the Readiness Card, and (optionally) publishes it to the PR. If config
is absent, it prints the skip notice and the loop completes normally. If
verification failed, it still renders a card, forced to VERIFICATION_FAILED.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from build_context_pack import build_pack
from pr_architecture_risk import _default_pr_runner, assess, fetch_pr_changed_files, parse_pr
from publish_pr_readiness import publish
from render_pr_readiness import build_readiness, render_markdown

Runner = Callable[[list[str]], Any]

SKIP_MESSAGE = "[mergeproof] readiness skipped\nReason: mergeproof.yaml not found"


def run_readiness(
    app_repo: str,
    pr_number: int,
    loop_summary: dict[str, Any],
    *,
    runner: Runner = _default_pr_runner,
    trust_pr_config: bool = False,
    do_publish: bool = False,
) -> dict[str, Any]:
    """Run the readiness phase. Returns a dict with status 'skipped' or 'rendered'."""
    changed = fetch_pr_changed_files(app_repo, pr_number, runner=runner)
    pack = build_pack(
        app_repo, pr_number, changed, runner=runner, trust_pr_config=trust_pr_config
    )
    if pack is None:
        print(SKIP_MESSAGE, file=sys.stderr)
        return {"status": "skipped", "reason": "mergeproof.yaml not found"}

    risks = assess(pack["facts"], changed)
    readiness = build_readiness(loop_summary, pack, risks)
    markdown = render_markdown(readiness)

    result = {
        "status": "rendered",
        "readiness": readiness,
        "markdown": markdown,
        "pack": pack,
        "risks": risks,
    }
    if do_publish:
        result["publish"] = publish(app_repo, pr_number, markdown)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the MergeProof readiness phase for a PR."
    )
    parser.add_argument("--pr", required=True, help="PR URL or OWNER/REPO#N.")
    parser.add_argument("--loop-summary", required=True, help="Path to loop_summary.json.")
    parser.add_argument("--trust-pr-config", action="store_true",
                        help="Read mergeproof config from PR head instead of base ref.")
    parser.add_argument("--publish", action="store_true",
                        help="Post/update the readiness comment on the PR.")
    parser.add_argument("--markdown", action="store_true",
                        help="Print the readiness Markdown on stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    try:
        repo, number = parse_pr(args.pr)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        loop_summary = json.loads(Path(args.loop_summary).read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: could not read loop summary: {exc}", file=sys.stderr)
        return 2

    try:
        result = run_readiness(
            repo, number, loop_summary,
            trust_pr_config=args.trust_pr_config, do_publish=args.publish,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if result["status"] == "skipped":
        return 0
    if args.markdown:
        print(result["markdown"], end="")
    else:
        print(f"[mergeproof] readiness: {result['readiness']['status']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `/opt/homebrew/bin/pytest tests/test_mergeproof_readiness.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add SCRIPTS/mergeproof_readiness.py tests/test_mergeproof_readiness.py
git commit -m "feat: readiness phase orchestrator chained off the CR loop"
```

---

## Task 8: offline fixtures + full-chain integration test + docs

A mock infra file set + mock `mergeproof.yaml` driving the whole chain offline, plus a JSON-stdout discipline check and a demo guide update.

**Files:**
- Create: `tests/test_mergeproof_integration.py`
- Create: `demo/production-readiness/fixtures/mergeproof.yaml`
- Modify: `demo/production-readiness/README.md`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_mergeproof_integration.py
"""End-to-end MergeProof chain, fully offline via an injected GitHub runner."""

from __future__ import annotations

import base64
import json

import build_context_pack as bcp
import mergeproof_readiness as mr


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


CONFIG = """\
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/**
      - modules/sqs/**
limits:
  max_files: 50
"""

INFRA = {
    "envs/prod/ingress.yaml": "kind: Ingress\nmetadata:\n  annotations:\n    konghq.com/x: y\n    kubernetes.io/ingress.class: alb\n",
    "envs/prod/redis.yaml": "image: redis:7\n",
    "modules/sqs/main.tf": 'resource "aws_sqs_queue" "scan" {\n  name = "scan-events"\n}\n',
}

LOOP_SUMMARY = {
    "pr_url": "https://github.com/acme/app/pull/9",
    "fixed_count": 5, "verification": "passed", "verification_command": "uv run pytest",
    "rereview": "completed", "cycles_used": 1, "cycles_total": 3,
}


class FakeGH:
    def __init__(self, changed):
        self.changed = changed

    def __call__(self, args):
        url = args[-1]
        if "/pulls/9/files" in url:
            return [{"filename": f} for f in self.changed]
        if "/pulls/9" in url:
            return {"base": {"ref": "main", "sha": "base"}}
        if "/contents/mergeproof.yaml" in url:
            return {"encoding": "base64", "content": _b64(CONFIG)}
        if "/contents/mergeproof" in url:
            raise RuntimeError("404")
        if "/commits/" in url:
            return {"sha": "isha"}
        if "/git/trees/" in url:
            return {"truncated": False, "tree": [
                {"path": p, "type": "blob", "size": len(t)} for p, t in INFRA.items()]}
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            return {"encoding": "base64", "content": _b64(INFRA[path])}
        raise RuntimeError(url)


def test_full_chain_public_api_and_async_risk():
    gh = FakeGH(changed=["core/api/routes.py", "core/workers/scan_worker.py"])
    result = mr.run_readiness("acme/app", 9, LOOP_SUMMARY, runner=gh)
    rd = result["readiness"]
    assert rd["status"] == "HUMAN_DECISION_REQUIRED"
    surfaces = {r["surface"] for r in rd["production_risks"]}
    assert "public_api" in surfaces
    assert "async_processing" in surfaces  # worker file + SQS in pack
    assert rd["architecture"]["exposure"] == "public"


def test_pack_json_is_machine_clean(capsys):
    gh = FakeGH(changed=[])
    pack = bcp.build_pack("acme/app", 9, [], runner=gh, now_iso="2026-06-14T00:00:00Z")
    blob = json.dumps(pack)
    assert "\033[" not in blob
    json.loads(blob)  # round-trips
```

- [ ] **Step 2: Run the integration test to verify it fails**

Run: `/opt/homebrew/bin/pytest tests/test_mergeproof_integration.py -q`
Expected: FAIL until Tasks 1–7 are in place; once they are, it should pass with no code changes. If it fails after Tasks 1–7, fix the offending module (do not weaken the test).

- [ ] **Step 3: Add the demo fixture**

```yaml
# demo/production-readiness/fixtures/mergeproof.yaml
version: 1
service: aegislocal-api
architecture_sources:
  - repo: acme/infra
    ref: main
    allow:
      - envs/prod/aegislocal-api/**
      - modules/kong/**
      - modules/sqs/**
      - modules/redis/**
limits:
  max_files: 200
  max_file_bytes: 262144
```

- [ ] **Step 4: Update the demo guide**

In `demo/production-readiness/README.md`, add a section after the "Real PR flow (primary)" heading explaining the cross-repo model and the new entry point:

```markdown
## Cross-repo model (mergeproof.yaml)

Production reality usually lives in an infra repo, not the app repo. The app
repo carries a `mergeproof.yaml` (read from the trusted base branch, never PR
head) that declares the infra repo + an allowlist of paths:

    version: 1
    service: aegislocal-api
    architecture_sources:
      - repo: acme/infra
        ref: main
        allow:
          - envs/prod/aegislocal-api/**
          - modules/sqs/**

The readiness phase runs automatically after the review loop's terminal state:

    python3 $SCRIPTS/mergeproof_readiness.py \
      --pr https://github.com/OWNER/REPO/pull/123 \
      --loop-summary /path/to/loop_summary.json \
      --publish

If `mergeproof.yaml` is absent, readiness is skipped with:

    [mergeproof] readiness skipped
    Reason: mergeproof.yaml not found
```

- [ ] **Step 5: Run the entire suite**

Run: `/opt/homebrew/bin/pytest -q`
Expected: PASS (all prior + all new MergeProof tests)

- [ ] **Step 6: Commit**

```bash
git add tests/test_mergeproof_integration.py demo/production-readiness/fixtures/mergeproof.yaml demo/production-readiness/README.md
git commit -m "test: end-to-end MergeProof chain + demo guide for cross-repo flow"
```

---

## Self-review

**Spec coverage:**
- §3 entry point / gating → Task 7 (skip message, render-on-config-exists) + §5 verification-failed → Task 6/7. ✓
- §4 trust model (base ref, CONFIG_CHANGED_REVIEW_REQUIRED, --trust-pr-config) → Task 4 + Task 6 status. ✓
- §5 config parser (subset + JSON, rejections, duplicate keys) → Task 1. ✓
- §6 fetch/extraction (resolve once, truncation, max_files, size/binary, partial source) → Tasks 3, 4, 5. ✓
- §7 artifacts separate; pack excludes raw contents/secrets/changed files → Task 5 (`test_pack_never_contains_raw_file_contents_or_changed_files`). ✓
- §8 code layout (one job per script; build_context_pack emits pack only) → Tasks 1–7 names/responsibilities. ✓
- §10 test matrix: all 13 listed tests are present (config parser 2, trust 2, fetch 5, pack integrity 3 incl. determinism via deterministic inputs, orchestration 3). ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type/name consistency:** `extract_facts(files, files_found=)`, `path_matches`, `_decode_content`, `fetch_infra_files(source, max_files, max_file_bytes, runner)`, `resolve(...)→{status,config,config_changed,...}`, `build_pack(app_repo, pr_number, changed_files, runner, trust_pr_config, now_iso)`, `_unwrap_architecture`, `run_readiness(app_repo, pr_number, loop_summary, runner, trust_pr_config, do_publish)` — names match across Tasks 2–8. `_default_pr_runner`, `fetch_pr_changed_files`, `parse_pr`, `assess` are reused from the existing `pr_architecture_risk.py`; `publish` from `publish_pr_readiness.py`; `build_readiness`/`render_markdown` from `render_pr_readiness.py`. ✓

**Note on determinism (test #"pack output is deterministic"):** covered implicitly — `build_pack` takes `now_iso` so output is fully determined by inputs; if a dedicated test is wanted, assert `build_pack(...ns, now_iso=X) == build_pack(...same..., now_iso=X)` in Task 5.

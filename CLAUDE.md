# CLAUDE.md

## Test runner

```bash
# If installed via Homebrew on Apple Silicon:
/opt/homebrew/bin/pytest

# Or run pytest from a virtual environment:
pytest
```

`python3 -m pytest` does not work — pytest is not on the bare Python path.

## Python type annotations

All scripts use `from __future__ import annotations`. This makes subscripted generics (`dict[str, Any]`, `list[str]`, `str | None`) safe on Python 3.9+. Do not revert to bare `dict` / `list` to "fix" compatibility — it is already handled.

## Plugin cache

The installed plugin lives at:

```
~/.claude/plugins/cache/gh-gemini-review-loop/gh-gemini-review-loop/<version>/
```

To test unreleased script changes without merging, copy the modified files directly into the cache. A future `/plugin` update will overwrite them.

## Scripts location

```
plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/
```

When running scripts locally (not through the installed plugin), invoke from the repo root:

```bash
python3 plugins/gh-gemini-review-loop/skills/gh-gemini-review-loop/scripts/fetch_gemini_threads.py
```

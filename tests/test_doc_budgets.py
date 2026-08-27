"""Doc size-budget ratchet (#109).

SKILL.md is loaded into every session's context, so its size is a real cost
imposed on every installer. #88 cut it 75KB -> 24KB and it regrew to 25.9KB
while the trim issue was still open: advisory intent does not hold. These
tests make the ceiling mechanical — a PR that inflates a budgeted file fails
CI unless it also edits ``tests/budgets.json`` in the same diff, turning
silent drift into a visible, reviewable line.

Placement rule the budgets enforce: new behavior docs go in
``references/<topic>.md``; SKILL.md gets at most one pointer line; inline in
SKILL.md only what is needed every cycle.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO_ROOT / "plugins" / "gh-review-loop" / "skills" / "gh-review-loop"
SKILL_MD = SKILL_DIR / "SKILL.md"
REFERENCES_DIR = SKILL_DIR / "references"
BUDGETS = json.loads((REPO_ROOT / "tests" / "budgets.json").read_text())

RAISE_HINT = (
    "If this growth is deliberate, raise the budget in tests/budgets.json in "
    "this same PR and justify it in the PR body. Otherwise move the new "
    "material to references/<topic>.md and leave one pointer line in SKILL.md."
)


def _frontmatter_description(text: str) -> str:
    """The frontmatter description, including YAML continuation lines."""
    match = re.search(r"^---\n(.*?)\n---", text, re.DOTALL)
    assert match, "SKILL.md has no frontmatter block"
    lines = match.group(1).splitlines()
    out: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith("description:"):
            capturing = True
            out.append(line[len("description:"):].strip())
        elif capturing and line.startswith((" ", "\t")):
            out.append(line.strip())
        elif capturing:
            break
    assert out, "SKILL.md frontmatter has no description"
    return " ".join(out).strip()


class TestSkillMdBudget:
    def test_byte_budget(self):
        limit = BUDGETS["skill_md"]["max_bytes"]
        size = SKILL_MD.stat().st_size
        assert size <= limit, (
            f"SKILL.md is {size} bytes, budget is {limit}. {RAISE_HINT}"
        )

    def test_line_budget(self):
        limit = BUDGETS["skill_md"]["max_lines"]
        lines = SKILL_MD.read_text().count("\n")
        assert lines <= limit, (
            f"SKILL.md is {lines} lines, budget is {limit}. {RAISE_HINT}"
        )

    def test_description_word_budget(self):
        # Locks in #105: the frontmatter description is the skill's trigger
        # surface, loaded into context on every session.
        limit = BUDGETS["skill_md"]["description_max_words"]
        words = len(_frontmatter_description(SKILL_MD.read_text()).split())
        assert words <= limit, (
            f"SKILL.md description is {words} words, budget is {limit}. "
            f"{RAISE_HINT}"
        )


class TestReferencesBudget:
    def test_per_file_byte_budget(self):
        # Bloat must not just migrate out of SKILL.md into one giant reference.
        limit = BUDGETS["references"]["max_bytes_per_file"]
        over = {
            f.name: f.stat().st_size
            for f in sorted(REFERENCES_DIR.glob("*.md"))
            if f.stat().st_size > limit
        }
        assert not over, (
            f"references over the {limit}-byte per-file budget: {over}. "
            f"{RAISE_HINT}"
        )

    def test_no_orphan_references(self):
        # Every references/*.md must be reachable from SKILL.md — an orphan
        # is documentation the agent can never be told to load.
        skill_text = SKILL_MD.read_text()
        orphans = [
            f.name
            for f in sorted(REFERENCES_DIR.glob("*.md"))
            if f.name not in skill_text
        ]
        assert not orphans, (
            f"references never pointed to from SKILL.md: {orphans}. "
            "Add a load-when pointer line or delete the file."
        )

"""Delta fetch mode (#95): canonical thread blocks, render fingerprints,
per-thread baseline collapse in render_markdown.

Invariant under test: a thread is collapsed to one line only when the exact
block the renderer would emit for it is unchanged from the immediately
previous cycle (per thread id). Everything else renders in full.
"""

import hashlib

import fetch_gemini_threads as fgt


BOT = "gemini-code-assist"


def comment(
    body="Use snake_case here.",
    *,
    login=BOT,
    url="https://github.com/o/r/pull/1#discussion_r1",
    created="2026-08-01T00:00:00Z",
    hunk=None,
):
    node = {"author": {"login": login}, "body": body, "url": url, "createdAt": created}
    if hunk is not None:
        node["diffHunk"] = hunk
    return node


def thread(
    *,
    tid="T1",
    path="src/x.py",
    line=10,
    resolved=False,
    outdated=False,
    comments=None,
):
    """Filtered-thread shape: flat comments list, as render_markdown consumes."""
    return {
        "id": tid,
        "path": path,
        "line": line,
        "isResolved": resolved,
        "isOutdated": outdated,
        "comments": comments if comments is not None else [comment()],
    }


# ---------------------------------------------------------------------------
# render_thread_block — canonical per-thread block, index-free by default
# ---------------------------------------------------------------------------

class TestRenderThreadBlock:
    def test_header_without_index_when_index_is_none(self):
        lines = fgt.render_thread_block(thread())
        assert lines[0] == "## src/x.py:10"

    def test_header_with_index(self):
        lines = fgt.render_thread_block(thread(), index=3)
        assert lines[0] == "## 3. src/x.py:10"

    def test_severity_status_rendered(self):
        t = thread(comments=[comment(body="![high](img) fix this")])
        lines = fgt.render_thread_block(t)
        assert lines[0] == "## src/x.py:10 [high]"

    def test_comment_body_and_url_rendered(self):
        lines = fgt.render_thread_block(thread())
        text = "\n".join(lines)
        assert "Use snake_case here." in text
        assert "https://github.com/o/r/pull/1#discussion_r1" in text

    def test_hunk_at_cap_not_truncated(self):
        hunk = "\n".join(f"line{i}" for i in range(fgt.HUNK_MAX_LINES))
        lines = fgt.render_thread_block(thread(comments=[comment(hunk=hunk)]))
        text = "\n".join(lines)
        assert "line9" in text
        assert "more diff lines" not in text

    def test_hunk_over_cap_truncated_with_marker(self):
        hunk = "\n".join(f"line{i}" for i in range(fgt.HUNK_MAX_LINES + 5))
        lines = fgt.render_thread_block(thread(comments=[comment(hunk=hunk)]))
        text = "\n".join(lines)
        assert "line9" in text
        assert "line10" not in text
        assert "… (+5 more diff lines)" in text

    def test_judge_line_includes_confidence_in_output_form(self):
        jr = {
            "status": "ok",
            "verdict": "valid_actionable",
            "confidence": 0.87,
            "severity_override": None,
            "recommended_action": "fix",
            "reason": "real bug",
        }
        lines = fgt.render_thread_block(thread(), judge_result=jr)
        assert "conf 0.87" in lines[0]

    def test_judge_line_excludes_confidence_in_canonical_form(self):
        jr = {
            "status": "ok",
            "verdict": "valid_actionable",
            "confidence": 0.87,
            "severity_override": None,
            "recommended_action": "fix",
            "reason": "real bug",
        }
        lines = fgt.render_thread_block(thread(), judge_result=jr, include_confidence=False)
        text = "\n".join(lines)
        assert "0.87" not in text
        assert "valid_actionable" in text


# ---------------------------------------------------------------------------
# thread_render_fingerprint — hash of the canonical (index-free, conf-free) block
# ---------------------------------------------------------------------------

class TestThreadRenderFingerprint:
    def test_is_sha256_of_canonical_block(self):
        t = thread()
        canonical = "\n".join(
            fgt.render_thread_block(t, judge_result=None, include_confidence=False)
        )
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        assert fgt.thread_render_fingerprint(t) == expected

    def test_stable_across_positional_index(self):
        # The fingerprint must not depend on where the thread appears in the list.
        t = thread()
        assert fgt.thread_render_fingerprint(t) == fgt.thread_render_fingerprint(dict(t))

    def test_changes_on_body_edit_past_char_1000(self):
        long_body = "x" * 1500
        edited = long_body[:-1] + "y"  # differs only at char 1500
        a = thread(comments=[comment(body=long_body)])
        b = thread(comments=[comment(body=edited)])
        assert fgt.thread_render_fingerprint(a) != fgt.thread_render_fingerprint(b)

    def test_changes_on_added_reply(self):
        a = thread()
        b = thread(comments=[comment(), comment(body="also consider Y", login=BOT)])
        assert fgt.thread_render_fingerprint(a) != fgt.thread_render_fingerprint(b)

    def test_changes_on_line_move(self):
        assert fgt.thread_render_fingerprint(thread(line=10)) != fgt.thread_render_fingerprint(
            thread(line=12)
        )

    def test_changes_on_resolved_state(self):
        assert fgt.thread_render_fingerprint(thread()) != fgt.thread_render_fingerprint(
            thread(resolved=True)
        )

    def test_changes_on_judge_verdict(self):
        jr_a = {
            "status": "ok",
            "verdict": "valid_actionable",
            "confidence": 0.9,
            "severity_override": None,
            "recommended_action": "fix",
            "reason": "",
        }
        jr_b = dict(jr_a, verdict="false_positive")
        t = thread()
        assert fgt.thread_render_fingerprint(t, judge_result=jr_a) != fgt.thread_render_fingerprint(
            t, judge_result=jr_b
        )

    def test_unchanged_on_judge_confidence_jitter(self):
        jr_a = {
            "status": "ok",
            "verdict": "valid_actionable",
            "confidence": 0.87,
            "severity_override": None,
            "recommended_action": "fix",
            "reason": "",
        }
        jr_b = dict(jr_a, confidence=0.91)
        t = thread()
        assert fgt.thread_render_fingerprint(t, judge_result=jr_a) == fgt.thread_render_fingerprint(
            t, judge_result=jr_b
        )

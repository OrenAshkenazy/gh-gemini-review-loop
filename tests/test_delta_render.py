"""Delta fetch mode (#95): canonical thread blocks, render fingerprints,
per-thread baseline collapse in render_markdown.

Invariant under test: a thread is collapsed to one line only when the exact
block the renderer would emit for it is unchanged from the immediately
previous cycle (per thread id). Everything else renders in full.
"""

import hashlib
import json
import sys

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


# ---------------------------------------------------------------------------
# Render baseline state — thread_id → previous cycle's render_fp, per (pr, author)
# ---------------------------------------------------------------------------

PR = fgt.PullRequest(owner="o", repo="r", number=7)


class TestRenderBaseline:
    def test_empty_when_never_saved(self):
        assert fgt.read_render_baseline(PR, BOT) == {}

    def test_round_trip(self):
        fgt.save_render_baseline(PR, BOT, {"T1": "fp1", "T2": "fp2"})
        assert fgt.read_render_baseline(PR, BOT) == {"T1": "fp1", "T2": "fp2"}

    def test_save_replaces_previous_mapping(self):
        # The baseline is the immediately previous cycle, not an ever-growing set:
        # this is what makes A→B→A render full on the third cycle.
        fgt.save_render_baseline(PR, BOT, {"T1": "fpA"})
        fgt.save_render_baseline(PR, BOT, {"T1": "fpB"})
        assert fgt.read_render_baseline(PR, BOT) == {"T1": "fpB"}

    def test_reviewers_have_independent_baselines(self):
        fgt.save_render_baseline(PR, BOT, {"T1": "fp1"})
        fgt.save_render_baseline(PR, "chatgpt-codex-connector", {"T1": "other"})
        assert fgt.read_render_baseline(PR, BOT) == {"T1": "fp1"}
        assert fgt.read_render_baseline(PR, "chatgpt-codex-connector") == {"T1": "other"}

    def test_terminal_run_completion_clears_the_baseline(self):
        # A later session has never seen the bodies these fingerprints stand
        # for, so a baseline surviving --record-run would collapse unseen
        # threads to URL-only stubs on that session's first fetch.
        fgt.update_run_tracking(PR, [("T1", "a.py")])
        fgt.save_render_baseline(PR, BOT, {"T1": "fp1"})
        fgt.save_render_baseline(PR, "chatgpt-codex-connector", {"T1": "other"})

        fgt.clear_run_tracking(PR)

        assert fgt.read_render_baseline(PR, BOT) == {}
        assert fgt.read_render_baseline(PR, "chatgpt-codex-connector") == {}

    def test_clearing_one_pr_leaves_another_pr_baseline_intact(self):
        other = fgt.PullRequest(owner="o", repo="r", number=8)
        fgt.save_render_baseline(PR, BOT, {"T1": "fp1"})
        fgt.save_render_baseline(other, BOT, {"T2": "fp2"})

        fgt.clear_run_tracking(PR)

        assert fgt.read_render_baseline(PR, BOT) == {}
        assert fgt.read_render_baseline(other, BOT) == {"T2": "fp2"}


# ---------------------------------------------------------------------------
# render_markdown delta collapse
# ---------------------------------------------------------------------------

def pull_req(*threads_):
    return {
        "number": 7,
        "url": "https://github.com/o/r/pull/7",
        "reviewThreads": {"nodes": []},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }


def render(threads_, baseline=None, full=False):
    return fgt.render_markdown(
        pull_req(),
        threads_,
        BOT,
        baseline=baseline,
        full=full,
    )


class TestDeltaCollapse:
    def test_no_baseline_renders_full(self):
        out = render([thread()])
        assert "Use snake_case here." in out

    def test_matching_fp_collapses_to_one_line(self):
        t = thread()
        baseline = {"T1": fgt.thread_render_fingerprint(t)}
        out = render([t], baseline=baseline)
        assert "Use snake_case here." not in out
        assert "unchanged since last cycle" in out

    def test_collapsed_line_keeps_anchor_severity_and_url(self):
        t = thread(comments=[comment(body="![high](img) fix this")])
        baseline = {"T1": fgt.thread_render_fingerprint(t)}
        out = render([t], baseline=baseline)
        collapsed = next(line for line in out.splitlines() if "unchanged" in line)
        assert "src/x.py:10" in collapsed
        assert "[high]" in collapsed
        assert "https://github.com/o/r/pull/1#discussion_r1" in collapsed

    def test_stale_fp_renders_full_block_byte_identical_to_full_mode(self):
        t = thread()
        baseline = {"T1": "stale-fp-from-older-version"}
        assert render([t], baseline=baseline) == render([t], full=True)

    def test_new_thread_with_identical_content_renders_full(self):
        # Same content, different thread id: first appearance is always full.
        t1 = thread(tid="T1")
        t2 = thread(tid="T2")
        baseline = {"T1": fgt.thread_render_fingerprint(t1)}
        out = render([t1, t2], baseline=baseline)
        assert out.count("unchanged since last cycle") == 1
        assert "Use snake_case here." in out

    def test_full_flag_ignores_baseline(self):
        t = thread()
        baseline = {"T1": fgt.thread_render_fingerprint(t)}
        out = render([t], baseline=baseline, full=True)
        assert "Use snake_case here." in out
        assert "unchanged since last cycle" not in out

    def test_ten_unchanged_verbose_findings_shrink_to_a_fifth(self):
        verbose = "This finding has a very detailed explanation. " * 40
        hunk = "\n".join(f"context line {i}" for i in range(fgt.HUNK_MAX_LINES))
        threads_ = [
            thread(
                tid=f"T{i}",
                line=i + 1,
                comments=[comment(body=f"{verbose} #{i}", hunk=hunk)],
            )
            for i in range(10)
        ]
        baseline = {t["id"]: fgt.thread_render_fingerprint(t) for t in threads_}
        full_out = render(threads_, full=True)
        delta_out = render(threads_, baseline=baseline)
        assert len(delta_out) <= len(full_out) * 0.20

    def test_a_b_a_across_three_cycles_renders_full_on_the_third(self):
        version_x = thread()
        version_y = thread(comments=[comment(body="Actually, use camelCase.")])
        fp_x = fgt.thread_render_fingerprint(version_x)
        fp_y = fgt.thread_render_fingerprint(version_y)

        # Cycle 2: thread changed X→Y, baseline holds X → full.
        assert "camelCase" in render([version_y], baseline={"T1": fp_x})
        # Cycle 3: thread reverted Y→X, baseline holds Y (previous cycle,
        # not a seen-ever set) → full again.
        assert "snake_case" in render([version_x], baseline={"T1": fp_y})
        # Control: baseline actually matching → collapsed.
        assert "snake_case" not in render([version_x], baseline={"T1": fp_x})


# ---------------------------------------------------------------------------
# main() wiring — emit-then-commit ordering, --full, JSON changedSinceLastCycle
# ---------------------------------------------------------------------------

CODEX = "chatgpt-codex-connector"


def graphql_thread(body="Please fix this.", *, tid="thread-1"):
    return {
        "id": tid,
        "path": "app.py",
        "line": 12,
        "isResolved": False,
        "isOutdated": False,
        "comments": {
            "nodes": [
                {
                    "author": {"login": CODEX},
                    "body": body,
                    "createdAt": "2026-06-09T07:17:16Z",
                    "url": "https://github.example/thread-1",
                }
            ]
        },
    }


def run_fetch(monkeypatch, capsys, *extra_argv, body="Please fix this."):
    pr = fgt.PullRequest(owner="o", repo="r", number=7)
    pull_request = {
        "reviewThreads": {"nodes": [graphql_thread(body=body)]},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }
    monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
    monkeypatch.setattr(fgt, "fetch_threads", lambda resolved_pr: pull_request)
    monkeypatch.setattr(fgt, "gh_authenticated_login", lambda: "agent")
    monkeypatch.setattr(
        sys, "argv", ["fetch_gemini_threads.py", "--judge-mode", "off", *extra_argv]
    )
    rc = fgt.main()
    captured = capsys.readouterr()
    assert rc == 0
    return captured


class TestMainDeltaWiring:
    def test_second_fetch_collapses_unchanged_threads(self, monkeypatch, capsys):
        first = run_fetch(monkeypatch, capsys)
        assert "Please fix this." in first.out
        second = run_fetch(monkeypatch, capsys)
        assert "Please fix this." not in second.out
        assert "unchanged since last cycle" in second.out

    def test_changed_body_renders_full_on_second_fetch(self, monkeypatch, capsys):
        run_fetch(monkeypatch, capsys)
        second = run_fetch(monkeypatch, capsys, body="Different feedback now.")
        assert "Different feedback now." in second.out
        assert "unchanged since last cycle" not in second.out

    def test_full_flag_ignores_committed_baseline(self, monkeypatch, capsys):
        run_fetch(monkeypatch, capsys)
        third = run_fetch(monkeypatch, capsys, "--full")
        assert "Please fix this." in third.out
        assert "unchanged since last cycle" not in third.out

    def test_baseline_commit_failure_fails_open(self, monkeypatch, capsys):
        def boom(pr, author, fps):
            raise OSError("disk full")

        with monkeypatch.context() as m:
            m.setattr(fgt, "save_render_baseline", boom)
            first = run_fetch(monkeypatch, capsys)
        assert "Please fix this." in first.out
        assert "could not save render baseline" in first.err
        # Baseline never committed → next fetch still renders full.
        second = run_fetch(monkeypatch, capsys)
        assert "Please fix this." in second.out

    def test_json_marks_changed_but_does_not_commit_baseline(self, monkeypatch, capsys):
        first = run_fetch(monkeypatch, capsys, "--format", "json")
        payload = json.loads(first.out)
        assert payload["threads"][0]["changedSinceLastCycle"] is True
        # JSON emission is the machine path — it must not move the baseline.
        second = run_fetch(monkeypatch, capsys, "--format", "json")
        assert json.loads(second.out)["threads"][0]["changedSinceLastCycle"] is True

    def test_json_reflects_markdown_committed_baseline(self, monkeypatch, capsys):
        run_fetch(monkeypatch, capsys)  # markdown commits the baseline
        after = run_fetch(monkeypatch, capsys, "--format", "json")
        assert json.loads(after.out)["threads"][0]["changedSinceLastCycle"] is False

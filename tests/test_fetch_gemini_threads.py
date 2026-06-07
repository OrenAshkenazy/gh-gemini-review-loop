"""Unit tests for pure functions in fetch_gemini_threads.py.

These tests intentionally avoid network/gh calls — they exercise only the
pure helpers that operate on already-fetched GraphQL payloads.
"""

import json
import sys

import pytest

from fetch_gemini_threads import (
    ADDRESSED_BY_REPLY_MIN_CHARS,
    PAGE_LIMIT_REVIEW_THREADS,
    PAGE_LIMIT_THREAD_COMMENTS,
    STICKY_RECEIPT_MARKER,
    PullRequest,
    accumulate_judge_results,
    addressed_by_reply_threads,
    _derive_outcome,
    clear_run_tracking,
    derive_record_fields,
    format_judge_verdict_summary,
    merge_judge_results,
    any_active_run,
    effective_rereview_limit,
    filter_by_min_severity,
    find_active_run,
    stamp_summary_emitted,
    summary_is_stale,
    filter_threads,
    is_addressed_by_reply,
    load_preferences_with_fallback,
    load_sticky_state,
    nonnegative_int,
    pagination_warnings,
    parse_pr_url,
    read_run_tracking,
    record_cycle,
    render_receipt,
    rereview_requests,
    review_activity_fingerprint,
    save_sticky_state,
    select_stats_records,
    severity_counts,
    sort_by_severity,
    sticky_state_path,
    thread_fingerprint,
    thread_severity,
    update_run_tracking,
)


BOT = "gemini-code-assist"


def make_thread(*, resolved=False, outdated=False, comments):
    """Build a GraphQL-shaped thread for tests."""
    return {
        "isResolved": resolved,
        "isOutdated": outdated,
        "path": "src/x.py",
        "line": 10,
        "comments": {"nodes": comments},
    }


def bot_comment(body="Use snake_case here."):
    return {"author": {"login": BOT}, "body": body}


def human_comment(body, login="alice"):
    return {"author": {"login": login}, "body": body}


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_canonical_url(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42")
        assert pr.owner == "o" and pr.repo == "r" and pr.number == 42

    def test_url_with_trailing_path(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42/files")
        assert pr is not None and pr.number == 42

    def test_url_with_query(self):
        pr = parse_pr_url("https://github.com/o/r/pull/42?foo=bar")
        assert pr is not None and pr.number == 42

    def test_invalid_returns_none(self):
        assert parse_pr_url("not-a-url") is None
        assert parse_pr_url("https://example.com/foo") is None


# ---------------------------------------------------------------------------
# is_addressed_by_reply
# ---------------------------------------------------------------------------

class TestIsAddressedByReply:
    def test_no_reply_returns_false(self):
        t = make_thread(comments=[bot_comment()])
        assert is_addressed_by_reply(t, BOT) is False

    def test_token_ack_returns_false(self):
        t = make_thread(comments=[bot_comment(), human_comment("ok")])
        assert is_addressed_by_reply(t, BOT) is False

    def test_substantive_reply_returns_true(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is True

    def test_bot_reply_does_not_count(self):
        long = "Some other bot is chiming in with a long-enough comment here."
        t = make_thread(comments=[bot_comment(), human_comment(long, login="github-actions[bot]")])
        assert is_addressed_by_reply(t, BOT) is False

    def test_resolved_returns_false(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(resolved=True, comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is False

    def test_outdated_returns_false(self):
        long = "Wontfix: existing convention in this module is camelCase."
        t = make_thread(outdated=True, comments=[bot_comment(), human_comment(long)])
        assert is_addressed_by_reply(t, BOT) is False

    def test_exactly_min_chars_returns_true(self):
        body = "x" * ADDRESSED_BY_REPLY_MIN_CHARS
        t = make_thread(comments=[bot_comment(), human_comment(body)])
        assert is_addressed_by_reply(t, BOT) is True

    def test_one_below_min_chars_returns_false(self):
        body = "x" * (ADDRESSED_BY_REPLY_MIN_CHARS - 1)
        t = make_thread(comments=[bot_comment(), human_comment(body)])
        assert is_addressed_by_reply(t, BOT) is False


# ---------------------------------------------------------------------------
# addressed_by_reply_threads + filter_threads interaction
# ---------------------------------------------------------------------------

def _pr_with_threads(threads):
    return {"reviewThreads": {"nodes": threads}}


class TestThreadFiltering:
    def test_addressed_by_reply_threads_isolation(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),  # actionable
            make_thread(comments=[bot_comment(), human_comment(long)]),  # addressed
            make_thread(resolved=True, comments=[bot_comment()]),  # resolved
            make_thread(outdated=True, comments=[bot_comment()]),  # outdated
        ]
        assert len(addressed_by_reply_threads(_pr_with_threads(threads), BOT)) == 1

    def test_filter_threads_default_excludes_addressed_by_reply(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),
            make_thread(comments=[bot_comment(), human_comment(long)]),
        ]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False)
        assert len(result) == 1

    def test_filter_threads_include_addressed_by_reply(self):
        long = "Wontfix: existing convention in this module is camelCase."
        threads = [
            make_thread(comments=[bot_comment()]),
            make_thread(comments=[bot_comment(), human_comment(long)]),
        ]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False,
                                 include_addressed_by_reply=True)
        assert len(result) == 2

    def test_filter_threads_drops_threads_without_author_comments(self):
        threads = [make_thread(comments=[human_comment("hello there friend, this is a long enough comment", login="bob")])]
        result = filter_threads(_pr_with_threads(threads), author=BOT,
                                 include_resolved=False, include_outdated=False)
        assert result == []


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class TestSeverity:
    @pytest.mark.parametrize("alt,expected", [
        ("critical", "critical"), ("high", "high"),
        ("medium", "medium"), ("low", "low"),
        ("HIGH", "high"), ("Critical", "critical"),
    ])
    def test_extracts_severity_from_alt_text(self, alt, expected):
        body = f"![{alt}](https://www.gstatic.com/codereviewagent/{alt.lower()}-priority.svg) Use X."
        t = make_thread(comments=[bot_comment(body)])
        assert thread_severity(t) == expected

    def test_no_marker_returns_unknown(self):
        assert thread_severity(make_thread(comments=[bot_comment("plain comment")])) == "unknown"

    def test_handles_post_filter_flat_shape(self):
        # filter_threads flattens comments to a plain list; severity must still work
        flat = {"comments": [{"author": {"login": BOT}, "body": "![low](x)"}]}
        assert thread_severity(flat) == "low"

    def test_sort_orders_by_severity_descending(self):
        def t(s):
            return make_thread(comments=[bot_comment(f"![{s}](x)" if s else "x")])
        unsorted = [t("low"), t("critical"), t(None), t("medium"), t("high")]
        sevs = [thread_severity(x) for x in sort_by_severity(unsorted)]
        assert sevs == ["critical", "high", "medium", "low", "unknown"]

    def test_sort_is_stable_within_severity(self):
        def t(s, marker):
            body = f"![{s}](x) {marker}"
            return make_thread(comments=[bot_comment(body)])
        items = [t("high", "A"), t("high", "B"), t("high", "C")]
        out = sort_by_severity(items)
        markers = [o["comments"]["nodes"][0]["body"].split()[-1] for o in out]
        assert markers == ["A", "B", "C"]

    def test_counts(self):
        def t(s):
            return make_thread(comments=[bot_comment(f"![{s}](x)" if s else "x")])
        counts = severity_counts([t("high"), t("high"), t("low"), t(None)])
        assert counts == {"high": 2, "low": 1, "unknown": 1}


# ---------------------------------------------------------------------------
# rereview_requests
# ---------------------------------------------------------------------------

class TestRereviewRequests:
    @staticmethod
    def _pr(comments):
        return {"comments": {"nodes": comments}}

    def test_counts_matching_trigger(self):
        pr = self._pr([
            {"author": {"login": "a"}, "body": "@gemini-code-assist please review"},
            {"author": {"login": "b"}, "body": "unrelated"},
        ])
        assert len(rereview_requests(pr)) == 1

    def test_filter_by_agent_login(self):
        pr = self._pr([
            {"author": {"login": "agent"}, "body": "@gemini-code-assist please review"},
            {"author": {"login": "human"}, "body": "@gemini-code-assist can you review again?"},
        ])
        assert len(rereview_requests(pr, agent_login="agent")) == 1
        assert len(rereview_requests(pr, agent_login="human")) == 1
        assert len(rereview_requests(pr, agent_login="nobody")) == 0
        assert len(rereview_requests(pr)) == 2  # no filter

    def test_ignores_comment_without_review_word(self):
        pr = self._pr([{"author": {"login": "a"}, "body": "@gemini-code-assist hi"}])
        assert rereview_requests(pr) == []


# ---------------------------------------------------------------------------
# re-review cap preference
# ---------------------------------------------------------------------------

class TestRereviewLimit:
    def test_cli_value_wins_over_preferences(self):
        assert effective_rereview_limit(2, {"max_rereview_requests": 5}) == 2

    def test_preferences_used_when_cli_absent(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": 5}) == 5

    def test_invalid_preference_falls_back_to_default(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": -1}) == 3
        assert effective_rereview_limit(None, {"max_rereview_requests": True}) == 3
        assert effective_rereview_limit(None, {"max_rereview_requests": "five"}) == 3

    def test_string_preference_is_coerced(self):
        assert effective_rereview_limit(None, {"max_rereview_requests": "5"}) == 5
        assert effective_rereview_limit(None, {"max_rereview_requests": " 6 "}) == 6


class TestPreferencesFallback:
    """``load_preferences_with_fallback`` must keep working when ``judge`` is missing."""

    # Keys callers may read on the returned dict without guarding against KeyError.
    _EXPECTED_KEYS = {
        "schema_version", "judge_mode", "judge_model",
        "judge_tip_shown", "max_rereview_requests", "set_at",
    }

    def test_falls_back_to_direct_json_when_judge_import_fails(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            '{"max_rereview_requests": 7}', encoding="utf-8"
        )
        # Force the lazy `from judge import load_preferences` to fail.
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()

        # Loaded value wins; defaults fill the rest so downstream callers can
        # read documented keys like `judge_model` without KeyError handling.
        assert prefs["max_rereview_requests"] == 7
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        # User must see *why* the canonical loader was skipped.
        assert "judge" in capsys.readouterr().err

    def test_fallback_returns_defaults_when_file_missing(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "judge", None)
        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert prefs["judge_mode"] == "off"

    def test_fallback_writes_defaults_on_first_run(
        self, tmp_path, monkeypatch
    ):
        """File must be created on first invocation so users can discover and edit it."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setitem(sys.modules, "judge", None)
        assert not (tmp_path / "preferences.json").exists()

        load_preferences_with_fallback()

        assert (tmp_path / "preferences.json").exists()
        saved = json.loads((tmp_path / "preferences.json").read_text())
        assert saved["judge_mode"] == "off"
        assert saved["max_rereview_requests"] == 3

    def test_fallback_returns_defaults_on_corrupt_json(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text("{ not json", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert "could not read" in capsys.readouterr().err

    def test_fallback_returns_defaults_when_not_object(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text("[1, 2, 3]", encoding="utf-8")
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert self._EXPECTED_KEYS.issubset(prefs.keys())
        assert "JSON object" in capsys.readouterr().err

    def test_fallback_composes_with_effective_rereview_limit(
        self, tmp_path, monkeypatch
    ):
        """Persistent cap setting takes effect even without the judge module."""
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "preferences.json").write_text(
            '{"max_rereview_requests": "5"}', encoding="utf-8"
        )
        monkeypatch.setitem(sys.modules, "judge", None)

        prefs = load_preferences_with_fallback()
        assert effective_rereview_limit(None, prefs) == 5


class TestNonnegativeInt:
    def test_valid_value(self):
        assert nonnegative_int("4") == 4

    def test_zero_is_allowed(self):
        assert nonnegative_int("0") == 0

    def test_negative_raises_argparse_error(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match=">= 0"):
            nonnegative_int("-1")

    def test_non_integer_raises_argparse_error(self):
        import argparse
        with pytest.raises(argparse.ArgumentTypeError, match="invalid int value"):
            nonnegative_int("abc")


# ---------------------------------------------------------------------------
# pagination_warnings
# ---------------------------------------------------------------------------

class TestPaginationWarnings:
    def test_under_limit_no_warnings(self):
        pr = {"reviewThreads": {"nodes": [{"comments": {"nodes": []}} for _ in range(5)]},
              "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert pagination_warnings(pr) == []

    def test_review_threads_at_cap_warns(self):
        pr = {"reviewThreads": {"nodes": [{"id": str(i), "comments": {"nodes": []}}
                                            for i in range(PAGE_LIMIT_REVIEW_THREADS)]},
              "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert any("reviewThreads hit page limit" in w for w in pagination_warnings(pr))

    def test_per_thread_comments_at_cap_warns(self):
        deep = {"id": "t1", "comments": {"nodes": [bot_comment("x") for _ in range(PAGE_LIMIT_THREAD_COMMENTS)]}}
        pr = {"reviewThreads": {"nodes": [deep]}, "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert any("comments hit page limit" in w for w in pagination_warnings(pr))


# ---------------------------------------------------------------------------
# thread_fingerprint
# ---------------------------------------------------------------------------

class TestThreadFingerprint:
    def test_stable_across_runs(self):
        threads = [make_thread(comments=[bot_comment("hi")])]
        a = thread_fingerprint([{"path": t["path"], "line": t["line"],
                                 "comments": t["comments"]["nodes"]} for t in threads])
        b = thread_fingerprint([{"path": t["path"], "line": t["line"],
                                 "comments": t["comments"]["nodes"]} for t in threads])
        assert a == b

    def test_changes_when_body_changes(self):
        a = thread_fingerprint([{"path": "p", "line": 1,
                                  "comments": [{"author": {"login": BOT}, "body": "X"}]}])
        b = thread_fingerprint([{"path": "p", "line": 1,
                                  "comments": [{"author": {"login": BOT}, "body": "Y"}]}])
        assert a != b


# ---------------------------------------------------------------------------
# review_activity_fingerprint + after_iso anchor
# ---------------------------------------------------------------------------

def _pr_with_review(submitted_at: str) -> dict:
    """Minimal pull_request fixture with one Gemini review at submitted_at."""
    return {
        "reviewThreads": {"nodes": []},
        "reviews": {"nodes": [
            {"author": {"login": BOT}, "submittedAt": submitted_at, "state": "COMMENTED"},
        ]},
        "comments": {"nodes": []},
    }


def _pr_with_thread_comment(created_at: str) -> dict:
    """Minimal pull_request fixture with one Gemini thread comment at created_at."""
    return {
        "reviewThreads": {"nodes": [
            {
                "isResolved": False,
                "isOutdated": False,
                "path": "src/x.py",
                "line": 1,
                "comments": {"nodes": [
                    {"author": {"login": BOT}, "body": "![medium](x) fix", "createdAt": created_at},
                ]},
            },
        ]},
        "reviews": {"nodes": []},
        "comments": {"nodes": []},
    }


class TestReviewActivityFingerprint:
    # --- no after_iso (existing behaviour) ---

    def test_none_when_no_activity(self):
        pr = {"reviewThreads": {"nodes": []}, "reviews": {"nodes": []}, "comments": {"nodes": []}}
        assert review_activity_fingerprint(pr, BOT) is None

    def test_non_none_when_review_present(self):
        pr = _pr_with_review("2026-06-07T10:00:00Z")
        assert review_activity_fingerprint(pr, BOT) is not None

    def test_different_review_bodies_produce_different_fingerprints(self):
        pr1 = _pr_with_review("2026-06-07T10:00:00Z")
        pr2 = _pr_with_review("2026-06-07T11:00:00Z")
        assert review_activity_fingerprint(pr1, BOT) != review_activity_fingerprint(pr2, BOT)

    # --- after_iso filters out old activity ---

    def test_none_when_review_before_anchor(self):
        pr = _pr_with_review("2026-06-07T09:00:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_none_when_review_exactly_at_anchor(self):
        # Exclusive lower bound: activity AT the anchor timestamp is treated as
        # "not new" (the re-review comment itself has that same timestamp).
        pr = _pr_with_review("2026-06-07T10:00:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_non_none_when_review_after_anchor(self):
        pr = _pr_with_review("2026-06-07T10:00:01Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is not None

    def test_none_when_thread_comment_before_anchor(self):
        pr = _pr_with_thread_comment("2026-06-07T09:59:00Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is None

    def test_non_none_when_thread_comment_after_anchor(self):
        pr = _pr_with_thread_comment("2026-06-07T10:00:01Z")
        assert review_activity_fingerprint(pr, BOT, after_iso="2026-06-07T10:00:00Z") is not None

    def test_mixed_only_new_review_counts(self):
        # One old review (should be filtered), one new review (should count).
        pr = {
            "reviewThreads": {"nodes": []},
            "reviews": {"nodes": [
                {"author": {"login": BOT}, "submittedAt": "2026-06-07T09:00:00Z", "state": "COMMENTED"},
                {"author": {"login": BOT}, "submittedAt": "2026-06-07T11:00:00Z", "state": "COMMENTED"},
            ]},
            "comments": {"nodes": []},
        }
        anchor = "2026-06-07T10:00:00Z"
        # Without anchor: sees both reviews
        fp_all = review_activity_fingerprint(pr, BOT)
        # With anchor: only the new review
        pr_old_only = _pr_with_review("2026-06-07T09:00:00Z")
        pr_new_only = _pr_with_review("2026-06-07T11:00:00Z")
        fp_anchored = review_activity_fingerprint(pr, BOT, after_iso=anchor)
        fp_new_only = review_activity_fingerprint(pr_new_only, BOT, after_iso=anchor)
        assert fp_anchored is not None
        assert fp_anchored == fp_new_only  # anchored result matches "new review only"
        assert fp_anchored != fp_all       # differs from the unanchored result

    def test_other_author_not_counted(self):
        pr = _pr_with_review("2026-06-07T11:00:00Z")
        # A different author than BOT should not match
        assert review_activity_fingerprint(pr, "other-bot", after_iso="2026-06-07T10:00:00Z") is None


# ---------------------------------------------------------------------------
# render_receipt
# ---------------------------------------------------------------------------

class TestRenderReceipt:
    def test_includes_all_metrics(self):
        pr_obj = PullRequest(owner="o", repo="r", number=1, url=None)
        threads = [make_thread(comments=[bot_comment("![high](x) fix me")])]
        receipt = render_receipt(
            pr_obj, _pr_with_threads([]), threads,
            author=BOT, resolved_outdated=2, resolved_addressed_by_reply=1,
            rereview_count=1, rereview_limit=3,
        )
        assert "1 / 3" in receipt
        assert "| 2 |" in receipt  # outdated resolved
        assert "high=1" in receipt
        assert "actionable threads remaining" in receipt


# ---------------------------------------------------------------------------
# filter_by_min_severity
# ---------------------------------------------------------------------------

def _sev_thread(sev: str | None):
    body = (f"![{sev}](https://www.gstatic.com/codereviewagent/{sev}-priority.svg) fix me"
            if sev else "plain comment with no marker")
    return {
        "isResolved": False,
        "isOutdated": False,
        "comments": {"nodes": [{"author": {"login": BOT}, "body": body}]},
    }


class TestFilterByMinSeverity:
    def test_high_drops_medium_and_low(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "high")
        assert [thread_severity(t) for t in out] == ["critical", "high"]

    def test_medium_keeps_medium_and_above(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "medium")
        assert [thread_severity(t) for t in out] == ["critical", "high", "medium"]

    def test_low_keeps_everything_severity_typed(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "low")
        assert len(out) == 4

    def test_critical_keeps_only_critical(self):
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low")]
        out = filter_by_min_severity(items, "critical")
        assert [thread_severity(t) for t in out] == ["critical"]

    def test_unknown_kept_by_default(self):
        items = [_sev_thread("low"), _sev_thread(None), _sev_thread("critical")]
        out = filter_by_min_severity(items, "high")
        # low dropped; critical kept; unknown kept (default keep_unknown=True)
        assert {thread_severity(t) for t in out} == {"critical", "unknown"}

    def test_keep_unknown_false_drops_unmarked(self):
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, "high", keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical"]

    def test_drop_unknown_only(self):
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, None, keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical", "low"]

    def test_drop_unknown_only_with_none_min_severity(self):
        # --drop-unknown-severity used alone: keep everything tagged, drop unknown.
        items = [_sev_thread("critical"), _sev_thread(None), _sev_thread("low")]
        out = filter_by_min_severity(items, None, keep_unknown=False)
        assert [thread_severity(t) for t in out] == ["critical", "low"]

    def test_none_min_severity_keeps_all_by_default(self):
        # min_severity=None with keep_unknown=True is a pure no-op.
        items = [_sev_thread(s) for s in ("critical", "high", "medium", "low", None)]
        out = filter_by_min_severity(items, None)
        assert len(out) == 5

    def test_preserves_input_order(self):
        # Stable: don't sort, don't shuffle. main() sorts separately.
        items = [_sev_thread("low"), _sev_thread("critical"), _sev_thread("high")]
        out = filter_by_min_severity(items, "high")
        assert [thread_severity(t) for t in out] == ["critical", "high"]

    def test_empty_input(self):
        assert filter_by_min_severity([], "high") == []


# ---------------------------------------------------------------------------
# Sticky receipt — state file + render variants
# ---------------------------------------------------------------------------

class TestStickyReceiptState:
    def test_state_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert sticky_state_path() == tmp_path / "state.json"

    def test_load_returns_empty_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert load_sticky_state() == {}

    def test_load_returns_empty_when_corrupt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "state.json").write_text("{ not json")
        assert load_sticky_state() == {}

    def test_save_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"comment_id": 42}})
        assert load_sticky_state() == {"o/r#1": {"comment_id": 42}}

    def test_load_returns_empty_when_valid_json_but_not_dict(
        self, tmp_path, monkeypatch
    ):
        # A valid-but-non-dict payload (list/scalar) from corruption or
        # hand-editing must not reach callers that do .values()/.items().
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        (tmp_path / "state.json").write_text("[1, 2, 3]")
        assert load_sticky_state() == {}

    def test_save_creates_parent_dir(self, tmp_path, monkeypatch):
        nested = tmp_path / "nested" / "dir"
        monkeypatch.setenv("GGRL_STATE_DIR", str(nested))
        save_sticky_state({"o/r#1": {"comment_id": 1}})
        assert (nested / "state.json").exists()


class TestStickyReceiptRender:
    @staticmethod
    def _empty_pr_payload():
        return {"reviewThreads": {"nodes": []}}

    def test_status_appears_in_header(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status="RUNNING",
        )
        assert "### gh-gemini-review-loop receipt — RUNNING" in body

    def test_sticky_embeds_marker(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status="RUNNING", sticky=True,
        )
        assert STICKY_RECEIPT_MARKER in body
        assert "Last updated:" in body
        assert "edits in place" in body

    def test_non_sticky_does_not_embed_marker(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3,
        )
        assert STICKY_RECEIPT_MARKER not in body
        assert "Generated by" in body

    def test_status_none_omits_header_suffix(self):
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        body = render_receipt(
            pr, self._empty_pr_payload(), [],
            author=BOT, resolved_outdated=0, resolved_addressed_by_reply=0,
            rereview_count=0, rereview_limit=3, status=None,
        )
        # Header line ends after "receipt" with no " — " suffix
        assert body.splitlines()[0] == "### gh-gemini-review-loop receipt"


class TestRunTracking:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_first_update_sets_started_at_and_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(self._pr(), [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(self._pr())
        assert "started_at" in run
        assert run["finding_ids"] == ["t1", "t2"]
        assert run["finding_paths"] == ["a.py", "b.py"]

    def test_second_update_unions_and_preserves_started_at(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        first_started = read_run_tracking(pr)["started_at"]
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        run = read_run_tracking(pr)
        assert run["started_at"] == first_started
        assert run["finding_ids"] == ["t1", "t2"]

    def test_clear_removes_run_but_keeps_other_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        from fetch_gemini_threads import load_sticky_state, save_sticky_state, _state_key
        save_sticky_state({_state_key(pr): {"commentId": 42}})
        update_run_tracking(pr, [("t1", "a.py")])
        clear_run_tracking(pr)
        assert read_run_tracking(pr) == {}
        assert load_sticky_state()[_state_key(pr)]["commentId"] == 42

    def test_record_cycle_appends_timed_entry(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        cycle = record_cycle(
            pr,
            started_at="2026-06-05T09:12:10Z",
            finished_at="2026-06-05T09:20:24Z",
            finding_count=3,
            outcome="continued",
        )
        assert cycle["duration_seconds"] == 494   # 8m14s of active work
        run = read_run_tracking(pr)
        assert run["cycles"] == [cycle]
        assert run["started_at"]  # cycle recording preserves run start

    def test_record_cycle_accumulates_across_cycles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        record_cycle(pr, started_at="2026-06-05T09:00:00Z",
                     finished_at="2026-06-05T09:02:00Z", finding_count=2, outcome="continued")
        record_cycle(pr, started_at="2026-06-05T09:10:00Z",
                     finished_at="2026-06-05T09:13:00Z", finding_count=1, outcome="clean")
        cycles = read_run_tracking(pr)["cycles"]
        assert [c["duration_seconds"] for c in cycles] == [120, 180]
        assert [c["outcome"] for c in cycles] == ["continued", "clean"]

    def test_record_cycle_tolerates_corrupt_state(self, tmp_path, monkeypatch):
        # A corrupt/hand-edited state (run is a string, or cycles is an int)
        # must not crash record_cycle's dict()/list() casts.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        from fetch_gemini_threads import save_sticky_state, _state_key
        save_sticky_state({_state_key(pr): {"run": "corrupt-not-a-dict"}})
        cycle = record_cycle(pr, started_at="2026-06-05T09:00:00Z",
                             finished_at="2026-06-05T09:01:00Z", finding_count=1, outcome="clean")
        assert cycle["duration_seconds"] == 60
        assert read_run_tracking(pr)["cycles"] == [cycle]  # reset cleanly, appended

    def test_record_cycle_tolerates_non_dict_state(self, tmp_path, monkeypatch):
        # load_sticky_state returning a non-dict (corrupt state.json) must not
        # AttributeError on state.get(key).
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        monkeypatch.setattr(fgt, "load_sticky_state", lambda: ["not", "a", "dict"])
        captured = {}
        monkeypatch.setattr(fgt, "save_sticky_state", lambda s: captured.update(state=s))
        cycle = fgt.record_cycle(self._pr(), started_at="2026-06-05T09:00:00Z",
                                 finished_at="2026-06-05T09:01:00Z",
                                 finding_count=1, outcome="clean")
        assert cycle["duration_seconds"] == 60
        assert isinstance(captured["state"], dict)  # rebuilt clean, not crashed

    def test_record_cycle_cli_warns_and_exits_zero_on_io_error(
        self, tmp_path, monkeypatch, capsys
    ):
        # A failed metrics write must not break the loop: warn, exit 0.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt

        def _boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(fgt, "save_sticky_state", _boom)
        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py",
            "--pr", "https://github.com/o/r/pull/9",
            "--record-cycle", "--cycle-started-at", "2026-06-05T09:00:00Z",
            "--finding-count", "2", "--cycle-outcome", "continued",
        ])
        rc = fgt.main()
        assert rc == 0
        assert "could not record cycle timing" in capsys.readouterr().err


class TestJudgeAccumulation:
    def _pr(self):
        return PullRequest(owner="o", repo="r", number=1)

    def test_accumulate_persists_verdicts_and_flag(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["judge_ran"] is True
        assert run["judge_results"] == {"t1": {"verdict": "valid_actionable"}}

    def test_accumulate_unions_and_later_cycle_supersedes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "needs_human"},
                                      "t2": {"verdict": "valid_actionable"}})
        # A later cycle re-judges t1 -> the newer verdict wins; t2 preserved.
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["judge_results"]["t1"] == {"verdict": "valid_actionable"}
        assert run["judge_results"]["t2"] == {"verdict": "valid_actionable"}

    def test_accumulate_empty_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {})
        assert read_run_tracking(pr) == {}  # no run entry created

    def test_accumulate_preserves_findings(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        update_run_tracking(pr, [("t1", "a.py")])
        started = read_run_tracking(pr)["started_at"]
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        run = read_run_tracking(pr)
        assert run["finding_ids"] == ["t1"]       # findings preserved
        assert run["started_at"] == started        # started_at preserved
        assert run["judge_results"]["t1"]["verdict"] == "valid_actionable"

    def test_clear_removes_accumulated_judge_results(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = self._pr()
        accumulate_judge_results(pr, {"t1": {"verdict": "valid_actionable"}})
        clear_run_tracking(pr)
        assert read_run_tracking(pr) == {}

    def test_merge_current_supersedes_accumulated(self):
        accumulated = {"t1": {"verdict": "needs_human"},
                       "t2": {"verdict": "valid_actionable"}}
        current = {"t1": {"verdict": "valid_actionable"}}
        merged = merge_judge_results(accumulated, current)
        assert merged["t1"] == {"verdict": "valid_actionable"}  # current wins
        assert merged["t2"] == {"verdict": "valid_actionable"}  # accumulated kept

    def test_merge_handles_none_and_empty(self):
        assert merge_judge_results(None, None) == {}
        assert merge_judge_results({"t1": {"verdict": "valid_actionable"}}, None) == {
            "t1": {"verdict": "valid_actionable"}
        }
        assert merge_judge_results(None, {"t2": {"verdict": "duplicate"}}) == {
            "t2": {"verdict": "duplicate"}
        }


class TestRecordRunIntegration:
    """Drive main() --record-run with the GitHub seams stubbed, proving the
    eval accumulated across cycles lands in the record at terminal 'clean'.

    No real network/gh calls — every GitHub-touching function is monkeypatched.
    """

    def test_clean_record_run_reports_accumulated_eval(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)

        # Cycle 1: fetched two findings and judged them; verdicts persisted.
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        accumulate_judge_results(pr, {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
        })

        # Terminal pass: the PR is now clean (both findings resolved). Stub the
        # GitHub seams so filter_threads yields zero actionable threads.
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "sort_by_severity", lambda threads: threads)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--record-run",
            "--judge-mode", "off",  # no OpenAI: the eval comes from accumulation
            "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "2", "--verification", "passed", "--outcome", "clean",
        ])

        rc = fgt.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "Ignored by judge: 1" in out      # false_positive counts as ignored
        # needs_human == 0 -> the judge needs-human line is omitted (receipt, not dashboard)
        assert "Needs human by judge" not in out

        record = json.loads(runs_log_path().read_text().strip().splitlines()[-1])
        assert record["judge"]["enabled"] is True
        assert record["judge"]["verdicts"]["valid_actionable"] == 1
        assert record["judge"]["verdicts"]["false_positive"] == 1
        assert record["findings_fetched"] == 2
        assert record["observed_fixed_count"] == 2     # both resolved this run
        # run state is cleared at record end -> no leakage into the next run
        assert read_run_tracking(pr) == {}


class TestCycleSummary:
    """Drive main() --cycle-summary with the GitHub seams stubbed: it must print
    the [loop] Summary block from accumulated state WITHOUT writing a record or
    clearing the accumulator, so it is safe to call every cycle."""

    def test_cycle_summary_prints_block_without_recording_or_clearing(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        import fetch_gemini_threads as fgt
        from metrics import runs_log_path

        pr = PullRequest(owner="o", repo="r", number=7)

        # A prior cycle fetched two findings and judged them.
        update_run_tracking(pr, [("t1", "a.py"), ("t2", "b.py")])
        accumulate_judge_results(pr, {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
        })

        # This cycle: t2 still actionable. Stub the GitHub seams.
        thread = {"id": "t2", "path": "b.py"}
        monkeypatch.setattr(fgt, "resolve_pr", lambda spec: pr)
        monkeypatch.setattr(fgt, "fetch_threads", lambda p: {"stub": True})
        monkeypatch.setattr(fgt, "filter_threads", lambda *a, **k: [thread])
        monkeypatch.setattr(fgt, "sort_by_severity", lambda threads: threads)
        monkeypatch.setattr(fgt, "rereview_requests", lambda *a, **k: ["c1"])
        monkeypatch.setattr(fgt, "addressed_by_reply_threads", lambda *a, **k: [])
        monkeypatch.setattr(fgt, "pagination_warnings", lambda pull_request: [])

        monkeypatch.setattr(sys, "argv", [
            "fetch_gemini_threads.py", "--cycle-summary",
            "--judge-mode", "off", "--no-agent-filter",
            "--no-resolve-outdated", "--no-resolve-addressed-by-reply",
            "--fixed-count", "1", "--verification", "passed",
        ])

        rc = fgt.main()
        assert rc == 0

        out = capsys.readouterr().out
        assert "[loop] Cycle receipt" in out      # mid-loop header, not terminal [loop] Summary
        assert "Findings fetched: 2" in out      # t1 + t2 accumulated
        assert "Fixed: 1" in out
        assert "Ignored by judge: 1" in out      # t2 false_positive, from accumulation

        # Read-only: no record written, accumulator NOT cleared.
        assert not runs_log_path().exists()
        run = read_run_tracking(pr)
        assert set(run.get("finding_ids", [])) == {"t1", "t2"}
        assert run.get("judge_results")  # verdicts still present for next cycle


class TestDeriveRecordFields:
    def test_observed_fixed_and_findings_and_needs_human(self):
        # baseline saw t1,t2,t3,t4; now t1 still actionable, t2 addressed-by-reply,
        # t3 & t4 gone (presumed fixed). judge off, outcome human.
        fields = derive_record_fields(
            baseline_ids={"t1", "t2", "t3", "t4"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids={"t2"},
            outcome="human",
            judge_ran=False,
            judge_results={},
        )
        assert fields["findings_fetched"] == 4
        assert fields["observed_fixed_count"] == 2          # t3, t4
        assert fields["remaining_actionable"] == 1          # t1
        assert fields["addressed_by_reply"] == 1            # t2
        assert fields["needs_human"] == 1                   # outcome human -> remaining_actionable

    def test_needs_human_from_judge_when_judge_ran(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids={"t1"},
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=True,
            judge_results={"t1": {"verdict": "needs_human", "recommended_action": "escalate"}},
        )
        assert fields["needs_human"] == 1

    def test_needs_human_zero_when_not_human_and_no_judge(self):
        fields = derive_record_fields(
            baseline_ids={"t1"},
            current_actionable_ids=set(),
            addressed_by_reply_ids=set(),
            outcome="clean",
            judge_ran=False,
            judge_results={},
        )
        assert fields["needs_human"] == 0


class TestDeriveOutcome:
    def test_cap_reached_wins(self):
        # cap_reached takes priority even if other conditions would apply
        assert _derive_outcome(0, "passed", cap_reached=True) == "capped"

    def test_verification_failed(self):
        assert _derive_outcome(0, "failed", cap_reached=False) == "verification_failed"

    def test_clean_when_no_remaining_and_passed(self):
        assert _derive_outcome(0, "passed", cap_reached=False) == "clean"

    def test_human_fallback_when_remaining(self):
        assert _derive_outcome(2, "passed", cap_reached=False) == "human"

    def test_human_fallback_when_verification_skipped(self):
        # skipped verification + no remaining is not "clean" (clean requires passed)
        assert _derive_outcome(0, "skipped", cap_reached=False) == "human"


# ---------------------------------------------------------------------------
# select_stats_records
# ---------------------------------------------------------------------------

class TestSelectStatsRecords:
    def _rec(self, repo, pr):
        return {"schema_version": 1, "repo": repo, "pr": pr}

    def test_filters_to_repo_and_takes_window(self):
        recs = [
            self._rec("o/r", 1), self._rec("x/y", 2),
            self._rec("o/r", 3), self._rec("o/r", 4),
        ]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=False)
        assert [r["pr"] for r in out] == [3, 4]   # last 2 for o/r, file order

    def test_all_repos_keeps_everything_in_window(self):
        recs = [self._rec("o/r", 1), self._rec("x/y", 2), self._rec("o/r", 3)]
        out = select_stats_records(recs, repo="o/r", window=2, all_repos=True)
        assert [r["pr"] for r in out] == [2, 3]


class TestFindActiveRun:
    """find_active_run is the cheap, network-free gate the loop hooks use to
    decide whether a Gemini loop is in flight for the current repo before
    spending a GitHub fetch on a summary."""

    def test_returns_none_when_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert find_active_run("o/r") is None

    def test_returns_none_when_run_missing_update_seq(self, tmp_path, monkeypatch):
        # A `run` block that never bumped update_seq (legacy/pre-feature cruft,
        # or cleared) is NOT an active loop, even if it has started_at.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"run": {"started_at": "2026-06-06T10:00:00Z"}}})
        assert find_active_run("o/r") is None

    def test_returns_none_when_no_run_block(self, tmp_path, monkeypatch):
        # A sticky-receipt-only entry (no `run`) is not an active loop.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#5": {"sticky_comment_id": 123}})
        assert find_active_run("o/r") is None

    def test_returns_pr_and_run_for_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        run = {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1, "finding_ids": ["a"]}
        save_sticky_state({"o/r#7": {"run": run}})
        assert find_active_run("o/r") == (7, run)

    def test_ignores_other_repos(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"other/repo#1": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}}})
        assert find_active_run("o/r") is None

    def test_does_not_prefix_match_similar_repo(self, tmp_path, monkeypatch):
        # "o/r2" must not be treated as belonging to "o/r".
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r2#1": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}}})
        assert find_active_run("o/r") is None

    def test_picks_most_recently_started_when_multiple_active(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"run": {"started_at": "2026-06-06T09:00:00Z", "update_seq": 1}},
            "o/r#2": {"run": {"started_at": "2026-06-06T11:00:00Z", "update_seq": 1}},
        })
        num, _ = find_active_run("o/r")
        assert num == 2


class TestAnyActiveRun:
    """Fast, repo-agnostic gate: lets the Stop hook skip git/repo resolution
    entirely when no loop is in flight anywhere."""

    def test_false_when_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert any_active_run() is False

    def test_false_when_only_sticky_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"sticky_comment_id": 9}})
        assert any_active_run() is False

    def test_false_when_run_block_lacks_update_seq(self, tmp_path, monkeypatch):
        # Legacy/pre-feature run (started_at but no update_seq) is not active —
        # this is what kept stale cruft from any repo looking "active" forever.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"started_at": "2026-06-06T10:00:00Z"}}})
        assert any_active_run() is False

    def test_true_when_any_active_run_exists(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"sticky_comment_id": 9},
            "x/y#3": {"run": {"started_at": "2026-06-06T10:00:00Z", "update_seq": 1}},
        })
        assert any_active_run() is True


class TestStateCorruptionTolerance:
    """The hooks read a local state.json that can be corrupted or hand-edited.
    Reading it must never crash — bad values are treated as "not active /
    not stale", never raised."""

    def test_summary_is_stale_tolerates_non_numeric_seq(self):
        assert summary_is_stale({"update_seq": "garbage", "last_summary_seq": 1}) is False
        assert summary_is_stale({"update_seq": None, "last_summary_seq": None}) is False

    def test_summary_is_stale_still_works_with_one_corrupt_field(self):
        # Valid update_seq, corrupt last_summary_seq -> treat corrupt as 0.
        assert summary_is_stale({"update_seq": 5, "last_summary_seq": "x"}) is True

    def test_any_active_run_ignores_non_numeric_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({"o/r#1": {"run": {"update_seq": "garbage"}}})
        assert any_active_run() is False

    def test_find_active_run_tolerates_mixed_started_at_types(self, tmp_path, monkeypatch):
        # A corrupted non-str started_at must not crash the sort across candidates.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        save_sticky_state({
            "o/r#1": {"run": {"started_at": "2026-06-06T09:00:00Z", "update_seq": 1}},
            "o/r#2": {"run": {"started_at": 12345, "update_seq": 1}},
        })
        result = find_active_run("o/r")  # must not raise
        assert result is not None

    def test_stamp_summary_emitted_tolerates_corrupt_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        save_sticky_state({"o/r#1": {"run": {"update_seq": "garbage"}}})
        stamp_summary_emitted(pr)  # must not raise


class TestSummaryStaleness:
    """The loop's Stop-hook backstop fires a summary only when the run has
    advanced (a new fetch bumped update_seq) since the last emitted summary.
    This dedups against the agent's own per-cycle summary without timing
    guesswork."""

    def test_update_run_tracking_bumps_update_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        assert read_run_tracking(pr)["update_seq"] == 1
        update_run_tracking(pr, [("t2", "b.py")])
        assert read_run_tracking(pr)["update_seq"] == 2

    def test_fresh_run_with_no_updates_is_not_stale(self):
        # started_at but never fetched: nothing to summarize yet.
        assert summary_is_stale({"started_at": "2026-06-06T10:00:00Z"}) is False

    def test_run_with_updates_and_no_summary_is_stale(self):
        assert summary_is_stale({"update_seq": 1}) is True

    def test_run_summarized_at_current_seq_is_not_stale(self):
        assert summary_is_stale({"update_seq": 2, "last_summary_seq": 2}) is False

    def test_run_advanced_since_last_summary_is_stale(self):
        assert summary_is_stale({"update_seq": 3, "last_summary_seq": 2}) is True

    def test_stamp_summary_emitted_marks_current_seq(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        assert summary_is_stale(read_run_tracking(pr)) is True
        stamp_summary_emitted(pr)
        assert summary_is_stale(read_run_tracking(pr)) is False

    def test_stamp_then_new_fetch_is_stale_again(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        update_run_tracking(pr, [("t1", "a.py")])
        stamp_summary_emitted(pr)
        update_run_tracking(pr, [("t2", "b.py")])  # next cycle's fetch
        assert summary_is_stale(read_run_tracking(pr)) is True

    def test_stamp_is_noop_when_no_active_run(self, tmp_path, monkeypatch):
        # Must not crash or create a run block when there's nothing tracked.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        pr = PullRequest(owner="o", repo="r", number=1, url=None)
        stamp_summary_emitted(pr)
        assert read_run_tracking(pr) == {}


from fetch_gemini_threads import resolve_judge_phase


def test_resolve_judge_phase_infers_complete_for_record_run():
    assert resolve_judge_phase(None, record_run=True) == "complete"


def test_resolve_judge_phase_infers_cycle_for_normal_fetch():
    assert resolve_judge_phase(None, record_run=False) == "cycle"


def test_resolve_judge_phase_explicit_flag_wins():
    assert resolve_judge_phase("cycle", record_run=True) == "cycle"
    assert resolve_judge_phase("complete", record_run=False) == "complete"


class TestFormatJudgeVerdictSummary:
    def _results(self, verdicts: dict[str, int]) -> dict[str, dict]:
        """Build fake judge_results keyed by thread id."""
        results = {}
        i = 0
        for verdict, count in verdicts.items():
            for _ in range(count):
                results[f"id{i}"] = {"status": "ok", "verdict": verdict}
                i += 1
        return results

    def test_single_verdict_type(self):
        results = self._results({"valid_actionable": 3})
        out = format_judge_verdict_summary(results, "cycle")
        assert "[loop] judge (cycle):" in out
        assert "3 thread(s) evaluated" in out
        assert "valid_actionable: 3" in out

    def test_multiple_verdict_types_sorted_by_count(self):
        results = self._results({"false_positive": 1, "valid_actionable": 2})
        out = format_judge_verdict_summary(results, "complete")
        assert "[loop] judge (complete):" in out
        # valid_actionable (2) should come before false_positive (1) by count.
        assert out.index("valid_actionable") < out.index("false_positive")

    def test_skipped_threads_excluded_from_count(self):
        results = {
            "id0": {"status": "ok", "verdict": "valid_actionable"},
            "id1": {"status": "skipped", "verdict": None},
        }
        out = format_judge_verdict_summary(results, "cycle")
        # Only 1 thread evaluated (the skipped one doesn't count).
        assert "1 thread(s) evaluated" in out

    def test_all_skipped_shows_fallback(self):
        results = {
            "id0": {"status": "skipped", "skip_reason": "no key"},
            "id1": {"status": "skipped", "skip_reason": "no key"},
        }
        out = format_judge_verdict_summary(results, "cycle")
        assert "all skipped" in out

    def test_phase_label_in_output(self):
        results = self._results({"needs_human": 1})
        assert "cycle" in format_judge_verdict_summary(results, "cycle")
        assert "complete" in format_judge_verdict_summary(results, "complete")

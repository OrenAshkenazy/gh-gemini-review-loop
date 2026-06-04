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
    addressed_by_reply_threads,
    clear_run_tracking,
    effective_rereview_limit,
    filter_by_min_severity,
    filter_threads,
    is_addressed_by_reply,
    load_preferences_with_fallback,
    load_sticky_state,
    nonnegative_int,
    pagination_warnings,
    parse_pr_url,
    read_run_tracking,
    render_receipt,
    rereview_requests,
    save_sticky_state,
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

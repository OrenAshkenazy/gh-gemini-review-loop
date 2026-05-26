"""Unit tests for pure functions in fetch_gemini_threads.py.

These tests intentionally avoid network/gh calls — they exercise only the
pure helpers that operate on already-fetched GraphQL payloads.
"""

import pytest

from fetch_gemini_threads import (
    ADDRESSED_BY_REPLY_MIN_CHARS,
    PAGE_LIMIT_REVIEW_THREADS,
    PAGE_LIMIT_THREAD_COMMENTS,
    PullRequest,
    addressed_by_reply_threads,
    filter_by_min_severity,
    filter_threads,
    is_addressed_by_reply,
    pagination_warnings,
    parse_pr_url,
    render_receipt,
    rereview_requests,
    severity_counts,
    sort_by_severity,
    thread_fingerprint,
    thread_severity,
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

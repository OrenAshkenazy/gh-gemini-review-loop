import json
from pathlib import Path

import pytest

import cluster_findings
import sweep_siblings
from cluster_findings import cluster, pattern_signature, recurrence_stats


_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_corpus_pr46.json").read_text()
)
_PR195_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_corpus_pr195.json").read_text()
)
_PR195_ANCHORS = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_corpus_pr195_anchors.json").read_text()
)
_PR195_SOURCES = Path(__file__).parent / "fixtures" / "cluster_corpus_pr195_sources"


def _thread(body: str, path: str = "a.py") -> dict:
    return {"path": path, "comments": [{"body": body}]}


def test_same_issue_different_identifiers_shares_signature():
    a = _thread(
        "![medium](x.svg) Validate that `source` is a dict before calling "
        "`source.get('files')` on line 204.",
        path="render_demo_ui.py",
    )
    b = _thread(
        "![medium](y.svg) Validate that `provenance` is a dict before calling "
        "`provenance.get('sources')` on line 336.",
        path="render_pr_readiness.py",
    )
    assert pattern_signature(a) == pattern_signature(b)


def test_different_issue_has_different_signature():
    guard = _thread("![medium](x.svg) Validate that `x` is a dict before `.get`.")
    indent = _thread(
        "![high](x.svg) Tab-vs-space: leading tabs are not detected because "
        "`lstrip(' ')` only strips spaces."
    )
    assert pattern_signature(guard) != pattern_signature(indent)


def test_malformed_thread_does_not_raise():
    assert pattern_signature(None) == ""
    assert pattern_signature({}) == ""
    assert pattern_signature({"comments": []}) == ""


def test_possessive_and_contraction_not_eaten():
    a = _thread("![medium](x.svg) Ensure the source's value isn't None before use.")
    # Normalization must keep the prose words, not collapse them to a literal.
    sig = pattern_signature(a)
    assert sig  # non-empty
    # A genuinely different sentence must differ.
    b = _thread("![medium](x.svg) Add a docstring to the helper.")
    assert pattern_signature(a) != pattern_signature(b)


def test_first_body_handles_graphql_nodes_shape():
    t = {"path": "a.py", "comments": {"nodes": [{"body": "![low](x.svg) Use isinstance check."}]}}
    assert pattern_signature(t)  # non-empty, doesn't raise

def _thread_full(body, path, line, sev_alt):
    return {
        "path": path,
        "line": line,
        "comments": [{"body": f"![{sev_alt}](x.svg) {body}"}],
    }


def test_cluster_groups_and_picks_max_severity_and_sorts():
    threads = [
        _thread_full("Validate x is a dict before .get", "render_demo_ui.py", 204, "medium"),
        _thread_full("Validate y is a dict before .get", "render_pr_readiness.py", 336, "medium"),
        _thread_full("Leading tabs not detected by lstrip space", "config_parser.py", 68, "high"),
    ]
    clusters = cluster(threads)
    assert len(clusters) == 2
    assert clusters[0].severity == "high"
    assert clusters[0].count == 1
    assert clusters[0].sites == ("config_parser.py:68",)
    assert clusters[1].severity == "medium"
    assert clusters[1].count == 2
    assert "render_demo_ui.py:204" in clusters[1].sites


def test_cluster_ignores_non_dict_members():
    clusters = cluster([None, "nope", {}])
    assert clusters == []

def _corpus_threads():
    return [
        {"path": f["path"], "line": f["line"], "comments": [{"body": f["body"]}]}
        for f in _CORPUS
    ]


def _pr195_thread(index):
    finding = _PR195_CORPUS[index]
    anchor = _PR195_ANCHORS[index]
    return {
        "path": f"{anchor['reviewSha']}/{anchor['path']}",
        "line": anchor["line"],
        "startLine": anchor["startLine"],
        "comments": [{"body": finding["body"]}],
    }


def test_real_corpus_clusters_into_intent_categories():
    clusters = cluster(_corpus_threads())
    by_label = {c.label: c for c in clusters}
    assert by_label["io-decode-guard"].count == 4
    assert by_label["type-guard"].count == 4
    assert by_label["exception-wrap"].count == 2
    # 15 real findings collapse to 8 groups (3 clusters + 5 singletons), not 15.
    assert len(clusters) == 8


def test_recurrence_stats_basic():
    current = ["sigA", "sigA", "sigB"]
    prior = {"sigA"}
    swept = {"sigA"}
    stats = recurrence_stats(current, prior_sigs=prior, swept_sigs=swept)
    assert stats["distinct_patterns"] == 2
    assert stats["recurrence_rate"] == 2 / 3
    assert stats["recurred_after_sweep"] == ["sigA"]


def test_recurrence_stats_empty():
    stats = recurrence_stats([], prior_sigs=set(), swept_sigs=set())
    assert stats["distinct_patterns"] == 0
    assert stats["recurrence_rate"] == 0.0
    assert stats["recurred_after_sweep"] == []


def test_recurrence_no_false_alarm_when_swept_same_cycle():
    # Pattern swept this cycle and still present, but NOT seen in a prior cycle
    # (its fix has not been re-reviewed yet) → must NOT flag recurred_after_sweep.
    stats = recurrence_stats(
        ["type-guard", "type-guard"],
        prior_sigs=set(),            # first time seen this session
        swept_sigs={"type-guard"},   # marked swept this cycle
    )
    assert stats["recurred_after_sweep"] == []


def test_recurrence_flags_when_swept_prior_and_reappears():
    # Swept in a prior cycle AND seen before AND present now → genuine recurrence.
    stats = recurrence_stats(
        ["type-guard"],
        prior_sigs={"type-guard"},
        swept_sigs={"type-guard"},
    )
    assert stats["recurred_after_sweep"] == ["type-guard"]


def test_recurrence_uses_prior_from_history_union():
    # Simulates a resumed run: live prior is empty, but history supplies the
    # prior+swept sets. The pattern present now must be flagged as recurred.
    live_prior = set()
    live_swept = set()
    history = {"seen": {"type-guard"}, "swept": {"type-guard"}}
    stats = recurrence_stats(
        ["type-guard", "type-guard", "io-decode-guard"],
        prior_sigs=live_prior | history["seen"],
        swept_sigs=live_swept | history["swept"],
    )
    assert stats["recurrence_rate"] == 2 / 3        # type-guard seen before
    assert stats["recurred_after_sweep"] == ["type-guard"]


class TestShapeMerging:
    """Prose clustering fails when a reviewer words one defect two ways.

    On PR #67 Sourcery posted "exception-wrap" and "add type and error checks
    around the parsed JSON" for the same unguarded json.loads. They hashed
    apart, so a pattern at two sites looked like two patterns at one site each
    and never reached the sweep's two-site minimum. The code did not vary the
    way the prose did.
    """

    @staticmethod
    def _thread(path, line, body):
        return {"path": path, "line": line,
                "comments": {"nodes": [{"author": {"login": "bot"}, "body": body}]}}

    @pytest.fixture
    def repo(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "svc.py").write_text(
            'data = json.loads(path.read_text(encoding="utf-8"))\n'      # 1
            'other = json.loads(path.read_text(encoding="utf-8"))\n'     # 2
            'conn = psycopg2.connect(dsn, sslmode="require")\n'          # 3
            'digest = hashlib.md5(raw).hexdigest()\n'                    # 4
            '# json.loads(path.read_text()) in a comment\n'              # 5
        )
        return tmp_path

    def test_two_wordings_of_one_shape_merge(self, repo):
        threads = [
            self._thread("app/svc.py", 1, "exception-wrap: wrap this in try/except"),
            self._thread("app/svc.py", 2, "**suggestion (bug_risk):** add type and error checks"),
        ]
        assert len(cluster_findings.cluster(threads)) == 2, "prose alone splits them"

        merged = cluster_findings.cluster(threads, root=repo)
        assert len(merged) == 1
        assert merged[0].count == 2
        assert merged[0].signature.startswith("shape:")

    def test_unrelated_findings_do_not_merge(self, repo):
        threads = [
            self._thread("app/svc.py", 1, "wrap this in try/except"),
            self._thread("app/svc.py", 3, "connection is never closed"),
            self._thread("app/svc.py", 4, "md5 is unsuitable for passwords"),
        ]
        clusters = cluster_findings.cluster(threads, root=repo)
        assert len(clusters) == 3
        assert all(c.count == 1 for c in clusters)

    def test_omitting_root_preserves_prose_only_behaviour(self, repo):
        threads = [
            self._thread("app/svc.py", 1, "wrap this in try/except"),
            self._thread("app/svc.py", 2, "add type and error checks"),
        ]
        assert cluster_findings.cluster(threads) == cluster_findings.cluster(threads)
        assert len(cluster_findings.cluster(threads)) == 2

    def test_a_comment_anchor_is_never_shape_merged(self, repo):
        threads = [
            self._thread("app/svc.py", 1, "wrap this in try/except"),
            self._thread("app/svc.py", 5, "this comment is stale"),
        ]
        clusters = cluster_findings.cluster(threads, root=repo)
        assert len(clusters) == 2, "prose describes code, a comment line is not a sibling"

    def test_unreadable_anchors_fall_back_to_prose(self, repo):
        threads = [
            self._thread("app/gone.py", 1, "wrap this in try/except"),
            self._thread("app/missing.py", 2, "add type and error checks"),
        ]
        clusters = cluster_findings.cluster(threads, root=repo)
        assert len(clusters) == 2
        assert all(not c.signature.startswith("shape:") for c in clusters)

    def test_merging_is_order_independent(self, repo):
        a = self._thread("app/svc.py", 1, "wrap this in try/except")
        b = self._thread("app/svc.py", 2, "add type and error checks")
        forward = cluster_findings.cluster([a, b], root=repo)
        reverse = cluster_findings.cluster([b, a], root=repo)
        assert [c.signature for c in forward] == [c.signature for c in reverse]
        assert [sorted(c.sites) for c in forward] == [sorted(c.sites) for c in reverse]

    def test_one_shape_cannot_absorb_unbounded_prose_clusters(self, repo, monkeypatch):
        """The cap bounds shape merging, not prose clustering.

        Prose clustering has never had a cap and this does not add one; what is
        bounded is how many *distinct* prose clusters one shape may pull
        together, so an over-broad shape cannot collapse a whole review.
        """
        monkeypatch.setattr(cluster_findings, "MAX_SHAPE_GROUP", 2)
        (repo / "app" / "many.py").write_text(
            "".join(f'v{i} = json.loads(path.read_text(encoding="utf-8"))\n' for i in range(6))
        )
        # Distinct wording, so each finding is its own prose cluster going in.
        wordings = [
            "wrap this in try/except",
            "add type checks around the parsed payload",
            "malformed input will raise here",
            "guard against a decode failure",
            "this can throw on bad data",
            "validate before returning",
        ]
        threads = [self._thread("app/many.py", i + 1, w) for i, w in enumerate(wordings)]
        assert len(cluster_findings.cluster(threads)) == 6, "prose keeps them apart"

        groups = cluster_findings.shape_groups(threads, repo)
        assert groups, "the shape is shared, so some merging must happen"
        assert all(len(g) <= 2 for g in groups)

    def test_pr195_cycle_one_has_a_multi_site_shape_cluster(self):
        clusters = cluster_findings.cluster(
            [_pr195_thread(index) for index in range(7)],
            root=_PR195_SOURCES,
        )

        assert max(cluster.count for cluster in clusters) >= 2

    def test_pr195_captured_anchors_match_their_review_sources(self):
        assert len(_PR195_ANCHORS) == len(_PR195_CORPUS) == 16
        assert all(
            cluster_findings._anchored_tokens(_pr195_thread(index), _PR195_SOURCES)
            for index in range(len(_PR195_CORPUS))
        )

    def test_pr195_repeated_keyword_policy_range_is_one_cluster(self):
        repeated = [_pr195_thread(index) for index in (2, 9, 13)]

        clusters = cluster_findings.cluster(repeated, root=_PR195_SOURCES)

        assert len(clusters) == 1
        assert clusters[0].count == 3

    def test_pr195_generic_span_locals_do_not_merge_unrelated_findings(self):
        groups = cluster_findings.shape_groups(
            [_pr195_thread(index) for index in range(len(_PR195_CORPUS))],
            _PR195_SOURCES,
        )

        assert not any({12, 14}.issubset(group) for group in map(set, groups))

    def test_anchored_tokens_union_the_whole_span(self):
        thread = _pr195_thread(13)

        actual = cluster_findings._anchored_tokens(thread, _PR195_SOURCES)
        source = (_PR195_SOURCES / thread["path"]).read_text().splitlines()
        span = source[thread["startLine"] - 1:thread["line"]]
        expected = set().union(*(
            sweep_siblings.tokenize(line)
            for line in span
            if sweep_siblings.is_code_line(line)
        ), set())

        assert actual == expected
        assert actual != sweep_siblings.tokenize(source[thread["line"] - 1])

    def test_anchored_tokens_fall_back_to_end_for_single_line(self, repo):
        thread = self._thread("app/svc.py", 1, "wrap this in try/except")
        thread["startLine"] = None
        thread["originalStartLine"] = None

        assert cluster_findings._anchored_tokens(thread, repo) == (
            sweep_siblings.tokenize('data = json.loads(path.read_text(encoding="utf-8"))')
        )

    def test_current_end_without_current_start_does_not_mix_original_range(self, repo):
        thread = self._thread("app/svc.py", 3, "connection is never closed")
        thread["startLine"] = None
        thread["originalLine"] = 2
        thread["originalStartLine"] = 1

        assert cluster_findings._anchored_tokens(thread, repo) == (
            sweep_siblings.tokenize('conn = psycopg2.connect(dsn, sslmode="require")')
        )

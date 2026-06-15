from cluster_findings import pattern_signature


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


from cluster_findings import Cluster, cluster


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
        _thread_full("Leading tabs not detected by lstrip space", "mergeproof_config.py", 68, "high"),
    ]
    clusters = cluster(threads)
    assert len(clusters) == 2
    assert clusters[0].severity == "high"
    assert clusters[0].count == 1
    assert clusters[0].sites == ["mergeproof_config.py:68"]
    assert clusters[1].severity == "medium"
    assert clusters[1].count == 2
    assert "render_demo_ui.py:204" in clusters[1].sites


def test_cluster_ignores_non_dict_members():
    clusters = cluster([None, "nope", {}])
    assert clusters == []


import json
from pathlib import Path
from cluster_findings import cluster

_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_corpus_pr46.json").read_text()
)


def _corpus_threads():
    return [
        {"path": f["path"], "line": f["line"], "comments": [{"body": f["body"]}]}
        for f in _CORPUS
    ]


def test_real_corpus_clusters_into_intent_categories():
    clusters = cluster(_corpus_threads())
    by_label = {c.label: c for c in clusters if c.label in {
        "io-decode-guard", "type-guard", "exception-wrap"}}
    assert by_label["io-decode-guard"].count == 4
    assert by_label["type-guard"].count == 4
    assert by_label["exception-wrap"].count == 2
    # 15 findings collapse to 8 groups (3 clusters + 5 singletons), not 15.
    assert len(clusters) == 8


from cluster_findings import recurrence_stats


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


import json
from pathlib import Path
from cluster_findings import cluster as cluster_fn


_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "cluster_corpus_pr46.json").read_text()
)


def _corpus_threads():
    return [
        {"path": f["path"], "line": f["line"], "comments": [{"body": f["body"]}]}
        for f in _CORPUS
    ]


def test_real_corpus_clusters_into_intent_categories():
    clusters = cluster_fn(_corpus_threads())
    by_label = {c.label: c for c in clusters}
    assert by_label["io-decode-guard"].count == 4
    assert by_label["type-guard"].count == 4
    assert by_label["exception-wrap"].count == 2
    # 15 real findings collapse to 8 groups (3 clusters + 5 singletons), not 15.
    assert len(clusters) == 8

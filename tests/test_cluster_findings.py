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

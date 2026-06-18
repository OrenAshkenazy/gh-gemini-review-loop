from publish_html_report import pages_url, publish_report, with_report_link

REPO = "OrenAshkenazy/mergeproof-demo-payments-api"


class _FakeGH:
    """Dispatch gh-api arg lists to canned responses; record every call."""

    def __init__(self, *, branch_exists: bool, file_sha: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.branch_exists = branch_exists
        self.file_sha = file_sha

    def __call__(self, args: list[str]):
        self.calls.append(args)
        if "--method" in args:  # POST/PUT
            path = args[args.index("--method") + 2]
            if path.endswith("/git/refs"):
                return {"ref": "refs/heads/gh-pages"}
            if "/contents/" in path:
                return {"content": {"sha": "newblob"}}
            if path.endswith("/pages"):
                return {"html_url": "https://x.github.io"}
            return {}
        path = args[0]  # GET
        if "/git/ref/heads/gh-pages" in path:
            if not self.branch_exists:
                raise RuntimeError("gh api failed: HTTP 404")
            return {"object": {"sha": "ghpagessha"}}
        if "/git/ref/heads/" in path:  # default branch head
            return {"object": {"sha": "mainsha"}}
        if "/contents/" in path:
            if self.file_sha:
                return {"sha": self.file_sha}
            raise RuntimeError("gh api failed: HTTP 404")
        if path == f"repos/{REPO}":
            return {"default_branch": "main"}
        return {}


def _put_call(calls, needle="index.html"):
    return next(
        c for c in calls
        if "--method" in c and c[c.index("--method") + 1] == "PUT" and any(needle in a for a in c)
    )


def test_pages_url():
    assert pages_url(REPO, 1) == "https://OrenAshkenazy.github.io/mergeproof-demo-payments-api/pr-1/"


def test_publish_creates_branch_and_file_when_absent():
    gh = _FakeGH(branch_exists=False, file_sha=None)
    url = publish_report(REPO, 1, "<html>hi</html>", runner=gh)
    assert url == pages_url(REPO, 1)
    # a branch-create POST happened
    assert any("--method" in c and c[c.index("--method") + 2].endswith("/git/refs") for c in gh.calls)
    # the report PUT created the file WITHOUT a sha (new file)
    put = _put_call(gh.calls, "pr-1/index.html")
    assert f"repos/{REPO}/contents/pr-1/index.html" in put
    assert not any(a.startswith("sha=") for a in put)
    # a .nojekyll marker was written so the static report serves without Jekyll
    assert any(
        "--method" in c and c[c.index("--method") + 2].endswith("/contents/.nojekyll")
        for c in gh.calls
    )
    # Pages enable was attempted
    assert any("--method" in c and c[c.index("--method") + 2].endswith("/pages") for c in gh.calls)


def test_publish_updates_existing_file_with_sha_and_no_branch_create():
    gh = _FakeGH(branch_exists=True, file_sha="oldblob")
    publish_report(REPO, 2, "<html>v2</html>", runner=gh)
    # no branch-create when gh-pages already exists
    assert not any("--method" in c and c[c.index("--method") + 2].endswith("/git/refs") for c in gh.calls)
    # the PUT includes the existing blob sha (update in place)
    put = _put_call(gh.calls)
    assert any(a == "sha=oldblob" for a in put)


def test_with_report_link_inserts_under_title():
    md = "<!-- mergeproof-pr-readiness -->\n## MergeProof PR Readiness\n\n**Status:** READY\n"
    out = with_report_link(md, "https://x.github.io/r/pr-1/")
    assert "[Open the full readiness report](https://x.github.io/r/pr-1/)" in out
    # link sits between the title and the Status line
    assert out.index("Open the full readiness report") < out.index("**Status:**")


def test_with_report_link_appends_when_no_title():
    out = with_report_link("no title here", "https://x/")
    assert out.rstrip().endswith("(https://x/)**")


def test_pages_enable_failure_warns_but_still_returns_url(capsys):
    class _PagesFail(_FakeGH):
        def __call__(self, args):
            if "--method" in args and args[args.index("--method") + 2].endswith("/pages"):
                raise RuntimeError("HTTP 422: plan does not support Pages")
            return super().__call__(args)

    gh = _PagesFail(branch_exists=True, file_sha="old")
    url = publish_report(REPO, 3, "<html/>", runner=gh)
    assert url == pages_url(REPO, 3)  # upload still succeeded; URL returned
    err = capsys.readouterr().err
    assert "could not enable GitHub Pages" in err


def test_pages_already_enabled_is_not_a_warning(capsys):
    class _PagesAlreadyEnabled(_FakeGH):
        def __call__(self, args):
            if "--method" in args and args[args.index("--method") + 2].endswith("/pages"):
                raise RuntimeError("gh: GitHub Pages is already enabled. (HTTP 409)")
            return super().__call__(args)

    gh = _PagesAlreadyEnabled(branch_exists=True, file_sha="old")
    url = publish_report(REPO, 3, "<html/>", runner=gh)

    err = capsys.readouterr().err
    assert url == pages_url(REPO, 3)
    assert err == ""

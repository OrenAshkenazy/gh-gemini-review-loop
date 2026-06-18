from publish_infra_pr import branch_name, compare_url, open_or_create_pr, stage_branch


def test_branch_name_is_deterministic_and_safe():
    assert branch_name("worker_deployment", {"worker_name": "refund_worker"}) == "mergeproof/worker_deployment-refund_worker"
    assert branch_name("secret_wiring", {"secret_name": "stripe/webhook"}) == "mergeproof/secret_wiring-stripe-webhook"


def test_compare_url_encodes_title_and_body():
    url = compare_url(
        repo="acme/platform-infra", base="main", branch="mergeproof/worker_deployment-refund_worker",
        title="MergeProof: worker_deployment for payments-api",
        body="Approver @platform-runtime\nSource: https://github.com/o/r/pull/1",
    )
    assert url.startswith("https://github.com/acme/platform-infra/compare/main...mergeproof/worker_deployment-refund_worker?")
    assert "expand=1" in url
    assert "title=MergeProof%3A+worker_deployment+for+payments-api" in url
    assert "%40platform-runtime" in url


def test_stage_branch_dry_run_does_not_push():
    calls = []
    def runner(args):
        calls.append(args)
        return ""
    result = stage_branch(
        repo="acme/platform-infra", base="main", branch="mergeproof/x",
        files={"envs/prod/a.yaml": "x: 1\n"},
        commit_message="msg", dry_run=True, runner=runner,
    )
    assert result["pushed"] is False
    assert result["generated_files"] == ["envs/prod/a.yaml"]
    assert calls == []


def test_stage_branch_live_push_invokes_runner():
    calls = []
    def runner(args):
        calls.append(args)
        return ""
    result = stage_branch(
        repo="acme/platform-infra", base="main", branch="mergeproof/x",
        files={"envs/prod/a.yaml": "x: 1\n"},
        commit_message="msg", dry_run=False, runner=runner,
    )
    assert result["pushed"] is True
    assert any("fetch" in " ".join(c) for c in calls)
    assert any("push" in " ".join(c) for c in calls)


def test_stage_branch_ignores_missing_existing_branch_on_first_push():
    calls = []

    def runner(args):
        calls.append(args)
        if args and args[0] == "-C" and "fetch" in args:
            raise RuntimeError("couldn't find remote ref")
        return ""

    result = stage_branch(
        repo="acme/platform-infra", base="main", branch="mergeproof/new",
        files={"envs/prod/a.yaml": "x: 1\n"},
        commit_message="msg", dry_run=False, runner=runner,
    )

    assert result["pushed"] is True
    assert any("push" in " ".join(c) for c in calls)


def test_branch_name_no_trailing_dash_for_blank_primary():
    assert branch_name("worker_deployment", {"worker_name": "   "}) == "mergeproof/worker_deployment"


def test_open_or_create_pr_reuses_existing_branch_pr():
    calls = []

    def runner(args):
        calls.append(args)
        return [{"number": 4, "html_url": "https://github.com/acme/infra/pull/4"}]

    result = open_or_create_pr("acme/infra", "main", "mergeproof/x", "title", "body", runner=runner)

    assert result == {
        "action": "existing",
        "number": 4,
        "html_url": "https://github.com/acme/infra/pull/4",
    }
    assert len(calls) == 1


def test_open_or_create_pr_posts_when_branch_has_no_open_pr():
    calls = []

    def runner(args):
        calls.append(args)
        if "--method" in args:
            return {"number": 5, "html_url": "https://github.com/acme/infra/pull/5"}
        return []

    result = open_or_create_pr("acme/infra", "main", "mergeproof/x", "title", "body", runner=runner)

    assert result["action"] == "created"
    assert result["html_url"] == "https://github.com/acme/infra/pull/5"
    assert any("--method" in call and "POST" in call for call in calls)

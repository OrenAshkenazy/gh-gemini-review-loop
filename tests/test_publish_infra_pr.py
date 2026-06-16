from publish_infra_pr import branch_name, compare_url, stage_branch


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
    assert any("push" in " ".join(c) for c in calls)


def test_branch_name_no_trailing_dash_for_blank_primary():
    assert branch_name("worker_deployment", {"worker_name": "   "}) == "mergeproof/worker_deployment"

import metrics


class TestHelpers:
    def test_runs_log_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.runs_log_path() == tmp_path / "runs.jsonl"

    def test_top_dir(self):
        assert metrics.top_dir("tests/test_auth.py") == "tests"
        assert metrics.top_dir("src/auth/login.py") == "src"
        assert metrics.top_dir("") == "(unknown)"

    def test_runs_log_path_default(self, monkeypatch):
        monkeypatch.delenv("GGRL_STATE_DIR", raising=False)
        result = metrics.runs_log_path()
        assert result.name == "runs.jsonl"
        assert "gh-gemini-review-loop" in str(result)

    def test_format_duration(self):
        assert metrics.format_duration(48) == "48s"
        assert metrics.format_duration(720) == "12m"
        assert metrics.format_duration(3840) == "1h 4m"
        assert metrics.format_duration(0) == "0s"


class TestPersistence:
    def test_append_then_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 1})
        metrics.append_record({"schema_version": 1, "repo": "a/b", "pr": 2})
        records, skipped = metrics.load_records()
        assert skipped == 0
        assert [r["pr"] for r in records] == [1, 2]

    def test_load_missing_file_is_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.load_records() == ([], 0)

    def test_load_skips_corrupt_and_future_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        path = tmp_path / "runs.jsonl"
        path.write_text(
            '{"schema_version": 1, "pr": 1}\n'
            "not json at all\n"
            '{"schema_version": 999, "pr": 2}\n'
            "\n"
            '{"schema_version": 1, "pr": 3}\n'
        )
        records, skipped = metrics.load_records()
        assert [r["pr"] for r in records] == [1, 3]
        assert skipped == 2  # corrupt line + future version; blank line ignored


class TestBuildJudgeBlock:
    def test_disabled_when_not_run(self):
        assert metrics.build_judge_block(False, {}) == {"enabled": False}

    def test_counts_verdicts_and_actions(self):
        results = {
            "t1": {"verdict": "valid_actionable", "recommended_action": "fix"},
            "t2": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t3": {"verdict": "false_positive", "recommended_action": "ignore"},
            "t4": {"verdict": "needs_human", "recommended_action": "escalate"},
        }
        block = metrics.build_judge_block(True, results)
        assert block["enabled"] is True
        assert block["verdicts"]["false_positive"] == 2
        assert block["verdicts"]["needs_human"] == 1
        assert block["verdicts"]["duplicate"] == 0
        assert block["recommended_actions"]["ignore"] == 2
        assert block["recommended_actions"]["escalate"] == 1


class TestBuildRecord:
    def _kwargs(self, **over):
        base = dict(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=2,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean",
            outcome_reason="0 actionable threads remaining",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/test_auth.py", "src/auth/login.py"],
            judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_full_record_shape_and_derived_fields(self):
        rec = metrics.build_record(**self._kwargs())
        assert rec["schema_version"] == 1
        assert rec["duration_seconds"] == 720
        assert rec["finding_areas"] == ["tests", "src"]
        assert rec["finding_paths"] == ["tests/test_auth.py", "src/auth/login.py"]
        assert rec["verification_details"] == {}
        assert rec["judge"] == {"enabled": False}

    def test_missing_started_at_falls_back_to_ts(self):
        rec = metrics.build_record(**self._kwargs(started_at=None))
        assert rec["started_at"] == rec["ts"]
        assert rec["duration_seconds"] == 0

    def test_all_outcomes_accepted(self):
        for outcome in metrics.VALID_OUTCOMES:
            rec = metrics.build_record(**self._kwargs(outcome=outcome))
            assert rec["outcome"] == outcome


class TestFormatRunSummary:
    def _rec(self, **over):
        base = metrics.build_record(
            repo="o/r", pr=23, provider="gemini-code-assist",
            findings_fetched=7, fixed_count=4, observed_fixed_count=4,
            remaining_actionable=1, needs_human=1, addressed_by_reply=0,
            cycles_used=2, cycle_cap=3, verification="passed",
            verification_details={}, outcome="clean", outcome_reason="ok",
            started_at="2026-06-04T18:10:11Z", ts="2026-06-04T18:22:11Z",
            finding_paths=["tests/x.py"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_judge_off_omits_judge_lines(self):
        out = metrics.format_run_summary(self._rec())
        assert out.splitlines() == [
            "[loop] Summary",
            "Findings fetched: 7",
            "Fixed: 4",
            "Needs human: 1",
            "Cycles used: 2/3",
            "Verification: passed",
            "Time to clean PR: 12m",
        ]

    def test_addressed_by_reply_line_omitted_when_zero(self):
        assert "Addressed by reply" not in metrics.format_run_summary(self._rec())

    def test_addressed_by_reply_line_shown_when_nonzero(self):
        out = metrics.format_run_summary(self._rec(addressed_by_reply=2))
        assert "Addressed by reply: 2" in out

    def test_judge_on_inserts_two_judge_lines_after_fixed(self):
        judge = {
            "enabled": True,
            "verdicts": {
                "valid_actionable": 3, "false_positive": 1, "duplicate": 1,
                "already_addressed": 1, "explanation_only": 0, "needs_human": 1,
            },
            "recommended_actions": {"fix": 3, "reply": 1, "ignore": 2, "escalate": 1},
        }
        out = metrics.format_run_summary(self._rec(judge=judge)).splitlines()
        assert out[2] == "Fixed: 4"
        assert out[3] == "Ignored by judge: 3"   # false_positive+duplicate+already_addressed+explanation_only
        assert out[4] == "Needs human (judge): 1"
        assert out[5] == "Needs human: 1"

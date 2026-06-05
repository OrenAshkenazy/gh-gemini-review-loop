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

    def test_load_returns_empty_on_oserror(self, tmp_path, monkeypatch):
        path = tmp_path / "runs.jsonl"
        path.write_text('{"schema_version": 1, "pr": 1}\n')
        def _raise(*a, **kw):
            raise OSError("Permission denied")
        monkeypatch.setattr(metrics.Path, "read_text", _raise)
        records, skipped = metrics.load_records(path)
        assert records == []
        assert skipped == 0


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

    def test_non_dict_verification_details_coerced_to_empty(self):
        # Reachable via `--verification-details '"lint"'` (valid JSON, not an object).
        # The stored record must stay schema-valid (verification_details is an object).
        rec = metrics.build_record(**self._kwargs(verification_details="lint"))
        assert rec["verification_details"] == {}

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

    def test_judge_off_default_receipt(self):
        # observed_fixed_count == fixed_count, so no "Observed fixed" line.
        out = metrics.format_run_summary(self._rec())
        assert out.splitlines() == [
            "[loop] Summary",
            "Findings fetched: 7",
            "Fixed: 4",
            "Remaining actionable: 1",
            "Needs human: 1",
            "Cycles used: 2/3",
            "Verification: passed",
            "Outcome: clean",
            "Time to clean PR: 12m",
        ]

    def test_observed_fixed_shown_when_differs_from_fixed(self):
        out = metrics.format_run_summary(
            self._rec(fixed_count=2, observed_fixed_count=1)
        ).splitlines()
        assert out[2] == "Fixed: 2"
        assert out[3] == "Observed fixed: 1"

    def test_observed_fixed_omitted_when_equal_to_fixed(self):
        assert "Observed fixed" not in metrics.format_run_summary(self._rec())

    def test_outcome_line_present(self):
        assert "Outcome: clean" in metrics.format_run_summary(self._rec())

    def test_time_label_is_spent_when_outcome_not_clean(self):
        out = metrics.format_run_summary(self._rec(outcome="human"))
        assert "Time spent: 12m" in out
        assert "Time to clean PR" not in out

    def test_addressed_by_reply_line_omitted_when_zero(self):
        assert "Addressed by reply" not in metrics.format_run_summary(self._rec())

    def test_addressed_by_reply_line_shown_when_nonzero(self):
        out = metrics.format_run_summary(self._rec(addressed_by_reply=2))
        assert "Addressed by reply: 2" in out

    def test_failed_check_line_shown_when_verification_failed(self):
        out = metrics.format_run_summary(
            self._rec(
                verification="failed",
                verification_details={"failed_check": "lint"},
                outcome="verification_failed",
            )
        ).splitlines()
        i = out.index("Verification: failed")
        assert out[i + 1] == "Failed check: lint"
        assert out[i + 2] == "Outcome: verification_failed"

    def test_failed_check_line_omitted_when_verification_passed(self):
        assert "Failed check" not in metrics.format_run_summary(self._rec())

    def test_tolerates_non_dict_verification_details(self):
        # Defense-in-depth: a legacy/hand-crafted record with a non-dict
        # verification_details must not crash the receipt (no .get on a str).
        rec = self._rec(verification="failed", verification_details="lint")
        out = metrics.format_run_summary(rec)  # must not raise
        assert "Failed check" not in out

    def test_judge_on_inserts_renamed_lines_after_fixed(self):
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
        assert out[4] == "Needs human by judge: 1"
        assert out[5] == "Remaining actionable: 1"

    def test_partial_record_renders_defaults_without_keyerror(self):
        # format_run_summary may receive records loaded from runs.jsonl, where
        # load_records validates schema_version but not field presence. A record
        # missing fields must degrade to defaults, not raise KeyError.
        out = metrics.format_run_summary({}).splitlines()
        assert out == [
            "[loop] Summary",
            "Findings fetched: 0",
            "Fixed: 0",
            "Remaining actionable: 0",
            "Needs human: 0",
            "Cycles used: 0/0",
            "Verification: skipped",
            "Outcome: unknown",
            "Time spent: 0s",
        ]

    def test_judge_lines_omitted_when_zero(self):
        judge = {
            "enabled": True,
            "verdicts": {v: 0 for v in metrics.JUDGE_VERDICTS},
            "recommended_actions": {a: 0 for a in metrics.JUDGE_ACTIONS},
        }
        out = metrics.format_run_summary(self._rec(judge=judge))
        assert "Ignored by judge" not in out
        assert "Needs human by judge" not in out


class TestAggregate:
    def _rec(self, **over):
        base = dict(
            schema_version=1, repo="o/r", pr=1, provider="gemini-code-assist",
            findings_fetched=5, observed_fixed_count=4, needs_human=1,
            addressed_by_reply=1, cycles_used=2, duration_seconds=600,
            finding_areas=["tests"], judge={"enabled": False},
        )
        base.update(over)
        return base

    def test_empty_returns_count_zero(self):
        assert metrics.aggregate([]) == {"count": 0}

    def test_basic_aggregation(self):
        recs = [
            self._rec(cycles_used=2, duration_seconds=600, observed_fixed_count=4, findings_fetched=5),
            self._rec(cycles_used=1, duration_seconds=1200, observed_fixed_count=3, findings_fetched=4),
        ]
        agg = metrics.aggregate(recs)
        assert agg["count"] == 2
        assert agg["avg_cycles"] == 1.5
        assert agg["avg_duration"] == 900.0
        assert agg["total_fixed"] == 7
        assert agg["total_fetched"] == 9
        assert agg["top_provider"] == "gemini-code-assist"
        assert agg["top_area"] == "tests"

    def test_duration_zero_excluded_from_average(self):
        recs = [self._rec(duration_seconds=0), self._rec(duration_seconds=600)]
        assert metrics.aggregate(recs)["avg_duration"] == 600.0

    def test_false_positives_only_over_judged_runs(self):
        judged = self._rec(judge={"enabled": True, "verdicts": {"false_positive": 3}})
        unjudged = self._rec(judge={"enabled": False})
        agg = metrics.aggregate([judged, unjudged])
        assert agg["judged_count"] == 1
        assert agg["false_positives_avoided"] == 3


class TestFormatStats:
    def test_empty_message(self):
        out = metrics.format_stats("o/r", {"count": 0})
        assert "No Gemini loop runs recorded yet" in out

    def test_full_output_with_judge_footnote(self):
        agg = {
            "count": 10, "avg_cycles": 1.8, "avg_duration": 540.0,
            "total_fixed": 32, "total_fetched": 41, "needs_human": 6,
            "addressed_by_reply": 9, "judged_count": 6,
            "false_positives_avoided": 14, "top_provider": "gemini-code-assist",
            "top_area": "tests",
        }
        out = metrics.format_stats("OrenAshkenazy/gh-gemini-review-loop", agg)
        assert "Last 10 runs" in out
        assert "Average cycles used: 1.8" in out
        assert "Average time to clean PR: 9m" in out
        assert "Findings fixed: 32 of 41" in out
        assert "False positives avoided: 14   (across 6 of 10 judged runs)" in out
        assert "Most repeated finding area: tests" in out

    def test_judge_line_omitted_when_no_judged_runs(self):
        agg = {
            "count": 2, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 1, "total_fetched": 2, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": "gemini-code-assist",
            "top_area": None,
        }
        out = metrics.format_stats("o/r", agg)
        assert "False positives avoided" not in out
        assert "Average time to clean PR" not in out  # avg_duration is None

    def test_skipped_footnote(self):
        agg = {
            "count": 1, "avg_cycles": 1.0, "avg_duration": None,
            "total_fixed": 0, "total_fetched": 0, "needs_human": 0,
            "addressed_by_reply": 0, "judged_count": 0,
            "false_positives_avoided": 0, "top_provider": None, "top_area": None,
        }
        out = metrics.format_stats("o/r", agg, skipped=2)
        assert "(2 unreadable records skipped)" in out

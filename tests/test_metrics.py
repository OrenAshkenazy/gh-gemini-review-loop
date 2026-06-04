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

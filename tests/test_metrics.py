import metrics


class TestHelpers:
    def test_runs_log_path_honors_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert metrics.runs_log_path() == tmp_path / "runs.jsonl"

    def test_top_dir(self):
        assert metrics.top_dir("tests/test_auth.py") == "tests"
        assert metrics.top_dir("src/auth/login.py") == "src"
        assert metrics.top_dir("") == "(unknown)"

    def test_format_duration(self):
        assert metrics.format_duration(48) == "48s"
        assert metrics.format_duration(720) == "12m"
        assert metrics.format_duration(3840) == "1h 4m"
        assert metrics.format_duration(0) == "0s"

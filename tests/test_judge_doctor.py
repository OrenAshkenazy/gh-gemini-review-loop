"""Tests for the judge_doctor CLI.

The doctor is intentionally a thin orchestrator over judge.py — most of the
real logic (placeholder detection, network probe) is already covered in
test_judge.py. These tests focus on the doctor's *contracts*: exit codes,
which checks run, and that the read-only mode never makes network calls.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "gh-gemini-review-loop" / "skills" / "gh-gemini-review-loop" / "scripts"
)
sys.path.insert(0, str(PLUGIN_SCRIPTS))

import judge_doctor  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_key_resolver(monkeypatch, tmp_path):
    """Force the tiered resolver to read only the env var for every test.

    Without this, a developer machine with a real key in macOS Keychain
    would silently override ``monkeypatch.delenv("OPENAI_API_KEY")`` calls
    and flip exit codes. We pin GGRL_STATE_DIR to a temp dotfile location
    and stub the keystore readers so tests are host-independent.
    """
    import key_resolver  # noqa: PLC0415

    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
    monkeypatch.setitem(key_resolver._READERS, "macos_keychain", lambda: None)
    monkeypatch.setitem(key_resolver._READERS, "linux_secret_service", lambda: None)


@pytest.fixture
def _green_non_key_checks(monkeypatch):
    """Stub network + gh checks to True so test outcomes depend only on the key.

    Applied per-test (not autouse) so direct unit tests of check_network_reachability
    can still exercise the real function.
    """
    monkeypatch.setattr(judge_doctor, "check_network_reachability", lambda: True)
    monkeypatch.setattr(judge_doctor, "check_gh_cli", lambda: True)


@pytest.mark.usefixtures("_green_non_key_checks")
class TestExitCodes:
    """Doctor must return 0 only when every gating check passes."""

    def test_missing_key_returns_nonzero(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        rc = judge_doctor.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "OPENAI_API_KEY" in out
        assert "no OpenAI key" in out

    def test_placeholder_key_returns_nonzero(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "REPLACE_WITH_YOUR_KEY")
        rc = judge_doctor.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "placeholder" in out.lower()

    def test_real_looking_key_passes_key_check(self, monkeypatch, capsys):
        # If the env is otherwise healthy a plausible key shape must NOT
        # cause the key check to fail. (Other checks like gh CLI may still
        # warn, but warn-only checks don't flip the exit code.)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)
        rc = judge_doctor.main([])
        # rc could be 0 or 1 depending on the host (settings.json on dev
        # machines, etc.) — but the [3/5] OPENAI_API_KEY section must show
        # the well-formed marker, never the placeholder one.
        out = capsys.readouterr().out
        section = out.split("[3/5]")[1].split("[4/5]")[0]
        assert "placeholder" not in section.lower()
        assert "well-formed" in section
        del rc  # not asserting on rc to keep test host-independent


@pytest.mark.usefixtures("_green_non_key_checks")
class TestProbeFlag:
    """``--probe`` is the only path that touches the network."""

    def test_no_probe_makes_no_network_call(self, monkeypatch, capsys):
        # Even with everything green, the default invocation must NOT call
        # the network. We assert this by sabotaging JudgeClient.judge —
        # if anything tried to call it during a no-probe run, the test
        # would crash.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)

        def _explode(*_args, **_kwargs):
            raise AssertionError("judge() must NOT be called without --probe")

        monkeypatch.setattr(judge_doctor.JudgeClient, "judge", _explode)
        # Don't care about rc — only that no exception was raised.
        judge_doctor.main([])
        capsys.readouterr()  # drain stdout so it doesn't pollute output

    def test_probe_invokes_judge_when_checks_pass(self, monkeypatch, capsys):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)
        called = {"n": 0}

        def _fake_probe(model="x"):
            called["n"] += 1
            return True

        monkeypatch.setattr(judge_doctor, "probe_openai", _fake_probe)
        rc = judge_doctor.main(["--probe"])
        capsys.readouterr()
        # If the gating checks passed, probe must have been called exactly
        # once. If they didn't (host-dependent settings.json etc.), probe
        # must NOT have been called.
        if rc == 0:
            assert called["n"] == 1
        else:
            assert called["n"] == 0

    def test_probe_skipped_when_gating_check_fails(self, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        called = {"n": 0}

        def _fake_probe(model="x"):
            called["n"] += 1
            return True

        monkeypatch.setattr(judge_doctor, "probe_openai", _fake_probe)
        rc = judge_doctor.main(["--probe"])
        capsys.readouterr()
        assert rc == 1
        assert called["n"] == 0, "probe must not run when prior checks fail"


@pytest.mark.usefixtures("_green_non_key_checks")
class TestColorOutput:
    def test_no_color_when_not_a_tty(self, monkeypatch, capsys):
        # capsys captures a non-tty stream, so the doctor should emit no
        # ANSI escapes — keeps log files / CI output readable.
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)
        judge_doctor.main([])
        out = capsys.readouterr().out
        assert "\033[" not in out

    def test_color_survives_missing_isatty(self, monkeypatch):
        # Some environments replace sys.stdout with a stream that has no
        # isatty method (custom test runners, GUI consoles, daemons). The
        # color helper must not crash there.
        class StreamWithoutIsatty:
            pass

        monkeypatch.setattr("sys.stdout", StreamWithoutIsatty())
        # Should return plain text, no AttributeError.
        result = judge_doctor._color("hello", judge_doctor.GREEN)
        assert result == "hello"


@pytest.mark.usefixtures("_green_non_key_checks")
class TestSettingsJsonTypeCheck:
    def test_non_string_injected_key_caught(self, monkeypatch, tmp_path, capsys):
        # If settings.json injects a boolean or integer as OPENAI_API_KEY,
        # the doctor must flag it instead of letting it slip through as
        # "non-placeholder" via the defensive isinstance guard in
        # looks_like_placeholder_key.
        settings = tmp_path / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text('{"env": {"OPENAI_API_KEY": true}}')
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "a" * 48)
        rc = judge_doctor.main([])
        out = capsys.readouterr().out
        assert "non-string" in out
        assert "type=bool" in out
        assert rc == 1


class TestNetworkReachability:
    def test_failed_socket_connect_returns_false(self, monkeypatch, capsys):
        # If the OpenAI host is unreachable (firewall, no DNS, captive portal),
        # the network check must fail loudly with an actionable hint, not
        # silently pass and let the downstream API call hang.
        def _boom(*_args, **_kwargs):
            raise OSError("Network is unreachable")

        monkeypatch.setattr("socket.create_connection", _boom)
        assert judge_doctor.check_network_reachability() is False
        out = capsys.readouterr().out
        assert "cannot reach" in out
        assert "OPENAI_BASE_URL" in out  # actionable hint for self-hosted users

    def test_probes_openai_base_url_when_set(self, monkeypatch, capsys):
        # Users behind firewalls that block api.openai.com but allow their
        # gateway (Ollama / LiteLLM / enterprise proxy) would see this
        # check fail incorrectly if the probe ignored OPENAI_BASE_URL.
        monkeypatch.setenv("OPENAI_BASE_URL", "http://gateway.internal:8443/v1")
        seen = {}

        def _capture(addr, timeout):  # noqa: ARG001
            seen["addr"] = addr
            raise OSError("stop here — we only care about the target")

        monkeypatch.setattr("socket.create_connection", _capture)
        judge_doctor.check_network_reachability()
        capsys.readouterr()
        assert seen["addr"] == ("gateway.internal", 8443)

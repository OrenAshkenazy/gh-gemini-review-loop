"""Sentinel-file fast path for the bundled hooks (#83).

The hooks.json commands are shell guards that exit before spawning Python
unless the loop-active sentinel exists (and is younger than the 24h TTL).
These tests cover the sentinel lifecycle in Python (created on fetch,
cleared by the terminal --record-run path, TTL semantics) and the actual
guard commands shipped in hooks.json, run through `sh` with a stub python3
so we can observe whether the interpreter would have been spawned.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fetch_gemini_threads import (
    PullRequest,
    clear_run_tracking,
    update_run_tracking,
)
from loop_state import (
    SENTINEL_TTL_SECONDS,
    any_active_run,
    clear_sentinel,
    sentinel_is_stale,
    sentinel_path,
    touch_sentinel,
)

HOOKS_JSON = (
    Path(__file__).resolve().parent.parent
    / "plugins" / "gh-review-loop" / "hooks" / "hooks.json"
)
PLUGIN_ROOT = HOOKS_JSON.parent.parent
SCRIPTS = PLUGIN_ROOT / "skills" / "gh-review-loop" / "scripts"


def _pr(owner: str = "o", repo: str = "r", number: int = 1) -> PullRequest:
    return PullRequest(owner=owner, repo=repo, number=number)


class TestSentinelLifecycle:
    def test_touch_creates_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not sentinel_path().exists()
        touch_sentinel()
        assert sentinel_path().exists()

    def test_touch_refreshes_mtime(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        touch_sentinel()
        assert not sentinel_is_stale()

    def test_clear_missing_sentinel_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        clear_sentinel()  # must not raise

    def test_fetch_creates_sentinel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert sentinel_path().exists()

    def test_record_run_clears_sentinel_when_last_loop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        clear_run_tracking(_pr())
        assert not sentinel_path().exists()

    def test_record_run_keeps_sentinel_while_other_loop_active(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr("o", "r", 1), [("t1", "a.py")])
        update_run_tracking(_pr("other", "repo", 9), [("t2", "b.py")])
        clear_run_tracking(_pr("o", "r", 1))
        assert sentinel_path().exists()
        clear_run_tracking(_pr("other", "repo", 9))
        assert not sentinel_path().exists()


class TestSentinelWriteFailureIsObservable:
    """A sentinel that cannot be written turns the gates OFF, not on.

    The shell guard exits 0 when the marker is absent, so a swallowed OSError
    would silently drop the edit gate, the push gate and the Stop backstop
    while state.json still says a run is active. The failure must be reported.
    """

    @staticmethod
    def _unwritable(monkeypatch, tmp_path) -> Path:
        # A regular file where the state dir should be: mkdir(parents=True)
        # raises NotADirectoryError (an OSError) on every call.
        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory")
        monkeypatch.setenv("GGRL_STATE_DIR", str(blocker / "state"))
        return blocker

    def test_touch_returns_true_on_success(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert touch_sentinel() is True

    def test_touch_returns_false_when_it_cannot_write(self, tmp_path, monkeypatch):
        self._unwritable(monkeypatch, tmp_path)
        assert touch_sentinel() is False
        assert not sentinel_path().exists()

    def test_fetch_warns_when_the_sentinel_cannot_be_written(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("fetch_gemini_threads.touch_sentinel", lambda: False)

        update_run_tracking(_pr(), [("t1", "a.py")])

        err = capsys.readouterr().err
        assert "loop-active sentinel" in err
        # Names the concrete guarantees that are gone, not just "warning".
        assert "push gate" in err
        assert "backstop" in err

    def test_fetch_is_silent_when_the_sentinel_is_written(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert "sentinel" not in capsys.readouterr().err

    def test_run_state_still_persists_when_the_sentinel_fails(
        self, tmp_path, monkeypatch
    ):
        # Fail-open: the loop keeps working, only the gates go quiet.
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr("fetch_gemini_threads.touch_sentinel", lambda: False)
        update_run_tracking(_pr(), [("t1", "a.py")])
        assert any_active_run() is True


class TestSentinelTtl:
    def test_missing_sentinel_is_not_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        assert not sentinel_is_stale()

    def test_fresh_sentinel_is_not_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        assert not sentinel_is_stale()

    def test_sentinel_past_ttl_is_stale(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path))
        touch_sentinel()
        old = time.time() - SENTINEL_TTL_SECONDS - 60
        os.utime(sentinel_path(), (old, old))
        assert sentinel_is_stale()


class TestSlimImports:
    def test_hook_entrypoints_do_not_import_fetch_module(self, tmp_path):
        """The gates exist to be cheap; importing the 3,800-line main module
        at hook time was the bulk of the ~110ms idle cost (#83)."""
        code = (
            "import loop_summary_gate, loop_profile_gate, loop_summary_hook, sys; "
            "assert 'fetch_gemini_threads' not in sys.modules, "
            "'hook entrypoints must not import fetch_gemini_threads'"
        )
        env = dict(os.environ, GGRL_STATE_DIR=str(tmp_path))
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=SCRIPTS,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr


def _hook_commands() -> list[tuple[str, str]]:
    data = json.loads(HOOKS_JSON.read_text())
    commands = []
    for event, entries in data["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                commands.append((event, hook["command"]))
    return commands


@pytest.fixture
def guard_env(tmp_path):
    """Env for running a hooks.json command with a stub python3 on PATH.

    The stub appends to a marker file instead of executing the real gate, so
    a test can assert whether the guard would have spawned the interpreter.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    marker = tmp_path / "python-ran"
    shim = shim_dir / "python3"
    shim.write_text(f'#!/bin/sh\necho ran >> "{marker}"\n')
    shim.chmod(0o755)
    env = {
        "PATH": f"{shim_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "GGRL_STATE_DIR": str(state_dir),
        "GGRL_PLUGIN_ROOT": str(PLUGIN_ROOT),
    }
    return {"env": env, "state_dir": state_dir, "marker": marker}


def _run_guard(command: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sh", "-c", command],
        input="{}",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestHooksJsonGuard:
    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_no_sentinel_skips_python(self, guard_env, event, command):
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert not guard_env["marker"].exists(), "python3 spawned on idle path"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_fresh_sentinel_spawns_python(self, guard_env, event, command):
        (guard_env["state_dir"] / "loop-active").touch()
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned during loop"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_stale_sentinel_is_removed_and_skipped(self, guard_env, event, command):
        sentinel = guard_env["state_dir"] / "loop-active"
        sentinel.touch()
        old = time.time() - SENTINEL_TTL_SECONDS - 3600
        os.utime(sentinel, (old, old))
        proc = _run_guard(command, guard_env["env"])
        assert proc.returncode == 0, proc.stderr
        assert not guard_env["marker"].exists(), "python3 spawned on stale sentinel"
        assert not sentinel.exists(), "stale sentinel not cleaned up"

    @pytest.mark.parametrize(("event", "command"), _hook_commands())
    def test_default_state_dir_falls_back_to_home(self, guard_env, event, command):
        env = dict(guard_env["env"])
        del env["GGRL_STATE_DIR"]
        home = Path(env["HOME"])
        sentinel = home / ".config" / "gh-gemini-review-loop" / "loop-active"
        sentinel.parent.mkdir(parents=True)
        sentinel.touch()
        proc = _run_guard(command, env)
        assert proc.returncode == 0, proc.stderr
        assert guard_env["marker"].exists(), "python3 not spawned via $HOME fallback"

    def test_guard_ttl_matches_python_ttl(self):
        """hooks.json uses find -mmin +1440; keep it in sync with the constant."""
        minutes = SENTINEL_TTL_SECONDS // 60
        for _event, command in _hook_commands():
            assert f"-mmin +{minutes}" in command

    def test_no_status_messages(self):
        """statusMessage rendered on every matching tool call in every project;
        the fix removes it outright (#83)."""
        assert "statusMessage" not in HOOKS_JSON.read_text()

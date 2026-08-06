import subprocess

import pytest
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "gh-review-loop" / "skills" / "gh-review-loop" / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture(autouse=True)
def _forbid_real_gh(monkeypatch):
    """Fail loudly if a test shells out to `gh`.

    The suite is hermetic by design, and a test that reaches the network is a
    bug that only shows up on a maintainer's authenticated machine -- where it
    looks like a mystery failure in unrelated code, not like a missing stub.
    Raising here turns that into a named assertion at the call site.

    Tests that exercise gh-invoking code pass an explicit ``runner``; tests that
    exercise the surrounding logic must stub the helper they don't care about.
    """
    real_run = subprocess.run

    def guard(cmd, *args, **kwargs):
        argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
        first = str(argv[0]) if argv else ""
        if first == "gh" or first.endswith("/gh"):
            raise AssertionError(
                "hermetic test suite: a test invoked the real `gh` "
                f"({list(argv)[:4]}). Pass runner=... or stub the helper."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guard)


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path_factory, monkeypatch):
    """Point per-user state at a temp directory for every test.

    Several scripts resolve their state and preferences under GGRL_STATE_DIR,
    falling back to ~/.config/gh-gemini-review-loop. load_preferences() creates
    that file when absent, so any test reaching it writes to the developer's
    real config. Tests that need a specific directory still set the variable
    themselves and override this.
    """
    monkeypatch.setenv("GGRL_STATE_DIR", str(tmp_path_factory.mktemp("ggrl-state")))

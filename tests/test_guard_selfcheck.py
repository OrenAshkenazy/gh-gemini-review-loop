import subprocess

import pytest


def test_the_hermetic_guard_actually_fires():
    """A test that shells out to gh must fail loudly, not reach the network."""
    with pytest.raises(AssertionError, match="hermetic test suite"):
        subprocess.run(["gh", "api", "user"], capture_output=True)

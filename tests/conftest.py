"""Shared pytest fixtures for shell-script tests."""

import os
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import pytest
from hypothesis import HealthCheck, settings

from tests._helpers import copy_script_to, git_env, init_test_repo

# Hypothesis profiles. The fuzz suites assert crash-resistance over adversarial
# input, so they must be reproducible: a green CI run that hid a crash on one
# random seed is worthless. "ci" pins a deterministic derandomized run with a
# generous example budget; "dev" is faster for the local edit loop. Select with
# HYPOTHESIS_PROFILE (defaults to "ci" so a bare `pytest` is reproducible).
settings.register_profile(
    "ci",
    settings(
        max_examples=400,
        derandomize=True,
        deadline=None,  # parser timing varies on CI runners; don't flake on it
        suppress_health_check=[HealthCheck.too_slow],
        print_blob=True,
    ),
)
settings.register_profile(
    "dev",
    settings(max_examples=75, deadline=None),
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


@pytest.fixture
def empty_git_repo(tmp_path: Path) -> Iterator[Path]:
    """Throwaway git repo with an initial empty commit (so HEAD exists)."""
    init_test_repo(tmp_path)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=tmp_path,
        env=git_env(),
        check=True,
    )
    yield tmp_path


@pytest.fixture
def copy_script() -> Callable[[str, Path], Path]:
    """Return a helper that copies a repo script into a sandbox dir."""
    return copy_script_to

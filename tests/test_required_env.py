"""Smoke tests: every new GitHub-glue script must exit non-zero (with a clear
message) when a required env var is unset. This catches the regression where
a workflow change silently drops an env var, leaving the script to misbehave
on an empty value."""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / ".github" / "scripts"

# `: "${VAR:?message}"` guard lines, in file order.
_GUARD_RE = re.compile(r'^\s*:?\s*"\$\{([A-Za-z_][A-Za-z0-9_]*):\?')


def _guard_vars(script: Path) -> list[str]:
    """Every env var protected by a `${VAR:?…}` guard in `script`, in file order."""
    out: list[str] = []
    for line in script.read_text().splitlines():
        m = _GUARD_RE.match(line)
        if m:
            out.append(m.group(1))
    return out


# Derive the corpus from the live tree so a NEW `${VAR:?…}`-guarded script is
# covered automatically rather than needing a hand-maintained list to drift.
CASES = sorted(
    (script.name, guards)
    for script in SCRIPTS.glob("*.sh")
    if (guards := _guard_vars(script))
)


def test_guarded_scripts_discovered() -> None:
    # Non-vacuity: the glob must actually find guarded scripts, else the
    # parametrize below would be empty and silently exercise nothing.
    assert CASES, "no `${VAR:?…}`-guarded scripts found under .github/scripts"


@pytest.mark.parametrize("script, required_vars", CASES, ids=[c[0] for c in CASES])
def test_script_exits_when_required_var_missing(
    script: str, required_vars: list[str]
) -> None:
    # Run with a scrubbed env so the script's first `${VAR:?…}` guard fires. Run
    # from the repo root (the env these scripts expect in CI) so a script that
    # sources a helper before its guard — release-prep.sh sources retry.bash via
    # `git rev-parse --show-toplevel` — reaches the guard instead of dying earlier.
    env = {"PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", str(SCRIPTS / script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, (
        f"{script} should exit non-zero with no env vars set, got 0"
    )
    # Tighten past a loose substring: bash's `${VAR:?msg}` prints "VAR: msg", so
    # require the guard-line shape `VAR: ` (not just the bare name appearing
    # anywhere). Only the first guard fires under `set -e`, so any one suffices.
    err = result.stderr
    assert any(f"{var}: " in err for var in required_vars), (
        f"{script} stderr should cite a guard `VAR: ` for one of {required_vars}: {err}"
    )

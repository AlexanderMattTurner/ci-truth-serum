"""The reviewer-hold sweep must survive a transient GitHub outage.

`gh pr list --json` asks GraphQL, and a GraphQL `HTTP 503` took the whole weekly
sweep red on 2026-08-17. The listing is the sweep's FIRST call, so an outage there
loses every PR rather than one, and the run reports a fault in a sweep that never
looked at anything.

These tests drive the real script with `gh` stubbed, so the retry is observed
rather than asserted about the source text. The second test is the one that keeps
the first honest: a retry that swallowed a permanent fault would be worse than no
retry at all.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None and not os.environ.get("CI"),
    reason="jq not available (CI runners must have it: skipping there would silently drop this suite)",
)

SCRIPT = REPO_ROOT / ".github" / "scripts" / "sweep-reviewer-holds.sh"


def _run(tmp_path: Path, *, failures: int) -> subprocess.CompletedProcess:
    """Run the sweep with `gh` failing its first FAILURES calls, then listing no PRs.

    An empty listing keeps the test on the retry behaviour: the per-PR delegation
    is the approval script's own suite.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'count_file="{tmp_path}/calls"\n'
        'attempts=$(( $(cat "$count_file" 2>/dev/null || echo 0) + 1 ))\n'
        'printf "%s" "$attempts" >"$count_file"\n'
        f"if ((attempts <= {failures})); then\n"
        "  echo 'HTTP 503: No server is currently available' >&2\n"
        "  exit 1\n"
        "fi\n"
        "printf '[]\\n'\n",
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "GH_REPO": "owner/name",
            "RETRY_BASE_DELAY": "0",
            "RETRY_MAX": "3",
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def test_a_transient_outage_is_retried_and_the_sweep_completes(tmp_path) -> None:
    result = _run(tmp_path, failures=1)
    assert result.returncode == 0, result.stderr
    assert "ci-retry" in result.stderr, (
        "the listing did not go through the retry helper, so the next 503 is "
        "still a red weekly sweep"
    )
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"


def test_a_permanent_fault_still_goes_red(tmp_path) -> None:
    result = _run(tmp_path, failures=99)
    assert result.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "3", (
        "the retry must exhaust its cap rather than give up early or loop"
    )

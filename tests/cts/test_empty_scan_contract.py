"""Every check must be distinguishable from one that scanned nothing.

A check that scanned no file returns 0, which is the exit code of a real pass.
These two tests drive every member of the tier registry to prove no check can
report that silently. Both are cross-module contracts, not unit tests of one
function, so they live here rather than in ``test_linecheck.py`` — that file is
``_linecheck.py``'s mutation oracle and is re-run once per mutant, where 50
subprocess spawns per run would exhaust the shard.
"""

import os
import subprocess
import sys
from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

run_tier = load_hook("run_tier.py", "run_tier")


def _members(*, workflow: bool) -> list[str]:
    return sorted(
        {
            module
            for members in run_tier.TIERS.values()
            for module, kind in members
            if (kind == run_tier.WORKFLOW) is workflow
        }
    )


def test_every_content_check_refuses_a_run_with_no_files() -> None:
    """No content check may report a clean pass over an empty file list.

    Driven from the tier registry, not a pasted list, so a content check added
    later without the guard fails here rather than shipping the false green.
    """
    content = _members(workflow=False)
    assert len(content) > 20, "registry lookup found almost nothing — check the kinds"
    statuses = {
        module: subprocess.run(
            [sys.executable, "-m", f"ci_truth_serum.{module}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        ).returncode
        for module in content
    }
    assert statuses == dict.fromkeys(content, 2)


def test_every_workflow_check_says_so_over_a_tree_with_no_workflows(
    tmp_path: Path,
) -> None:
    """No workflow check may report a silent clean pass over a tree it never scanned.

    Exit 0 is honest here — a repository with no workflow has none to violate —
    so the assertion is on the notice, which is what tells the two cases apart.
    Driven from the tier registry so a workflow check added later is covered.
    """
    workflow_checks = _members(workflow=True)
    assert len(workflow_checks) > 15, "registry lookup found almost nothing"
    silent = []
    for module in workflow_checks:
        done = subprocess.run(
            [sys.executable, "-m", f"ci_truth_serum.{module}"],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
        if "scanned nothing" not in done.stderr:
            silent.append((module, done.returncode, done.stderr[:200]))
    assert silent == []

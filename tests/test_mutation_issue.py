"""Tests for .github/scripts/mutation-issue.py — the daily mutation ledger.

They pin which jobs count as red and the body a red or survivor run writes.
The `gh` calls are not driven here: they are one command each, and a stub would
only restate them.
"""

import importlib.util

import pytest

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "mutation-issue.py"
_spec = importlib.util.spec_from_file_location("mutation_issue", _SRC)
mi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mi)


def test_red_jobs_counts_failed_and_cancelled_only() -> None:
    results = (
        "plan=success\nmutation=failure\naggregate=skipped\n\nmutation-js=cancelled\n"
    )
    assert mi.red_jobs(results) == ["mutation", "mutation-js"]


def test_red_jobs_refuses_an_unreadable_line() -> None:
    with pytest.raises(SystemExit, match="unreadable RESULTS line"):
        mi.red_jobs("mutation failure")


def test_body_carries_the_marker_failed_jobs_and_survivors() -> None:
    body = mi.render_body(
        ["mutation"], "### `check_a`\n\n- `x.py:3` `op`", "RUN", "abc"
    )
    assert body.startswith(mi.MARKER + "\n\n")
    assert "## Failed jobs\n\n- `mutation`" in body
    assert body.endswith("## Surviving mutants\n\n### `check_a`\n\n- `x.py:3` `op`\n")
    assert "`abc`" in body and "RUN" in body


def test_body_omits_the_sections_a_run_has_nothing_for() -> None:
    body = mi.render_body([], "### `check_a`", "RUN", "abc")
    assert "## Failed jobs" not in body
    assert "## Surviving mutants" in body

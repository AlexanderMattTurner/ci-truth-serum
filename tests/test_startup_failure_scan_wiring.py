"""Behavior tests for the startup-failure-scan wiring: the weekly workflow, its
wrapper script, and the routing that carries a finding to a human.

`release-canary` shipped as a console script nothing in this repo invoked, and
it rotted unseen until it started dying on this very repo. `startup-failure-scan`
is the same artifact class, so these tests pin the same properties:

  * the scheduled workflow really runs the wrapper, and the wrapper really
    invokes the declared console script entry point;
  * neither the step nor its job is taken off the failure path, so a finding
    fails the run instead of reporting green; and
  * that workflow's own failures reach a human, judged by the repo's routing
    SSOT (`ci_truth_serum/_cts_failure_routing.py`) over the real workflow tree.

The wrapper's exit-status propagation is driven for real against a stubbed `uv`,
so the behavior is observed and not asserted about the source text.
"""

import subprocess
from pathlib import Path

import pytest
import tomllib
import yaml

from tests._helpers import REPO_ROOT, load_hook

SCRIPT_REL = ".github/scripts/startup-failure-scan.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCAN_WORKFLOW = WORKFLOWS / "startup-failure-scan.yaml"

routing_mod = load_hook("_cts_failure_routing.py", "startup_scan_wiring_routing")
linecheck = load_hook("_cts_linecheck.py", "startup_scan_wiring_linecheck")


def _scan_doc() -> dict:
    return yaml.safe_load(SCAN_WORKFLOW.read_text(encoding="utf-8"))


def _steps_running(doc: dict, needle: str) -> list[tuple[str, dict]]:
    """Every `(job_id, step)` in DOC whose `run:` body invokes NEEDLE."""
    return [
        (job_id, step)
        for job_id, job in (doc.get("jobs") or {}).items()
        for step in job.get("steps") or []
        if isinstance(step, dict) and needle in str(step.get("run", ""))
    ]


# ── the workflow runs the script ────────────────────────────────────────


def test_a_scheduled_workflow_runs_the_wrapper() -> None:
    doc = _scan_doc()
    steps = _steps_running(doc, SCRIPT_REL)
    assert len(steps) == 1, f"expected exactly one scan step, got {steps}"
    # `on:` parses as the YAML boolean True, so read the schedule off both keys.
    triggers = doc.get("on") or doc.get(True)
    assert triggers["schedule"], "the sensor must run on a schedule, not only by hand"


def test_the_job_can_read_the_run_history_it_scans() -> None:
    # `actions: read` is what the runs and jobs listings need. Without it the
    # scan 403s, and a scan that cannot read reports nothing to fix.
    job = _scan_doc()["jobs"]["scan"]
    assert job["permissions"]["actions"] == "read"


def test_a_finding_is_not_swallowed() -> None:
    """No `continue-on-error`, and no `if:` that holds only on success — the two
    ways a wired sensor reports green while finding nothing."""
    doc = _scan_doc()
    job_id, step = _steps_running(doc, SCRIPT_REL)[0]
    job = doc["jobs"][job_id]
    assert not step.get("continue-on-error"), "the scan step must fail the job"
    assert not job.get("continue-on-error"), "the scan job must fail the workflow"
    for gate in (step.get("if", ""), job.get("if", "")):
        assert routing_mod.gate_direction(gate) != routing_mod.BLOCKED, (
            f"gate {gate!r} holds only on success, so the scan would never run"
        )


def test_the_wrapper_invokes_a_declared_entry_point() -> None:
    """The name the wrapper runs is a declared console script, so a rename cannot
    leave the step invoking a command that no longer exists."""
    scripts = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    assert "startup-failure-scan" in scripts, scripts
    assert scripts["startup-failure-scan"].startswith(
        "ci_truth_serum.startup_failure_scan:"
    )
    body = (REPO_ROOT / SCRIPT_REL).read_text(encoding="utf-8")
    assert "startup-failure-scan" in body


def test_the_scan_workflow_failure_reaches_a_human() -> None:
    """A finding turns this workflow red, and that red must reach somebody.

    Judged with the repo's own routing SSOT over the real workflow tree, so a
    notifier list that stopped naming this workflow fails here rather than
    silently dropping the alert.
    """
    matcher = linecheck.notifier_matcher()
    docs = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    ]
    watched = routing_mod.watched_names(
        [doc for doc in docs if isinstance(doc, dict)], matcher
    )
    doc = _scan_doc()
    route = routing_mod.routing(
        doc, SCAN_WORKFLOW.read_text(encoding="utf-8"), matcher, watched
    )
    assert not route.opted_out, (
        "the scan must not opt out of alerting: a finding nobody is told about "
        "is the exact failure this sensor exists to end"
    )
    assert route.self_notify or route.watched, (
        f"no notifier watches {doc['name']!r}, so a finding would land in an "
        "Actions tab nobody opens"
    )


# ── the wrapper, driven for real ────────────────────────────────────────


def _run_wrapper(
    tmp_path: Path, *, scan_exit: int, output: str
) -> subprocess.CompletedProcess:
    """Run the real wrapper with `uv` stubbed to print OUTPUT and exit SCAN_EXIT."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "uv").write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' {output!r}\nexit {scan_exit}\n",
        encoding="utf-8",
    )
    (bindir / "uv").chmod(0o755)
    summary = tmp_path / "summary.md"
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / SCRIPT_REL), "owner/name"],
        capture_output=True,
        text=True,
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        check=False,
    )
    proc.summary = summary.read_text(encoding="utf-8") if summary.exists() else ""
    return proc


def test_a_finding_propagates_the_non_zero_exit(tmp_path) -> None:
    proc = _run_wrapper(tmp_path, scan_exit=1, output="### found it")
    assert proc.returncode == 1
    # The report still reaches the summary. `set -e` on the assignment would
    # have ended the script with the finding trapped in a variable.
    assert "### found it" in proc.summary


def test_a_clean_scan_exits_zero_and_still_publishes(tmp_path) -> None:
    proc = _run_wrapper(tmp_path, scan_exit=0, output="No workflow failed")
    assert proc.returncode == 0
    assert "No workflow failed" in proc.summary


def test_the_wrapper_demands_a_repo_argument() -> None:
    proc = subprocess.run(
        ["bash", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert proc.returncode != 0
    assert "usage" in proc.stderr


@pytest.mark.parametrize("scan_exit", [2, 3])
def test_an_unexpected_exit_status_is_passed_through(tmp_path, scan_exit) -> None:
    # A crash inside the scan is not a finding, and flattening it to 1 would
    # make an API error read as a broken workflow file.
    assert (
        _run_wrapper(tmp_path, scan_exit=scan_exit, output="x").returncode == scan_exit
    )

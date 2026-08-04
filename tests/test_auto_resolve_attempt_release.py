"""The auto-resolve job must give back an attempt it never spent.

The resolve job marks a pull request head as attempted before it installs its
tools, so a crash or a timeout part-way through a paid resolution cannot make
the next sweep repeat that work. The cost of that ordering is a run that fails
*before* any work starts: it marks the head, does nothing, and the mark then
suppresses every later scan of that pull request for a full TTL.

That is not hypothetical. A missing pins file failed the mergiraf install, and
every open conflicted pull request in this repository was latched with no merge
attempted and nothing said on the pull request itself.

These read the workflow with a real YAML parser and assert the release path
exists and is guarded so it can never hand back a head a paid pass worked on.
"""

import yaml

from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "auto-resolve-conflicts.yaml"
RELEASE_SCRIPT = "auto-resolve/release-attempt.sh"


def _resolve_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    resolve = next(
        job
        for job in jobs.values()
        if isinstance(job, dict)
        and any(
            RELEASE_SCRIPT in str(step.get("run", "")) for step in job.get("steps", [])
        )
    )
    return resolve["steps"]


def _release_steps() -> list[dict]:
    return [s for s in _resolve_steps() if RELEASE_SCRIPT in str(s.get("run", ""))]


def test_a_run_that_failed_before_any_work_releases_its_attempt() -> None:
    """One release step is reachable on the failure path.

    Without it a bootstrap failure — a bad checkout, a broken tool install —
    burns the head's attempt for a full TTL while resolving nothing.
    """
    on_failure = [s for s in _release_steps() if "failure()" in str(s.get("if", ""))]
    assert len(on_failure) == 1, (
        "expected exactly one failure-path release step in the resolve job"
    )


def test_the_failure_release_cannot_hand_back_a_head_a_paid_pass_worked_on() -> None:
    """The failure-path release is gated on the pre-pass never having run.

    `prepare` is the first step that touches the tree, and the model call sits
    behind its outputs. Gating on anything weaker would let a failure *after* a
    resolution began release the head, and the next sweep would pay to redo it.
    """
    on_failure = [s for s in _release_steps() if "failure()" in str(s.get("if", ""))]
    assert on_failure, "no failure-path release step to gate"
    condition = str(on_failure[0]["if"])
    assert "steps.prepare.outcome == ''" in condition, (
        f"failure-path release is not gated on prepare never running: {condition}"
    )


def test_the_no_op_release_is_not_on_the_failure_path() -> None:
    """The two release paths stay distinct.

    The no-op release runs on a SUCCESSFUL run that merged cleanly. Folding the
    two conditions together would release a head on any failure at all.
    """
    no_op = [s for s in _release_steps() if "no_op_head" in str(s.get("if", ""))]
    assert len(no_op) == 1
    assert "failure()" not in str(no_op[0]["if"])

"""The auto-resolve job must give back an attempt it never spent.

The resolve job marks a pull request head as attempted before it installs its
tools, so a crash or a timeout part-way through a paid resolution cannot make
the next sweep repeat that work. The cost of that ordering is a run that fails
*before* any work starts: it marks the head, does nothing, and the mark then
suppresses every later scan of that pull request for a full TTL.

That is not hypothetical, and it happened twice in different shapes. A missing
pins file failed the mergiraf install, and every open conflicted pull request in
this repository was latched with no merge attempted. Later the credential guard
refused an empty ladder AFTER the pre-pass had run, so the release was skipped,
and pull request #147 stayed stranded even once the credentials were fixed —
only a new head clears a mark.

So the gate is on SPEND, not on how far the run got: `execution_file` is written
as soon as the CLI produces a log, so an empty value means no model was called.

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
    """The failure-path release is gated on the run having spent nothing.

    Both arms are needed and neither is sufficient. Gating only on `prepare`
    never running keeps the mark on a credential refusal that spent nothing —
    the #147 strand. Gating only on the empty execution log would release a head
    whose failure came before the model step ever ran, where `execution_file` is
    empty for a different reason. Dropping the `execution_file` test entirely
    would release a head whose ladder really did call the model and get errors
    back, and the next sweep would pay to redo it.
    """
    on_failure = [s for s in _release_steps() if "failure()" in str(s.get("if", ""))]
    assert on_failure, "no failure-path release step to gate"
    condition = " ".join(str(on_failure[0]["if"]).split())
    assert "steps.prepare.outcome == ''" in condition, (
        f"failure-path release does not cover a run that never touched the tree: {condition}"
    )
    assert "steps.resolve_llm.outputs.execution_file == ''" in condition, (
        f"failure-path release does not cover a run that called no model: {condition}"
    )
    assert "steps.resolve_llm.outcome == 'failure'" in condition, (
        "the empty-execution-log arm must also require the model step to have "
        f"FAILED, or a skipped model step releases the head: {condition}"
    )


def test_the_release_reads_the_step_that_writes_the_execution_log() -> None:
    """`execution_file` must name the step that actually produces it.

    The condition is a string GitHub resolves at run time: a wrong step id is
    always the empty string, which silently releases every failing head. This
    pins the id to the step whose script writes that output.
    """
    steps = _resolve_steps()
    writer = next(
        s for s in steps if "claude-conflict-resolve.sh" in str(s.get("run", ""))
    )
    assert writer["id"] == "resolve_llm", (
        f"the execution-log writer is step id {writer['id']!r}, but the release "
        "condition reads steps.resolve_llm.outputs.execution_file"
    )


def test_the_no_op_release_is_not_on_the_failure_path() -> None:
    """The two release paths stay distinct.

    The no-op release runs on a SUCCESSFUL run that merged cleanly. Folding the
    two conditions together would release a head on any failure at all.
    """
    no_op = [s for s in _release_steps() if "no_op_head" in str(s.get("if", ""))]
    assert len(no_op) == 1
    assert "failure()" not in str(no_op[0]["if"])


def test_the_release_is_declared_after_every_step_it_reads() -> None:
    """A step sees only the steps declared ABOVE it.

    GitHub resolves `steps.<later-id>` in an earlier step's `if:` to the empty
    string rather than erroring, so a release placed before the model step reads
    an empty outcome, its spend arm never fires, and the whole fix is a silent
    no-op that still passes every other assertion here.
    """
    steps = _resolve_steps()
    ids = [s.get("id") for s in steps]
    release_index = next(
        i
        for i, s in enumerate(steps)
        if RELEASE_SCRIPT in str(s.get("run", "")) and "failure()" in str(s.get("if", ""))
    )
    condition = str(steps[release_index]["if"])
    read_ids = {sid for sid in ids if sid and f"steps.{sid}." in condition}
    assert read_ids, f"the release condition names no step id: {condition}"
    for sid in read_ids:
        assert ids.index(sid) < release_index, (
            f"the release reads steps.{sid} but is declared before it, so that "
            "value is always the empty string"
        )

"""The sync workflow must not run the code the sync is proposing.

`.github/scripts` is one of the SYNC_PATHS, so the `template-sync` branch carries
the TEMPLATE's copy of every script. Once the resolve step checks that branch out,
`bash .github/scripts/x.sh` reads the incoming, unreviewed file. Run 32714066016
died exactly there: the template's newer resolver demands a `RESOLVER_DIR` this
workflow never sets, so every scheduled sync that hit a conflict failed the same
way, and the failure was in a script no reviewer had approved.

The fix adds a base-ref worktree under `runner.temp` before the checkout, and
runs the resolver, the push and the auto-merge from there. It is the shape and
the names `auto-resolve-conflicts.yaml` already uses: a `resolver` step whose
`dir` output is the scripts directory and whose `base` output is the worktree.
These tests pin that: after the branch switch, no step may name a workspace
script path.

The path test is a text property (`does this body name that path`), not a shell
structure question, so it is a plain substring scan by design.
"""

import yaml

from tests._helpers import REPO_ROOT

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "template-sync.yaml"
SWITCH = "git checkout -B template-sync"
WORKSPACE_SCRIPTS = ".github/scripts/"
STAGED = "${{ steps.resolver.outputs.dir }}"
STAGED_REF = "${RESOLVER_DIR}"
WORKTREE = "${{ steps.resolver.outputs.base }}"


def _sync_steps() -> list[dict]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return [step for step in doc["jobs"]["sync"]["steps"] if isinstance(step, dict)]


def _switch_index(steps: list[dict]) -> int:
    for index, step in enumerate(steps):
        if SWITCH in str(step.get("run", "")):
            return index
    raise AssertionError(
        f"no step switches to the sync branch ({SWITCH!r}); this suite guards the "
        "steps that follow that switch, so it now guards nothing"
    )


def test_the_staging_step_takes_a_worktree_of_the_base_ref() -> None:
    """The worktree must be added while the workspace is still on the base ref.

    A full worktree, not a copy of one directory: an enumerated subset leaves out
    whatever it did not anticipate, which a script finds only after the paid work.
    """
    steps = _sync_steps()
    staging = [
        index
        for index, step in enumerate(steps)
        if step.get("id") == "resolver" and "git worktree add" in str(step["run"])
    ]
    assert staging, "no step stages a base-ref worktree"
    assert staging[0] < _switch_index(steps), (
        "the staging step runs after the sync branch is checked out, so it stages "
        "the incoming scripts rather than the reviewed ones"
    )


def test_the_steps_after_the_switch_run_the_staged_copy() -> None:
    steps = _sync_steps()
    after = steps[_switch_index(steps) :]
    running = [step for step in after if STAGED_REF in str(step.get("run", ""))]
    assert running, (
        "no step after the branch switch runs the staged copy — the assertion "
        "below would then hold for an empty set"
    )
    for step in after:
        assert WORKSPACE_SCRIPTS not in str(step.get("run", "")), (
            f"step {step.get('name')!r} runs a script from the workspace after the "
            f"sync branch is checked out, so it executes the template's incoming "
            f"copy. Run it from {STAGED} instead."
        )


def test_every_staged_invocation_has_the_directory_in_its_env() -> None:
    """A `${RESOLVER_DIR}` that no `env:` sets expands to an empty path."""
    for step in _sync_steps():
        if STAGED_REF not in str(step.get("run", "")):
            continue
        assert (step.get("env") or {}).get("RESOLVER_DIR") == STAGED, (
            f"step {step.get('name')!r} reads RESOLVER_DIR without binding it to "
            "the staging step's output"
        )


def test_the_resolve_step_binds_the_worktree_too() -> None:
    """`auto-resolve/self-review.sh` reads its prompts and its CLI from
    BASE_WORKTREE, and refuses to run without it."""
    resolve = [step for step in _sync_steps() if step.get("id") == "resolve"]
    assert len(resolve) == 1, "no resolve step to check"
    assert (resolve[0].get("env") or {}).get("BASE_WORKTREE") == WORKTREE

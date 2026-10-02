#!/usr/bin/env python3
"""
Forbid a concurrency group whose eviction kills a CURRENT sibling's work rather
than a superseded run's. Two shapes do that, and the lint reports both.

SHAPE 1 — a job that SKIPS still claims the slot.

GitHub claims a job's concurrency slot when it CREATES the job, BEFORE it reads
the job's `if:`. A run that skips the job therefore still takes the slot, and
GitHub evicts whatever member was already queued there. When the group holds one
value across a trigger the job serves AND a trigger it skips, the evicted member
is a live run's real work on a commit nobody superseded.

`cancel-in-progress` is irrelevant, which is what makes this invisible: the
victim's jobs report `cancelled` with every step `completed` and `success`, so
no log names a failure. One measured run finished all eleven steps and was then
cancelled by a `labeled` run whose jobs all skipped.

Neither of shape 2's conditions is needed here. The workflow need not declare a
pull_request activity type outside the defaults, because the two triggers can be
two of the defaults; and it need not back a required check, because the cost is
the thrown-away work rather than a reddened gate. Fix it by varying the group by
the same condition the `if:` reads — end the group `-shared` on the triggers the
job serves and `-inert-${{ github.run_id }}` on the triggers it skips. Write the
truthy half as a non-empty string: `cond && '' || …` is always inert, because
GitHub reads the empty string as false. Opt out per job with
"# inert-group-ok: <reason>".

Two more arms of shape 1 read inside one trigger, and both use the same opt-out:

- A FIXED group behind an `if:` that reads the delivery. On a `labeled` run,
  `if: github.event.label.name == 'go'` can hold or fail. A push can read
  `github.ref`, and a `workflow_dispatch` run can read `inputs.deploy`. So two
  runs of one trigger can split into one that serves and one that skips, and
  they share the one slot.
- A run-id escape that picks the run id on EVERY trigger the job serves. The
  condition is inverted, or the shared operand is `''`. The group then never
  holds two runs, so the job is never serialized. A workflow-level group gets
  the same reading over every trigger the workflow declares. Its opt-out sits
  in the top-level `concurrency:` block.

The sibling `check_collapsing_job_group` owns the neighbouring question: a group
whose per-ref key is EMPTY on some event, so every run of that event shares one
slot. There the runs sharing the slot are all runs of one event, so the job's
`if:` decides whether any work is lost.

SHAPE 2 — a ref-keyed group on a required-check workflow that can fire more than
one run per commit.

`check_static_concurrency` treats `github.ref` / `github.head_ref` keys as safe
on the assumption that a ref-keyed run is only ever superseded by a *newer run
of the same ref*, whose own reporter re-posts the check. That assumption breaks
when `on.pull_request.types` includes an activity type outside the default
{opened, synchronize, reopened}: types like `labeled` or `closed` fire a new
run WITHOUT a new head SHA, so several runs queue on ONE commit (a Dependabot
PR is born with labels — `opened` + one `labeled` per label land near-
simultaneously). GitHub keeps at most one running + one pending run per group;
the third same-SHA run cancels a sibling that is *current*, not superseded. Its
`always()` reporter resolves `cancelled` → the required check goes RED on the
live head with no real failure.

`cancel-in-progress` cannot save a ref-keyed group here: `true` cancels the
in-progress run (current SHA → red), `false` cancels the pending one (also
current SHA → red). The safe fixes are to drop the group or key it on
`github.run_id` (a group of one cannot cancel a sibling).

A workflow "backs a required check" by EITHER of two routes. The heuristic
route is a decide gate plus one of the shapes that fail closed on a broken gate:
an `always()` reporter, or a fail-closed twin (`if: always() &&
needs.<gate>.result != 'success'`). A workflow can also declare a required check
with nothing but a `# required-check: true` marker on one job — the mandatory
SSOT marker check_required_reporter enforces and sync_required_checks reads —
and that shape has no decide gate to find.

Which jobs each route judges follows from what reads their results. An always()
reporter is a funnel every job feeds, so that route judges every job. A twin
reads only its gates' results, so that route judges the gates, the twins and the
MARKED jobs; an unrelated job's cancellation reddens no check the twin posts.
The marker route judges the workflow-level block plus the marked jobs alone.

This lint is deliberately scoped to per-ref/per-PR groups (the key polarity
check_static_concurrency calls safe). A STATIC group shares the failure mode but
stays check_static_concurrency's territory at the workflow level, and a STATIC
job-level group is left unflagged ON PURPOSE: moving a static workflow-level
group down onto the expensive job is the remedy check_static_concurrency and
check_cancellable_required_check both prescribe, so a lint that flagged it would
fire on every application of the blessed fix.

Opt out with "# pending-cancel-ok" for a deliberately-serialized workflow that
is genuinely never a required check. That token is file-wide and answers shape 2
only; shape 1 takes the per-job "# inert-group-ok: <reason>" instead.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ALWAYS_REPORTER_SHAPE,
    TWIN_SHAPE,
    _job_blocks,
    _marked_jobs,
    annotated,
    concurrency_line,
    declared_triggers,
    decide_gate_names,
    group_has_run_id_escape,
    group_is_event_constant,
    group_is_per_run_on,
    group_of,
    group_separates_triggers,
    job_concurrency_line,
    job_delivery_split_triggers,
    job_skipped_triggers,
    opted_out,
    required_check_shape,
    valid_twin_names,
    workflow_files,
    yaml_comment_view,
)
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "pending-cancel-ok"
INERT_OPT_OUT = "inert-group-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# The pull_request activity types that only fire alongside a new head SHA. Any
# type OUTSIDE this set (labeled, closed, ready_for_review, …) fires a fresh run
# on the SAME commit — the storm that makes a ref-keyed group cancel a current-
# SHA sibling.
DEFAULT_PR_TYPES = frozenset({"opened", "synchronize", "reopened"})

# Group-expression substrings that key a group per-ref / per-PR — one shared slot
# for every run of the PR, including the same-SHA siblings a type storm queues.
# `github.ref` also covers `github.ref_name`; `pull_request.number` covers
# `github.event.pull_request.number`. Best-effort substring match of the group
# expression, not a full ${{ }} parse — same policy as check_static_concurrency.
PER_REF_KEYS = (
    "github.ref",
    "github.head_ref",
    "pull_request.number",
    "github.event.number",
)

# A group also keyed per-run is a group of one: it can never hold two runs, so
# it can never pending-cancel a sibling. (Not run_attempt — concurrent runs
# share attempt 1.)
PER_RUN_KEYS = ("github.run_id", "github.run_number")


def _storm_types(doc: dict) -> set[str]:
    """The declared pull_request(_target) activity types that fire a run WITHOUT
    a new head SHA — empty when the workflow sticks to the default types (or the
    `pull_request:` / `pull_request: ~` shorthand, which means the defaults)."""
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return set()  # `on: [push, pull_request]` list/scalar form → default types
    extra: set[str] = set()
    for trigger in ("pull_request", "pull_request_target"):
        cfg = triggers.get(trigger)
        if not isinstance(cfg, dict):
            continue  # `pull_request:` / `~` / `true` shorthand → default types
        types = cfg.get("types")
        if isinstance(types, str):
            types = [types]  # GitHub normalizes a scalar filter to a one-item list
        if not isinstance(types, list):
            continue
        extra |= {str(t) for t in types} - DEFAULT_PR_TYPES
    return extra


def _ref_keyed(group: object) -> bool:
    """True when a concurrency group expression is keyed per-ref/per-PR and NOT
    also per-run (github.run_id / run_number make it a group of one — safe)."""
    if group is None:
        return False
    text = str(group)
    if any(key in text for key in PER_RUN_KEYS):
        return False
    return any(key in text for key in PER_REF_KEYS)


def _message(storm: set[str], basis: str) -> str:
    types = ", ".join(sorted(storm))
    return (
        "concurrency.group is keyed per-ref/per-PR on a workflow that "
        f"backs a required check ({basis}) AND declares "
        f"pull_request types beyond opened/synchronize/reopened ({types}). Those "
        "types fire extra runs on the SAME head SHA; GitHub holds at most one "
        "running + one pending run per group, so a same-SHA sibling gets "
        "cancelled — its always() reporter resolves 'cancelled' and the required "
        "check goes red on the current commit with no real failure "
        "(cancel-in-progress true or false only picks WHICH current-SHA run "
        "dies). Drop the group, or key it on github.run_id (a group of one "
        f"cannot cancel a sibling), or add '# {OPT_OUT}' if this workflow is "
        "never a required check."
    )


_INERT_MESSAGE = (
    "job '{name}' skips on a {skipped} run and runs on a {served} run, and its "
    "concurrency group '{group}' holds the same value on both. GitHub claims a "
    "job's group slot when it CREATES the job, BEFORE it reads the `if:`. So the "
    "run that skips this job still takes the slot, and GitHub evicts the member "
    "already queued there — a live run's real work, on a commit nobody "
    "superseded. cancel-in-progress does not change this; it only picks which "
    "member dies. The victim is hard to recognise, because its jobs report "
    "'cancelled' with every step completed and successful, so no log names a "
    "failure. Vary the group by the same condition the `if:` reads — end it "
    "'-shared' on the triggers the job serves and "
    "'-inert-${{{{ github.run_id }}}}' on the triggers it skips — or add "
    "'# " + INERT_OPT_OUT + ": <reason>'."
)


_DELIVERY_MESSAGE = (
    "job '{name}' holds the fixed concurrency group '{group}', and its `if:` "
    "reads the event payload, the ref or an input. So on a {trigger} run the "
    "job can run or skip, by what that one delivery carries. GitHub claims a job's group slot when it "
    "CREATES the job, BEFORE it reads the `if:`. A run that skips this job "
    "therefore evicts the run queued in that slot to do the work. Read the same "
    "condition in the group: end it '-shared' when the `if:` holds and "
    "'-inert-${{{{ github.run_id }}}}' when it does not. Or add "
    "'# " + INERT_OPT_OUT + ": <reason>'."
)

_MISFIRE_MESSAGE = (
    "{subject} chooses its concurrency group between a shared value and the "
    "run id, but it picks the run id on every trigger {scope} runs on. So the "
    "group '{group}' never holds two runs, and {scope} is never serialized. "
    "Either the condition is inverted, or the shared operand is ''. GitHub "
    "reads '' as false, so `cond && '' || <run id>` picks the run id even when "
    "cond holds. Make the condition true where {scope} runs, and give the "
    "shared operand a non-empty value such as 'shared'. Or add "
    "'# " + INERT_OPT_OUT + ": <reason>'."
)


def _shape_one_message(name: str, group: str, if_value: object, triggers) -> str | None:
    """The shape-1 finding for one job, or None.

    Three arms, in order. A trigger the job skips shares a group value with a
    trigger it serves. A fixed group sits behind an `if:` that the payload
    decides. A run-id escape picks the run id on every trigger the job serves.
    Each arm answers only from definite readings, so it under-reports.
    """
    skipped = job_skipped_triggers(if_value, triggers)
    served = [trigger for trigger in triggers if trigger not in skipped]
    if not served:
        return None  # the job runs on no trigger
    pair = next(
        (
            (skip, serve)
            for skip in skipped
            for serve in served
            if not group_separates_triggers(group, skip, serve)
        ),
        None,
    )
    if pair is not None:
        return _INERT_MESSAGE.format(
            name=name,
            skipped=_trigger_name(pair[0]),
            served=_trigger_name(pair[1]),
            group=group,
        )
    if group_is_event_constant(group):
        split = job_delivery_split_triggers(if_value, served)
        if split:
            return _DELIVERY_MESSAGE.format(
                name=name, group=group, trigger=_trigger_name(split[0])
            )
    if group_has_run_id_escape(group) and all(
        group_is_per_run_on(group, trigger) for trigger in served
    ):
        return _MISFIRE_MESSAGE.format(
            subject=f"job '{name}'", scope="the job", group=group
        )
    return None


def _inert_slot_violations(
    doc: dict, jobs: dict, blocks: dict, comment_lines: list[str]
) -> list[tuple[int | None, str]]:
    """Every job whose group lets a skipping run take a working run's slot, or
    whose run-id escape never shares a slot at all."""
    triggers = declared_triggers(doc)
    violations: list[tuple[int | None, str]] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        group = group_of(cfg.get("concurrency"))
        if not isinstance(group, str) or not group:
            continue
        message = _shape_one_message(str(name), group, cfg.get("if"), triggers)
        if message is None:
            continue
        block = blocks.get(str(name))
        if block and _block_opted_out(comment_lines, block):
            continue
        violations.append(
            (job_concurrency_line(block, block[0] if block else 1), message)
        )
    return violations


def _workflow_misfire_violations(
    text: str, doc: dict, comment_lines: list[str]
) -> list[tuple[int | None, str]]:
    """The finding for a workflow-level group whose run-id escape picks the run
    id on every trigger the workflow declares, or an empty list.

    The group claims its slot for every run, so no job `if:` narrows the
    triggers it serves. A declared opt-out in the `concurrency:` block clears it.
    """
    group = group_of(doc.get("concurrency"))
    if not isinstance(group, str) or not group_has_run_id_escape(group):
        return []
    triggers = declared_triggers(doc)
    if not triggers or not all(group_is_per_run_on(group, t) for t in triggers):
        return []
    line = concurrency_line(text)
    window = _top_level_block(text.splitlines(), line)
    if any(annotated(comment_lines[num], INERT_OPT_OUT) for num in window):
        return []
    message = _MISFIRE_MESSAGE.format(
        subject="the workflow", scope="the workflow", group=group
    )
    return [(line, message)]


def _top_level_block(lines: list[str], start: int) -> range:
    """The 0-based line indexes of the top-level key on 1-based line START and
    its body: every following line that is blank or indented."""
    end = start
    while end < len(lines) and (not lines[end].strip() or lines[end][0] in " \t"):
        end += 1
    return range(start - 1, end)


def _trigger_name(trigger) -> str:
    """A trigger written the way a workflow author reads it."""
    return (
        f"'{trigger.event}' / '{trigger.action}'"
        if trigger.action
        else f"'{trigger.event}'"
    )


def _block_opted_out(comment_lines: list[str], block: tuple[int, str]) -> bool:
    """True if a reason-bearing `# inert-group-ok:` sits in a real YAML comment
    inside the job's source block. COMMENT_LINES is the file with every
    non-comment character blanked, so a quoted scalar cannot silence the lint."""
    start = block[0] - 1
    window = comment_lines[start : start + len(block[1].splitlines())]
    return any(annotated(line, INERT_OPT_OUT) for line in window)


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every ref-keyed concurrency group — workflow-
    level OR job-level — on a required-check workflow whose pull_request types
    can queue multiple runs on one head SHA.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "concurrency-group safety against same-SHA pending-cancellation — "
                "fix the syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict) or opted_out(text, OPT_OUT):
        return []

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    blocks = _job_blocks(text)
    comment_lines = yaml_comment_view(text)
    violations = _inert_slot_violations(doc, jobs, blocks, comment_lines)
    violations += _workflow_misfire_violations(text, doc, comment_lines)

    storm = _storm_types(doc)
    if not storm:
        return (
            violations  # one run per SHA — a ref-keyed group only supersedes older SHAs
        )

    heuristic = required_check_shape(jobs, doc)
    marked = _marked_jobs(blocks, jobs)
    if not (heuristic or marked):
        return (
            violations  # not a required-check shape — a reddened cancel self-describes
        )
    basis = heuristic or "the '# required-check: true' marker"

    if _ref_keyed(group_of(doc.get("concurrency"))):
        violations.append(
            (concurrency_line(text), f"workflow-level {_message(storm, basis)}")
        )

    # An always() reporter is a funnel every job feeds: it reads each job's
    # result, so any job's cancellation can strand the required check. A TWIN
    # reads only its gates' results, so an unrelated job's cancel reddens nothing
    # it reports — under that shape the judged set is the gates, the twins, and
    # whatever carries the marker. A marker-only workflow has no funnel at all.
    if heuristic == ALWAYS_REPORTER_SHAPE:
        judged = jobs
    else:
        names = set(marked)
        if heuristic == TWIN_SHAPE:
            names |= decide_gate_names(jobs) | set(valid_twin_names(jobs, doc))
        judged = {name: jobs[name] for name in names if name in jobs}
    for name, cfg in judged.items():
        if not isinstance(cfg, dict):
            continue
        if _ref_keyed(group_of(cfg.get("concurrency"))):
            block = blocks.get(str(name))
            fallback = block[0] if block else concurrency_line(text)
            line = job_concurrency_line(block, fallback)
            violations.append((line, f"job '{name}': {_message(storm, basis)}"))
    return violations


def main() -> int:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR, ACTIONS_DIR):
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

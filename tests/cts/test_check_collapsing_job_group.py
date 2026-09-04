"""Tests for ci_truth_serum/check_collapsing_job_group.py — the (opinionated)
lint that flags a JOB-level concurrency group whose per-ref key is empty or
fixed on an event the job's own `if:` still admits, so every run of that event
shares one slot and cancels work on an unrelated commit."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

cjg = load_hook("check_collapsing_job_group.py", "check_collapsing_job_group")


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _workflow(events: str, job_body: str) -> str:
    return f"name: x\non:\n{events}jobs:\n  sweep:\n{job_body}"


PR_AND_SCHEDULE = "  pull_request:\n  schedule:\n    - cron: '0 * * * *'\n"

# The defect: a group keyed on the PR number, on a job that also runs on cron.
COLLAPSING_JOB = (
    "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n"
    "    runs-on: ubuntu-latest\n"
    "    concurrency:\n"
    "      group: sweep-${{ github.event.pull_request.number }}\n"
    "      cancel-in-progress: true\n"
    "    steps: []\n"
)


# ── the defect ───────────────────────────────────────────────────────────────


def test_pr_number_group_on_a_scheduled_job_is_an_error(tmp_path):
    """`github.event.pull_request.number` is empty on a cron run, so every
    scheduled run of this job shares the one slot `sweep-`."""
    found = cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, COLLAPSING_JOB)))
    assert len(found) == 1
    line, message = found[0]
    assert line == 7  # the `sweep:` key line
    assert "'schedule'" in message
    assert "github.run_id" in message


def test_a_job_with_no_if_admits_every_declared_event(tmp_path):
    """No `if:` is the widest admission there is, so the collapse still bites."""
    body = COLLAPSING_JOB.split("\n", 1)[1]  # drop the `if:` line
    found = cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))
    assert len(found) == 1


def test_an_opaque_gate_reads_as_admitting_every_event(tmp_path):
    """A job gated through `needs.<gate>.outputs` restricts nothing this lint can
    read, so the one-sided reading reports it — that is what the annotation is
    for."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: needs.decide.outputs.run == 'true'\n",
    )
    assert len(cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))) == 1


def test_an_unreadable_disjunct_unbounds_the_union(tmp_path):
    """`||` must answer "cannot tell" when ANY arm is unreadable: the opaque arm
    can be true on a cron run, so the readable arm must not be allowed to bound
    the union and hide the collapse."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event_name == 'pull_request' || needs.decide.outputs.run == 'true'\n",
    )
    assert len(cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))) == 1


def test_an_unreadable_conjunct_leaves_the_readable_arm_bounding(tmp_path):
    """`&&` must KEEP the readable arms: an opaque conjunct only fails to narrow,
    so `pull_request` still bounds the job and the cron collapse stays inert."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: needs.decide.outputs.run == 'true' && github.event_name == 'pull_request'\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_github_ref_collapses_on_pull_request_target(tmp_path):
    """`pull_request_target` runs in the base repo, so `github.ref` is the base
    branch and every open PR's run shares it."""
    events = "  pull_request_target:\n"
    body = (
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: retarget-${{ github.ref }}\n"
        "      cancel-in-progress: true\n"
        "    steps: []\n"
    )
    assert len(cjg.check_file(_write(tmp_path, _workflow(events, body)))) == 1


def test_every_collapsing_event_is_named(tmp_path):
    """Two declared events can both flatten one group; the report names both so
    a fix is not written against the first alone."""
    events = "  pull_request:\n  schedule:\n    - cron: '0 * * * *'\n  workflow_run:\n    workflows: [x]\n"
    body = (
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: sweep-${{ github.head_ref }}\n"
        "    steps: []\n"
    )
    (_line, message) = cjg.check_file(_write(tmp_path, _workflow(events, body)))[0]
    assert "'schedule'" in message and "'workflow_run'" in message


# ── what must NOT fire ───────────────────────────────────────────────────────


def test_an_if_that_excludes_the_collapsing_event_is_clean(tmp_path):
    """GitHub claims the slot before it reads the `if:`, so the skipping run does
    take it — but every run sharing this collapsed slot is a run of the same
    event, so all of them skip and the eviction costs no work."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event_name == 'pull_request'\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_a_payload_demand_excludes_the_collapsing_event(tmp_path):
    """`github.event.action == 'closed'` is false on a cron run, because that
    payload carries no action — the `if:` excludes the event without naming it."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event.action == 'closed'\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_a_null_tolerant_comparison_does_not_count_as_an_exclusion(tmp_path):
    """GitHub reads an absent path as null, and `null != 'Bot'` is TRUE, so this
    condition does not keep the job off a cron run. Reading it as an exclusion
    would fail the lint open on the exact shape it exists to catch."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event.pull_request.user.type != 'Bot'\n",
    )
    assert len(cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))) == 1


def test_a_parenthesised_conjunct_inside_a_disjunction_still_excludes(tmp_path):
    """`(A && B) || C` is the shape a real event fan-out takes. Reading the
    parenthesised arm is what keeps the union bounded, so the cron event stays
    excluded."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: >-\n"
        "      (github.event_name == 'pull_request' &&\n"
        '       contains(fromJSON(\'["opened", "synchronize"]\'), github.event.action)) ||\n'
        "      github.event_name == 'merge_group'\n",
    )
    events = PR_AND_SCHEDULE + "  merge_group:\n"
    found = cjg.check_file(_write(tmp_path, _workflow(events, body)))
    # merge_group survives (the group is empty there too), 'schedule' does not —
    # and only reading the parenthesised arm can drop it.
    assert len(found) == 1
    assert "'merge_group'" in found[0][1] and "'schedule'" not in found[0][1]


def test_two_parenthesised_arms_are_not_read_as_one_wrapping_pair(tmp_path):
    """`(A) || (B)` opens and closes with a parenthesis without being wrapped by
    one. Stripping those would hand the reader `A) || (B` and lose the split, so
    the cron arm would vanish and the defect would go unreported."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: (github.event_name == 'schedule') || (github.event_name == 'pull_request')\n",
    )
    assert len(cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))) == 1


def test_a_group_with_no_per_ref_key_is_out_of_scope(tmp_path):
    """Moving a static workflow-level group down onto the expensive job is the
    remedy check_static_concurrency prescribes, so flagging it would fire on
    every application of the blessed fix."""
    body = (
        "    runs-on: ubuntu-latest\n"
        "    concurrency:\n"
        "      group: one-publisher-at-a-time\n"
        "      cancel-in-progress: false\n"
        "    steps: []\n"
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_a_group_that_varies_on_every_declared_event_is_clean(tmp_path):
    """`github.run_id` holds a fresh value on every event, so the group can
    never be shared."""
    body = COLLAPSING_JOB.replace(
        "      group: sweep-${{ github.event.pull_request.number }}\n",
        "      group: sweep-${{ github.event.pull_request.number || github.run_id }}\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_a_workflow_level_group_is_not_this_lint(tmp_path):
    """check_static_concurrency owns the workflow-level block."""
    body = (
        "name: x\non:\n" + PR_AND_SCHEDULE + "concurrency:\n"
        "  group: wf-${{ github.event.pull_request.number }}\n"
        "jobs:\n  sweep:\n    runs-on: ubuntu-latest\n    steps: []\n"
    )
    assert cjg.check_file(_write(tmp_path, body)) == []


def test_a_merge_group_action_gate_does_not_exclude_the_merge_queue(tmp_path):
    """A merge-queue payload carries `action: checks_requested`, so that gate
    admits `merge_group` — and the merge queue is the one place a run has no PR
    number at all. Reading merge_group as actionless would drop the report."""
    events = "  pull_request:\n  merge_group:\n"
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event.action == 'checks_requested'\n",
    )
    found = cjg.check_file(_write(tmp_path, _workflow(events, body)))
    assert len(found) == 1
    assert "'merge_group'" in found[0][1]


def test_an_event_literal_is_matched_without_regard_to_case(tmp_path):
    """GitHub compares two strings without regard to case, so `== 'SCHEDULE'`
    admits a cron run. Matching the literal exactly would miss the collapse."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event_name == 'SCHEDULE'\n",
    )
    found = cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body)))
    assert len(found) == 1
    assert "'schedule'" in found[0][1]


def test_a_negated_event_literal_is_matched_without_regard_to_case(tmp_path):
    """The same rule on the other polarity: `!= 'SCHEDULE'` really does keep the
    job off a cron run, so nothing is reported."""
    body = COLLAPSING_JOB.replace(
        "    if: github.event_name == 'schedule' || github.event_name == 'pull_request'\n",
        "    if: github.event_name != 'SCHEDULE'\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


def test_a_key_name_inside_a_quoted_literal_is_not_a_per_ref_key(tmp_path):
    """`${{ 'github.ref' }}` is the fixed text `github.ref`, not the ref. The
    group is fully static, which this lint exempts."""
    body = COLLAPSING_JOB.replace(
        "      group: sweep-${{ github.event.pull_request.number }}\n",
        "      group: sweep-${{ 'github.ref' }}\n",
    )
    assert cjg.check_file(_write(tmp_path, _workflow(PR_AND_SCHEDULE, body))) == []


# ── the annotation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["key", "body"])
def test_a_reasoned_annotation_suppresses(tmp_path, where):
    """The annotation is read from the job's key line and from its body alike."""
    reason = "# collapsing-group-ok: two cron runs read one base on purpose\n"
    if where == "key":
        job = "  sweep: " + reason + COLLAPSING_JOB
    else:
        job = "  sweep:\n    " + reason + COLLAPSING_JOB
    body = f"name: x\non:\n{PR_AND_SCHEDULE}jobs:\n{job}"
    assert cjg.check_file(_write(tmp_path, body)) == []


def test_an_annotation_inside_a_quoted_scalar_does_not_suppress(tmp_path):
    """YAML reads `#` inside a quoted scalar as data, so this is a job name that
    happens to mention the token. Honouring it would let any job silence the
    lint through a string value nobody reads as a directive."""
    job = (
        '  sweep:\n    name: "# collapsing-group-ok: an example in the docs"\n'
        + COLLAPSING_JOB
    )
    body = f"name: x\non:\n{PR_AND_SCHEDULE}jobs:\n{job}"
    assert len(cjg.check_file(_write(tmp_path, body))) == 1


def test_a_bare_annotation_does_not_suppress(tmp_path):
    """The reason is required — a bare token is a silencer, not a decision."""
    job = "  sweep:\n    # collapsing-group-ok\n" + COLLAPSING_JOB
    body = f"name: x\non:\n{PR_AND_SCHEDULE}jobs:\n{job}"
    assert len(cjg.check_file(_write(tmp_path, body))) == 1


def test_an_annotation_in_a_sibling_job_does_not_suppress(tmp_path):
    """The annotation is scoped to the job it sits in."""
    body = (
        f"name: x\non:\n{PR_AND_SCHEDULE}jobs:\n"
        "  other:\n    # collapsing-group-ok: unrelated\n    runs-on: ubuntu-latest\n    steps: []\n"
        "  sweep:\n" + COLLAPSING_JOB
    )
    assert len(cjg.check_file(_write(tmp_path, body))) == 1


# ── unreadable input ─────────────────────────────────────────────────────────


def test_unparseable_yaml_is_reported_not_passed(tmp_path):
    """A file the lint cannot read must fail loudly, never pass as clean."""
    found = cjg.check_file(_write(tmp_path, "on:\n  pull_request:\n jobs: ]["))
    assert len(found) == 1
    line, message = found[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_a_non_mapping_document_is_clean(tmp_path):
    assert cjg.check_file(_write(tmp_path, "- a\n- b\n")) == []

"""Tests for hooks/check_pending_cancel_concurrency.py — the (opinionated) lint
that forbids a per-ref/per-PR concurrency group (workflow-level OR job-level) on
a required-check workflow whose `on.pull_request.types` includes an activity
type outside {opened, synchronize, reopened}. Those types fire extra runs on the
SAME head SHA; GitHub's one-running + one-pending slot per group then cancels a
current-SHA sibling, whose always() reporter resolves 'cancelled' and reddens
the required check with no real failure."""

from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, load_hook

pc = load_hook("check_pending_cancel_concurrency.py", "check_pending_cancel")

# A workflow that backs a required check: decide gate + an always() reporter.
REQUIRED_CHECK_JOBS = (
    "jobs:\n"
    "  decide:\n"
    "    uses: ./.github/workflows/decide-reusable.yaml\n"
    "  work:\n"
    "    needs: decide\n"
    "    if: needs.decide.outputs.run == 'true'\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
    "  report:\n"
    "    needs: [decide, work]\n"
    "    if: always()\n"
    "    runs-on: ubuntu-latest\n"
    "    steps: []\n"
)

STORM_TRIGGER = "name: x\non:\n  pull_request:\n    types: [opened, labeled]\n"
DEFAULT_TRIGGER = "name: x\non:\n  pull_request:\n    types: [opened, synchronize]\n"

REF_GROUP = (
    "concurrency:\n"
    "  group: ${{ github.head_ref || github.ref }}\n"
    "  cancel-in-progress: false\n"
)


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _job_group(job: str, group: str) -> str:
    """REQUIRED_CHECK_JOBS with a concurrency block inserted into JOB."""
    key = f"  {job}:\n"
    conc = f"    concurrency:\n      group: {group}\n      cancel-in-progress: false\n"
    assert key in REQUIRED_CHECK_JOBS
    return REQUIRED_CHECK_JOBS.replace(key, key + conc)


# ── RED: the incident shape ───────────────────────────────────────────────────


def test_ref_keyed_workflow_level_group_with_storm_types_is_an_error(tmp_path):
    body = STORM_TRIGGER + REF_GROUP + REQUIRED_CHECK_JOBS
    violations = pc.check_file(_write(tmp_path, body))
    assert len(violations) == 1
    line, message = violations[0]
    # The annotation must point at the offending workflow-level `concurrency:` key.
    assert body.splitlines()[line - 1].startswith("concurrency:")
    assert "workflow-level" in message
    assert "labeled" in message
    assert "github.run_id" in message


def test_ref_keyed_job_level_group_with_storm_types_is_an_error(tmp_path):
    body = STORM_TRIGGER + _job_group(
        "report", "report-${{ github.head_ref || github.ref }}"
    )
    violations = pc.check_file(_write(tmp_path, body))
    assert len(violations) == 1
    line, message = violations[0]
    assert body.splitlines()[line - 1].strip().startswith("concurrency:")
    assert "job 'report'" in message
    assert "labeled" in message


def test_every_offending_block_is_reported(tmp_path):
    """Workflow-level AND job-level ref-keyed groups each get their own finding."""
    body = (
        STORM_TRIGGER
        + REF_GROUP
        + _job_group("work", "w-${{ github.event.pull_request.number }}")
    )
    violations = pc.check_file(_write(tmp_path, body))
    assert len(violations) == 2
    messages = [m for _l, m in violations]
    assert any("workflow-level" in m for m in messages)
    assert any("job 'work'" in m for m in messages)


def test_pr_number_key_counts_as_per_pr(tmp_path):
    body = (
        STORM_TRIGGER
        + (
            "concurrency:\n  group: x-${{ github.event.pull_request.number }}\n"
            "  cancel-in-progress: true\n"
        )
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


def test_cancel_in_progress_true_does_not_make_it_safe(tmp_path):
    """With `true` the same-SHA event cancels the IN-PROGRESS run — still red on
    the current head. The cancel flag is not the discriminator; the types are."""
    body = (
        STORM_TRIGGER
        + ("concurrency:\n  group: ${{ github.ref }}\n  cancel-in-progress: true\n")
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


def test_scalar_types_value_is_normalized(tmp_path):
    """GitHub treats `types: labeled` (scalar) as `[labeled]` — so must we."""
    body = (
        "name: x\non:\n  pull_request:\n    types: labeled\n"
        + REF_GROUP
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


def test_pull_request_target_storm_types_also_flag(tmp_path):
    body = (
        "name: x\non:\n  pull_request_target:\n    types: [opened, labeled]\n"
        + REF_GROUP
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


def test_quoted_on_key_is_parsed(tmp_path):
    """A quoted `"on":` key parses as the string "on", not YAML 1.1's boolean
    True — the trigger lookup must find it either way."""
    body = (
        '"on":\n  pull_request:\n    types: [opened, labeled]\n'
        + REF_GROUP
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


# ── GREEN: each condition individually absent ────────────────────────────────


@pytest.mark.parametrize(
    "group",
    [
        "${{ github.run_id }}",
        "x-${{ github.head_ref || github.ref }}-${{ github.run_id }}",
        "x-${{ github.ref }}-${{ github.run_number }}",
    ],
)
def test_per_run_keyed_group_is_clean(tmp_path, group):
    """github.run_id / github.run_number make the group a group of one — it
    cannot cancel a sibling, even when a ref key is also present."""
    body = (
        STORM_TRIGGER
        + (f"concurrency:\n  group: {group}\n  cancel-in-progress: false\n")
        + REQUIRED_CHECK_JOBS
    )
    assert pc.check_file(_write(tmp_path, body)) == []


def test_absent_group_is_clean(tmp_path):
    body = STORM_TRIGGER + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


def test_default_types_only_is_clean(tmp_path):
    """{opened, synchronize} fire at most one run per head SHA — a ref-keyed
    group is only ever superseded by a newer commit, whose reporter re-posts."""
    body = DEFAULT_TRIGGER + REF_GROUP + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


@pytest.mark.parametrize(
    "trigger",
    ["name: x\non:\n  pull_request:\n", "name: x\non:\n  pull_request: ~\n"],
)
def test_bare_pull_request_shorthand_is_clean(tmp_path, trigger):
    """`pull_request:` with no `types:` (and the `~` form) means the default set."""
    body = trigger + REF_GROUP + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


def test_list_form_trigger_is_clean(tmp_path):
    body = "name: x\non: [push, pull_request]\n" + REF_GROUP + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


def test_no_required_check_shape_is_clean(tmp_path):
    """Storm types + ref-keyed group but no decide gate / always() reporter —
    e.g. a label-gated release-prep workflow — is not this lint's business."""
    jobs = (
        "jobs:\n"
        "  bump:\n"
        "    if: github.event.label.name == 'release'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: []\n"
    )
    body = STORM_TRIGGER + REF_GROUP + jobs
    assert pc.check_file(_write(tmp_path, body)) == []


def test_decide_gate_without_reporter_is_clean(tmp_path):
    jobs = (
        "jobs:\n"
        "  decide:\n"
        "    uses: ./.github/workflows/decide-reusable.yaml\n"
        "  work:\n"
        "    needs: decide\n"
        "    if: needs.decide.outputs.run == 'true'\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: []\n"
    )
    body = STORM_TRIGGER + REF_GROUP + jobs
    assert pc.check_file(_write(tmp_path, body)) == []


def test_static_group_is_not_this_lints_business(tmp_path):
    """A group with no per-ref key is out of scope: at the workflow level it is
    check_static_concurrency's territory; a job-level static group is a
    documented handoff gap neither lint flags (see the module docstring)."""
    body = (
        STORM_TRIGGER
        + ("concurrency:\n  group: my-static-lock\n  cancel-in-progress: false\n")
        + REQUIRED_CHECK_JOBS
    )
    assert pc.check_file(_write(tmp_path, body)) == []


# ── opt-out ───────────────────────────────────────────────────────────────────


def test_opt_out_comment_suppresses_the_error(tmp_path):
    body = f"# {pc.OPT_OUT}\n" + STORM_TRIGGER + REF_GROUP + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


def test_opt_out_token_in_string_value_does_not_suppress(tmp_path):
    """The opt-out counts only inside a real `#` comment — a group value that
    literally contains the token must still be flagged (fail-open otherwise)."""
    body = (
        STORM_TRIGGER
        + (
            f'concurrency:\n  group: "{pc.OPT_OUT}-${{{{ github.ref }}}}"\n'
            "  cancel-in-progress: false\n"
        )
        + REQUIRED_CHECK_JOBS
    )
    assert len(pc.check_file(_write(tmp_path, body))) == 1


# ── malformed / degenerate input ──────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    violations = pc.check_file(_write(tmp_path, "on: [pull_request\nconcurrency: {\n"))
    assert len(violations) == 1
    line, message = violations[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_non_dict_yaml_top_level_is_ignored(tmp_path):
    assert pc.check_file(_write(tmp_path, "- item1\n- item2\n")) == []


def test_non_mapping_jobs_is_ignored(tmp_path):
    body = STORM_TRIGGER + REF_GROUP + "jobs: scalar-not-a-mapping\n"
    assert pc.check_file(_write(tmp_path, body)) == []


def test_scalar_concurrency_shorthand_is_the_group(tmp_path):
    """`concurrency: <expr>` is GitHub shorthand for `{group: <expr>,
    cancel-in-progress: false}` — a ref-keyed scalar is the incident shape."""
    body = STORM_TRIGGER + "concurrency: ci-${{ github.ref }}\n" + REQUIRED_CHECK_JOBS
    assert len(pc.check_file(_write(tmp_path, body))) == 1


def test_scalar_static_concurrency_is_clean(tmp_path):
    """A static scalar shorthand has no per-ref key — not this lint's business."""
    body = STORM_TRIGGER + "concurrency: my-lock\n" + REQUIRED_CHECK_JOBS
    assert pc.check_file(_write(tmp_path, body)) == []


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_reports_violation_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "bad.yaml").write_text(STORM_TRIGGER + REF_GROUP + REQUIRED_CHECK_JOBS)
    monkeypatch.setattr(pc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(pc, "WORKFLOWS_DIR", wf_dir)
    monkeypatch.setattr(pc, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert pc.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=5::" in out
    assert "violation" in out


def test_all_shipped_workflows_pass(monkeypatch, capsys):
    """The repo dogfoods this lint: its storm-typed workflows (release-prep,
    phone-home) back no required check, so none are flagged."""
    monkeypatch.setattr(pc, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(pc, "WORKFLOWS_DIR", REPO_ROOT / ".github" / "workflows")
    monkeypatch.setattr(pc, "ACTIONS_DIR", REPO_ROOT / ".github" / "actions")
    assert pc.main() == 0, capsys.readouterr().out

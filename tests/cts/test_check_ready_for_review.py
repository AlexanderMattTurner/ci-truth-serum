"""Tests for ci_truth_serum/check_ready_for_review.py — the lint that makes a
draft-gated workflow fire again when the pull request leaves draft.

`ready_for_review` is not a default `pull_request` activity type. A workflow that
skips jobs while the pull request is a draft and omits that type never fires
again, so its skipped check runs stand and GitHub counts them as satisfied."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

rfr = load_hook("check_ready_for_review.py", "check_ready_for_review")

DRAFT_STEP = (
    "jobs:\n"
    "  build:\n"
    "    runs-on: ubuntu-latest\n"
    "    steps:\n"
    "      - run: ./decide.sh\n"
    "        env:\n"
    "          IS_DRAFT: ${{ github.event.pull_request.draft }}\n"
)


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


# ── violations ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("trigger_block", "label"),
    [
        ("on:\n  pull_request:\n", "no types: at all, so the default set applies"),
        (
            "on:\n  pull_request:\n    types: [opened, synchronize, reopened]\n",
            "the default set spelled out",
        ),
        ("on: pull_request\n", "the scalar trigger form"),
        ("on: [pull_request, merge_group]\n", "the list trigger form"),
        (
            "on:\n  pull_request_target:\n    types: [opened, synchronize]\n",
            "pull_request_target, which carries the same activity types",
        ),
    ],
)
def test_draft_gate_without_ready_for_review_is_flagged(tmp_path, trigger_block, label):
    result = rfr.check_file(_write(tmp_path, trigger_block + DRAFT_STEP))
    assert len(result) == 1, label
    line, message = result[0]
    assert "ready_for_review" in message
    assert line is not None


def test_reusable_call_input_named_draft_is_flagged(tmp_path):
    """The gate sits in the callee, so the payload field never appears here. This
    is the shape a payload-field-only detector misses; it was 10 of 18 draft-gated
    workflows on the tree this lint was dogfooded against."""
    body = (
        "on:\n  pull_request:\n"
        "jobs:\n"
        "  decide:\n"
        "    uses: ./.github/workflows/decide-reusable.yaml\n"
        "    with:\n"
        "      skip-on-draft: true\n"
    )
    result = rfr.check_file(_write(tmp_path, body))
    assert len(result) == 1
    assert "ready_for_review" in result[0][1]


def test_draft_gate_in_a_job_if_is_flagged(tmp_path):
    body = (
        "on:\n  pull_request:\n"
        "jobs:\n"
        "  build:\n"
        "    if: ${{ !github.event.pull_request.draft }}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: make test\n"
    )
    assert len(rfr.check_file(_write(tmp_path, body))) == 1


def test_the_reported_line_is_the_trigger_key(tmp_path):
    """The fix is a `types:` entry under the trigger, so the report anchors there
    rather than on the gated job."""
    body = "name: x\non:\n  pull_request:\n" + DRAFT_STEP
    line, _ = rfr.check_file(_write(tmp_path, body))[0]
    assert body.splitlines()[line - 1].strip() == "pull_request:"


# ── false-positive guards ─────────────────────────────────────────────────────


def test_ready_for_review_declared_is_clean(tmp_path):
    body = (
        "on:\n  pull_request:\n"
        "    types: [opened, synchronize, reopened, ready_for_review]\n"
    ) + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_one_declaring_trigger_covers_the_workflow(tmp_path):
    """Either trigger's run re-fires the whole workflow, so the gated jobs get
    their chance whichever one delivered the event."""
    body = (
        "on:\n"
        "  pull_request:\n"
        "    types: [opened, synchronize, ready_for_review]\n"
        "  pull_request_target:\n"
        "    types: [opened]\n"
    ) + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_no_draft_gate_is_clean(tmp_path):
    body = (
        "on:\n  pull_request:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - run: make test\n"
    )
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_reusable_workflow_declares_no_pull_request_trigger(tmp_path):
    """A `workflow_call` workflow holds the draft gate but names no trigger the
    author can add a type to. Its CALLER is the file this lint judges."""
    body = (
        "on:\n  workflow_call:\n    inputs:\n      skip-on-draft:\n"
        "        type: boolean\n"
    ) + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_push_only_workflow_is_clean(tmp_path):
    body = ("on:\n  push:\n    branches: [main]\n") + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_reusable_call_input_without_draft_in_its_name_is_clean(tmp_path):
    body = (
        "on:\n  pull_request:\n"
        "jobs:\n"
        "  decide:\n"
        "    uses: ./.github/workflows/decide-reusable.yaml\n"
        "    with:\n"
        "      paths-regex: '^src/'\n"
    )
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_opt_out_suppresses(tmp_path):
    body = ("# ready-for-review-ok\non:\n  pull_request:\n") + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []


def test_opt_out_token_inside_a_string_does_not_suppress(tmp_path):
    """A fail-open: the token must be read out of a COMMENT, never anywhere in the
    byte stream, or a value could silently switch the lint off."""
    body = (
        "on:\n  pull_request:\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: echo ready-for-review-ok\n"
        "        env:\n"
        "          IS_DRAFT: ${{ github.event.pull_request.draft }}\n"
    )
    assert len(rfr.check_file(_write(tmp_path, body))) == 1


# ── the file the lint cannot read ─────────────────────────────────────────────


def test_unparseable_yaml_is_reported_not_passed(tmp_path):
    result = rfr.check_file(_write(tmp_path, "on:\n  pull_request:\n\tbad: [tab\n"))
    assert len(result) == 1
    line, message = result[0]
    assert line is None
    assert "could not parse" in message


def test_a_bare_scalar_types_value_declares_the_type(tmp_path):
    """`types:` takes a scalar as well as a list, and both declare the type."""
    body = ("on:\n  pull_request:\n    types: ready_for_review\n") + DRAFT_STEP
    assert rfr.check_file(_write(tmp_path, body)) == []

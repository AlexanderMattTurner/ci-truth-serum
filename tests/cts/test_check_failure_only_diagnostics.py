"""Tests for ci_truth_serum/check_failure_only_diagnostics.py — the (opinionated)
lint that fails a diagnostics step GitHub skips on a cancelled run.

`failure()` is false when a job is cancelled, and a job that passes its
`timeout-minutes` ends cancelled. A step gated on `failure()` alone therefore
loses the artifact on the run whose evidence matters most.

Drives check_file(path) directly so each rule is asserted on its own."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

fod = load_hook("check_failure_only_diagnostics.py", "check_failure_only_diagnostics")

UPLOAD = "actions/upload-artifact@v4"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _job(steps: str, job_if: str = "", name: str = "test") -> str:
    gate = f"    if: {job_if}\n" if job_if else ""
    return (
        f"name: x\non:\n  pull_request:\njobs:\n  {name}:\n"
        f"{gate}    runs-on: ubuntu-latest\n    steps:\n{steps}"
    )


def _step(condition: str | None = None, **fields: str) -> str:
    lines = ["      - name: collect"]
    if condition is not None:
        lines.append(f"        if: {condition}")
    lines += [f"        {key}: {value}" for key, value in fields.items()]
    return "\n".join(lines) + "\n"


def _messages(path: Path) -> list[str]:
    return [message for _, message in fod.check_file(path)]


# ── flagged: the condition names no cancellation ─────────────────────────────


@pytest.mark.parametrize(
    "condition",
    [
        "failure()",
        "${{ failure() }}",
        "${{ failure() && github.event_name == 'push' }}",
        "failure() || steps.build.outcome == 'failure'",
        # A negated mention still keeps the step off the cancelled run.
        "${{ failure() && !cancelled() }}",
        "${{ failure() && ! cancelled() }}",
    ],
)
def test_an_upload_gated_on_failure_alone_is_an_error(tmp_path, condition):
    path = _write(tmp_path, _job(_step(condition, uses=UPLOAD)))
    messages = _messages(path)
    assert len(messages) == 1
    assert "uploads an artifact" in messages[0]
    assert "cancelled" in messages[0]


@pytest.mark.parametrize(
    ("script", "named"),
    [
        ("bash .github/scripts/collect-logs.sh", "collect-logs.sh"),
        ("./scripts/collect_logs.sh", "collect_logs.sh"),
        ("python3 tools/dump-artifacts.py", "dump-artifacts.py"),
        ("sh ./ci/diagnostics.sh", "diagnostics.sh"),
        ("node scripts/triage.mjs --pr 3", "triage.mjs"),
        ("bash ci/post-mortem.sh", "post-mortem.sh"),
    ],
)
def test_a_diagnostics_script_gated_on_failure_alone_is_an_error(
    tmp_path, script, named
):
    path = _write(tmp_path, _job(_step("failure()", run=script)))
    messages = _messages(path)
    assert len(messages) == 1
    assert named in messages[0]


def test_a_local_diagnostics_action_is_an_error(tmp_path):
    step = _step("failure()", uses="./.github/actions/collect-diagnostics")
    messages = _messages(_write(tmp_path, _job(step)))
    assert len(messages) == 1
    assert "diagnostics action ./.github/actions/collect-diagnostics" in messages[0]


def test_a_job_level_gate_is_judged_for_a_step_that_carries_none(tmp_path):
    """A gate on the job skips every step under it, so a diagnostics job gated
    on failure() loses its artifacts the same way."""
    path = _write(tmp_path, _job(_step(uses=UPLOAD), job_if="failure()"))
    messages = _messages(path)
    assert len(messages) == 1
    assert "the 'test' job" in messages[0]


def test_the_finding_lands_on_the_step_line(tmp_path):
    path = _write(tmp_path, _job(_step("failure()", uses=UPLOAD)))
    ((line, _message),) = fod.check_file(path)
    assert path.read_text(encoding="utf-8").splitlines()[line - 1].endswith("collect")


def test_a_composite_action_step_is_judged_too(tmp_path):
    body = (
        "name: collect\nruns:\n  using: composite\n  steps:\n"
        "      - if: failure()\n        uses: actions/upload-artifact@v4\n"
    )
    assert len(_messages(_write(tmp_path, body, "action.yaml"))) == 1


def test_every_flagged_step_of_one_job_is_reported(tmp_path):
    steps = _step("failure()", uses=UPLOAD) + _step(
        "failure()", run="bash ci/collect-logs.sh"
    )
    assert len(_messages(_write(tmp_path, _job(steps)))) == 2


# ── clean: the condition names cancellation, or the step collects nothing ────


@pytest.mark.parametrize(
    "condition",
    [
        "always()",
        "${{ always() }}",
        "failure() || cancelled()",
        "${{ cancelled() || failure() }}",
        "${{ !cancelled() }}",  # it calls no failure(), so this check has no say
        "success()",
        "steps.build.outcome == 'failure'",
        "needs.build.result == 'failure'",
    ],
)
def test_a_condition_that_names_cancellation_or_no_failure_is_clean(
    tmp_path, condition
):
    assert _messages(_write(tmp_path, _job(_step(condition, uses=UPLOAD)))) == []


def test_a_step_with_no_condition_at_all_is_clean(tmp_path):
    assert _messages(_write(tmp_path, _job(_step(uses=UPLOAD)))) == []


def test_an_ordinary_step_gated_on_failure_is_clean(tmp_path):
    """The check is about evidence a run leaves behind, not about every
    failure-gated step: a notifier belongs to check-cron-alert-coverage."""
    step = _step("failure()", run="bash .github/scripts/open-issue.sh")
    assert _messages(_write(tmp_path, _job(step))) == []


def test_a_script_name_inside_a_printed_message_is_not_a_call(tmp_path):
    """The `run:` body is read on the bash grammar, so a command that only
    prints text holds no call at all."""
    step = _step("failure()", run="echo 'run collect-logs.sh to see the report'")
    assert _messages(_write(tmp_path, _job(step))) == []


def test_a_clean_step_condition_does_not_excuse_the_job_gate(tmp_path):
    """Both gates have to let the step run: an always() step inside a job gated
    on failure() never starts, so the artifact is lost just the same."""
    path = _write(tmp_path, _job(_step("always()", uses=UPLOAD), job_if="failure()"))
    messages = _messages(path)
    assert len(messages) == 1
    assert "the 'test' job" in messages[0]


def test_the_step_condition_is_named_before_the_job_gate(tmp_path):
    """Both are gated on failure(), and one finding names the step's own."""
    path = _write(tmp_path, _job(_step("failure()", uses=UPLOAD), job_if="failure()"))
    messages = _messages(path)
    assert len(messages) == 1
    assert messages[0].startswith("step 'collect'")


def test_a_job_gate_that_names_cancellation_is_clean(tmp_path):
    path = _write(tmp_path, _job(_step(uses=UPLOAD), job_if="always()"))
    assert _messages(path) == []


# ── the opt-out ──────────────────────────────────────────────────────────────


def test_an_annotated_step_is_suppressed(tmp_path):
    body = _job(
        "      # failure-only-diagnostics-ok: the report holds nothing on a cancel\n"
        + _step("failure()", uses=UPLOAD)
    )
    assert _messages(_write(tmp_path, body)) == []


def test_a_bare_annotation_does_not_suppress(tmp_path):
    """The reason is required: a token with no reason states no decision."""
    body = _job(
        "      # failure-only-diagnostics-ok\n" + _step("failure()", uses=UPLOAD)
    )
    assert len(_messages(_write(tmp_path, body))) == 1


def test_the_annotation_is_scoped_to_the_step_it_sits_on(tmp_path):
    """A job that keeps one deliberate failure() upload still gets a finding on
    its next diagnostics step."""
    body = _job(
        "      # failure-only-diagnostics-ok: this one is deliberate\n"
        + _step("failure()", uses=UPLOAD)
        + _step("failure()", run="bash ci/collect-logs.sh")
    )
    assert len(_messages(_write(tmp_path, body))) == 1


# ── fail-closed on the artifact under test ───────────────────────────────────


def test_unparseable_yaml_is_a_violation_not_a_pass(tmp_path):
    findings = fod.check_file(_write(tmp_path, "jobs:\n  a:\n   - x\n  b:\n\t- y\n"))
    assert len(findings) == 1
    line, message = findings[0]
    assert line == 1
    assert "could not parse this workflow or an action it uses as YAML" in message


def test_a_document_that_is_not_a_mapping_is_clean(tmp_path):
    assert fod.check_file(_write(tmp_path, "- just\n- a list\n")) == []


# ── the condition reader ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("condition", "gated"),
    [
        ("failure()", True),
        ("FAILURE()", False),  # GitHub's function names are lower case
        ("steps.x.outputs.failure()", False),  # a property path, not the function
        ("failure() || cancelled()", False),
        ("always()", False),
        (None, False),
    ],
)
def test_gated_on_failure_reads_the_condition(condition, gated):
    assert fod.gated_on_failure(condition) is gated


def test_covers_cancellation_rejects_a_negated_mention():
    assert fod.covers_cancellation("failure() || cancelled()") is True
    assert fod.covers_cancellation("failure() && !cancelled()") is False
    assert fod.covers_cancellation("failure()") is False


@pytest.mark.parametrize(
    ("condition", "covers"),
    [
        ("failure() || cancelled()", True),
        ("always()", True),
        ("failure() && !cancelled()", False),
        # The `!` reaches its call through the parentheses, so this is the same
        # condition as the one above and skips the cancelled run too.
        ("failure() && !(cancelled())", False),
        ("failure() && ! ( cancelled() )", False),
        ("failure() && !(always())", False),
        # A `(` that opens a group rather than following a `!` is not a negation.
        ("failure() && (cancelled())", True),
        ("success() && !failure() || cancelled()", True),
    ],
)
def test_covers_cancellation_reads_a_parenthesized_negation(condition, covers):
    assert fod.covers_cancellation(condition) is covers


# ── running a script, versus naming one ──────────────────────────────────────


@pytest.mark.parametrize(
    "script",
    [
        "rm ci/collect-logs.sh",
        "cat ci/collect-logs.sh",
        "tool --exclude ci/collect-logs.sh",
        "git add ci/collect-logs.sh",
        "shellcheck ci/collect-logs.sh",
    ],
)
def test_a_command_that_only_names_a_script_is_clean(tmp_path, script):
    """Deleting, printing or listing a diagnostics script does not run it, so
    reading every argument as a call would flag all three."""
    assert _messages(_write(tmp_path, _job(_step("failure()", run=script)))) == []


@pytest.mark.parametrize(
    "script",
    [
        "sudo bash ci/collect-logs.sh",
        "bash -e ci/collect-logs.sh",
        "env ./ci/collect-logs.sh",
    ],
)
def test_a_wrapped_or_flagged_invocation_is_still_a_call(tmp_path, script):
    assert len(_messages(_write(tmp_path, _job(_step("failure()", run=script))))) == 1


# ── a composite action hides the upload ──────────────────────────────────────


def _composite(tmp_path, body: str, name: str = "playwright") -> None:
    directory = tmp_path / ".github" / "actions" / name
    directory.mkdir(parents=True)
    (directory / "action.yaml").write_text(body, encoding="utf-8")


UPLOADING_COMPOSITE = (
    "name: playwright\nruns:\n  using: composite\n  steps:\n"
    "      - run: pnpm test\n"
    "      - uses: actions/upload-artifact@v4\n"
)


def test_a_composite_that_uploads_is_judged_at_its_caller(tmp_path, monkeypatch):
    """The directory name says nothing about evidence, so only following the
    action finds the upload the caller's gate skips."""
    monkeypatch.setattr(fod, "REPO_ROOT", tmp_path)
    _composite(tmp_path, UPLOADING_COMPOSITE)
    step = _step("failure()", uses="./.github/actions/playwright")
    messages = _messages(_write(tmp_path, _job(step)))
    assert len(messages) == 1
    assert "./.github/actions/playwright, which uploads an artifact" in messages[0]


def test_a_composite_that_collects_nothing_is_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(fod, "REPO_ROOT", tmp_path)
    _composite(
        tmp_path,
        "name: build\nruns:\n  using: composite\n  steps:\n      - run: pnpm build\n",
        "build",
    )
    step = _step("failure()", uses="./.github/actions/build")
    assert _messages(_write(tmp_path, _job(step))) == []


def test_two_actions_that_use_each_other_terminate(tmp_path, monkeypatch):
    monkeypatch.setattr(fod, "REPO_ROOT", tmp_path)
    _composite(
        tmp_path,
        "name: a\nruns:\n  using: composite\n  steps:\n"
        "      - uses: ./.github/actions/b\n",
        "a",
    )
    _composite(
        tmp_path,
        "name: b\nruns:\n  using: composite\n  steps:\n"
        "      - uses: ./.github/actions/a\n",
        "b",
    )
    step = _step("failure()", uses="./.github/actions/a")
    assert _messages(_write(tmp_path, _job(step))) == []


# ── the opt-out must be a real YAML comment ──────────────────────────────────


def test_a_marker_inside_a_string_value_does_not_suppress(tmp_path):
    """A step could otherwise turn the check off by naming its own opt-out."""
    step = (
        '      - name: "# failure-only-diagnostics-ok: not a comment"\n'
        "        if: failure()\n"
        f"        uses: {UPLOAD}\n"
    )
    assert len(_messages(_write(tmp_path, _job(step)))) == 1


def test_a_marker_a_run_body_echoes_does_not_suppress(tmp_path):
    step = (
        "      - if: failure()\n"
        "        run: |\n"
        '          echo "# failure-only-diagnostics-ok: not a comment"\n'
        "          bash ci/collect-logs.sh\n"
    )
    assert len(_messages(_write(tmp_path, _job(step)))) == 1


def test_comment_view_keeps_only_yaml_comments():
    """Code becomes blanks rather than vanishing, so every column — and so every
    line number a finding reports — still lines up with the source."""
    text = 'name: "# x"  # real: why\nrun: echo hi\n'
    assert fod.comment_view(text) == [" " * 13 + "# real: why", " " * 12]

"""Tests for ci_truth_serum/check_trusted_base.py — the (security) pre-commit lint
that flags a privileged pull_request(_target) job staging code from a ref the PR
AUTHOR chooses. Two such refs, one lint:

  * the PR HEAD (the canonical pwn-request), and
  * the PR BASE — nothing requires a PR to be based on the default branch, so
    pushing branch A with a rewritten script and opening PR B *based on A* makes
    a "stage the base branch" job run A under the repo's credentials while PR B's
    own diff stays clean.

Drives check_file(path) directly so each rule is asserted in isolation, and every
assertion is on an observable verdict (finding count, message, reported line),
never on the lint's source text."""

from pathlib import Path

import pytest
import yaml

from tests._helpers import REPO_ROOT, load_hook

tb = load_hook("check_trusted_base.py", "check_trusted_base")
ue = load_hook("check_untrusted_exec.py", "untrusted_exec_for_trusted_base")

_HEAD_CHECKOUT = (
    "    steps:\n"
    "      - uses: actions/checkout@v4\n"
    "        with:\n"
    "          ref: ${{ github.event.pull_request.head.sha }}\n"
)

_BASE_REFS = (
    "${{ github.event.pull_request.base.ref }}",
    "${{ github.event.pull_request.base.sha }}",
    "${{ github.base_ref }}",
)


def _base_checkout(ref: str, uses: str = "actions/checkout@v4") -> str:
    return (
        "    steps:\n"
        f"      - uses: {uses}\n"
        "        with:\n"
        f"          ref: {ref}\n"
        "      - run: bash .github/scripts/release-prep.sh\n"
    )


def _privileged_wf(steps: str, trigger: str = "pull_request_target") -> str:
    """A privileged (contents: write) PR-triggered workflow with one job."""
    return (
        f"on:\n  {trigger}:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n" + steps
    )


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── the flagged shape ────────────────────────────────────────────────────────


def test_head_checkout_with_write_permissions_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    result = tb.check_file(path)
    assert len(result) == 1
    line, message = result[0]
    assert line == 6  # the `build:` job key
    assert "pwn-request" in message
    assert "build" in message


def test_head_checkout_with_secret_in_step_env_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.head_ref }}\n"
        "      - run: ./build.sh\n"
        "        env:\n          TOKEN: ${{ secrets.NPM_TOKEN }}\n",
    )
    result = tb.check_file(path)
    assert len(result) == 1
    assert "pwn-request" in result[0][1]


def test_head_ref_dot_ref_variant_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  pull-requests: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.ref }}\n",
    )
    assert len(tb.check_file(path)) == 1


# ── safe shapes (not flagged) ────────────────────────────────────────────────


def test_head_checkout_read_only_no_secret_is_clean(tmp_path):
    """Checking out untrusted code with only read perms and no secret is the
    CORRECT way to lint/build a PR — it must not be flagged."""
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_privileged_but_no_head_checkout_is_clean(tmp_path):
    """A privileged job that checks out the trusted base merge ref (the default)
    is safe."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n",
    )
    assert tb.check_file(path) == []


def test_non_pr_trigger_is_clean(tmp_path):
    """The vulnerability only exists on pull_request(_target) — a push workflow
    that checks out a ref with write perms is not this class."""
    path = _write(
        tmp_path,
        "on:\n  push:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_empty_permissions_drops_all_scopes_is_clean(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions: {}\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_write_all_string_permissions_is_flagged(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions: write-all\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


# ── opt-out (reason required) ────────────────────────────────────────────────


def test_optout_with_reason_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "# trusted-base-ok: runs only the base branch's trusted script copy\n"
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert tb.check_file(path) == []


def test_reasonless_optout_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        "# trusted-base-ok:\n"
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


def test_optout_token_in_string_value_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        'env:\n  NOTE: "trusted-base-ok: fake reason in a value"\n'
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
    )
    assert len(tb.check_file(path)) == 1


# ── malformed / non-dict ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    path = _write(tmp_path, "on: [pull_request\njobs: {\n")
    result = tb.check_file(path)
    assert len(result) == 1
    assert result[0][0] is None
    assert "could not parse as YAML" in result[0][1]


def test_non_dict_yaml_is_ignored(tmp_path):
    path = _write(tmp_path, "- a\n- b\n", name="list.yaml")
    assert tb.check_file(path) == []


# ── main wiring ──────────────────────────────────────────────────────────────


def test_main_reports_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT
    )
    monkeypatch.setattr(tb, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(tb, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(tb, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert tb.main() == 1
    out = capsys.readouterr().out
    assert "pwn-request" in out
    assert "violation" in out


# ── the author-chosen BASE ref ───────────────────────────────────────────────


@pytest.mark.parametrize("ref", _BASE_REFS)
def test_base_ref_checkout_with_write_permissions_is_flagged(tmp_path, ref):
    """Every spelling of the base ref, member by member: `base.ref`, `base.sha`,
    and the `github.base_ref` shorthand all resolve to the branch the PR author
    chose as its base."""
    result = tb.check_file(_write(tmp_path, _privileged_wf(_base_checkout(ref))))
    assert len(result) == 1
    line, message = result[0]
    assert line == 6  # the `stage:` job key
    assert "job 'stage'" in message


def test_base_ref_checkout_with_secret_in_step_env_is_flagged(tmp_path):
    """Privilege via a secret rather than a write scope — same conjunct as the head
    finding uses."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.base.ref }}\n"
        "      - run: bash .github/scripts/release-prep.sh\n"
        "        env:\n          TOKEN: ${{ secrets.RELEASE_PAT }}\n",
    )
    assert len(tb.check_file(path)) == 1


def test_base_message_states_the_attack_the_bogus_fix_and_the_eliminator(tmp_path):
    """The message is the whole deliverable for a finding a reviewer will want to
    argue with: it must carry the stacked-PR mechanism, name the `main`/`master`
    narrowing as a NON-fix (it is the mitigation a reader would otherwise reach
    for), and point at re-deriving the default branch from the GitHub-written
    event payload."""
    result = tb.check_file(
        _write(tmp_path, _privileged_wf(_base_checkout(_BASE_REFS[0])))
    )
    assert len(result) == 1
    message = result[0][1]
    for needle in (
        "chosen by the PR AUTHOR",
        "open PR B whose BASE is A",
        "diff stays clean",
        "base.ref == 'main' || base.ref == 'master' does NOT fix this",
        "'master' is an ordinary pushable branch name",
        "$GITHUB_EVENT_PATH",
        ".repository.default_branch",
        "with no fallback",
        "trusted-base-ok",
    ):
        assert needle in message, needle


def test_main_ref_literal_guard_does_not_rescue_a_base_checkout(tmp_path):
    """The mitigation that looks convincing: gating the job on
    `base.ref == 'main' || base.ref == 'master'`. On a repo whose default branch is
    `main`, `master` is an ordinary branch anyone with push access can create — so
    the second disjunct hands the attack straight back, and this must still be a
    finding."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
        "    if: >-\n"
        "      github.event.pull_request.base.ref == 'main' ||\n"
        "      github.event.pull_request.base.ref == 'master'\n"
        + _base_checkout(_BASE_REFS[0]),
    )
    assert len(tb.check_file(path)) == 1


def test_head_and_base_in_one_job_yields_one_head_finding(tmp_path):
    """One hole, one finding. A job staging both refs is reported as the head
    finding, the older and broader of the two."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.base.ref }}\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.head_ref }}\n",
    )
    result = tb.check_file(path)
    assert len(result) == 1
    assert "pwn-request" in result[0][1]


def test_head_and_base_findings_are_distinguishable_but_both_named(tmp_path):
    """The two kinds keep separate remedies, so they carry separate messages — and
    the head one stays the finding it already was (`pwn-request`), so a consumer
    reading these annotations sees no change in the head class."""
    head = tb.check_file(
        _write(
            tmp_path,
            "on:\n  pull_request:\n"
            "permissions:\n  contents: write\n"
            "jobs:\n  stage:\n    runs-on: ubuntu-latest\n" + _HEAD_CHECKOUT,
            name="head.yaml",
        )
    )
    base = tb.check_file(
        _write(
            tmp_path,
            _privileged_wf(_base_checkout(_BASE_REFS[0])),
            name="base.yaml",
        )
    )
    assert len(head) == len(base) == 1
    assert "pwn-request" in head[0][1]
    assert "pwn-request" not in base[0][1]
    assert "BASE ref" in base[0][1]
    assert "BASE ref" not in head[0][1]


def test_base_optout_with_reason_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "# trusted-base-ok: stages only the payload-verified default branch\n"
        + _privileged_wf(_base_checkout(_BASE_REFS[0])),
    )
    assert tb.check_file(path) == []


# ── base ref as DATA, not as staged code (not flagged) ───────────────────────


def test_base_ref_in_env_for_a_diff_range_is_clean(tmp_path):
    """The dominant legitimate use by a wide margin: the base context read as data
    to compute a diff range. Nothing is materialized from it, so it is no finding —
    flagging this would report the idiom instead of the defect."""
    path = _write(
        tmp_path,
        "on:\n  pull_request:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
        "    env:\n"
        "      BASE_SHA: ${{ github.event.pull_request.base.sha }}\n"
        "      BASE_REF: ${{ github.base_ref }}\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        '      - run: git diff --name-only "$BASE_SHA"...HEAD\n',
    )
    assert tb.check_file(path) == []


def test_base_ref_handed_to_a_non_checkout_action_is_clean(tmp_path):
    """`with.ref` on an action that does not materialize a tree stages no code."""
    path = _write(
        tmp_path,
        _privileged_wf(_base_checkout(_BASE_REFS[0], uses="dorny/paths-filter@v3")),
    )
    assert tb.check_file(path) == []


@pytest.mark.parametrize("uses", ["actions/checkout@v4", "actions/checkout"])
def test_checkout_action_spellings_are_recognized(uses, tmp_path):
    path = _write(tmp_path, _privileged_wf(_base_checkout(_BASE_REFS[0], uses=uses)))
    assert len(tb.check_file(path)) == 1


def test_base_checkout_read_only_and_secret_free_is_clean(tmp_path):
    """A read-only base checkout is the ordinary, safe shape."""
    path = _write(
        tmp_path,
        "on:\n  pull_request_target:\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n" + _base_checkout(_BASE_REFS[0]),
    )
    assert tb.check_file(path) == []


def test_opaque_base_ref_is_not_guessed_at(tmp_path):
    """A ref routed through a job output could be anything; this lint reports only
    a named base CONTEXT, trading that recall for the precision that keeps it
    default-on (the same trade check_untrusted_exec documents)."""
    path = _write(
        tmp_path,
        _privileged_wf(_base_checkout("${{ needs.decide.outputs.base_ref }}")),
    )
    assert tb.check_file(path) == []


def test_base_checkout_on_a_non_pr_trigger_is_clean(tmp_path):
    """No pull request, no author-chosen base."""
    path = _write(
        tmp_path,
        "on:\n  push:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n" + _base_checkout(_BASE_REFS[0]),
    )
    assert tb.check_file(path) == []


# ── non-vacuity: each conjunct of the BASE rule must CONTRIBUTE ──────────────
#
# One violating fixture, mutated once per conjunct to remove exactly that
# conjunct's precondition. A mutant that still reports proves the conjunct does
# no work.

_BASE_VIOLATING = _privileged_wf(_base_checkout(_BASE_REFS[0]))


@pytest.mark.parametrize(
    ("mutation", "body"),
    [
        (
            "privilege-removed",
            "on:\n  pull_request_target:\n"
            "permissions:\n  contents: read\n"
            "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
            + _base_checkout(_BASE_REFS[0]),
        ),
        ("base-ref-removed", _privileged_wf(_base_checkout("main"))),
        (
            "staging-removed",
            _privileged_wf(_base_checkout(_BASE_REFS[0], uses="dorny/paths-filter@v3")),
        ),
        ("pr-trigger-removed", _privileged_wf(_base_checkout(_BASE_REFS[0]), "push")),
    ],
)
def test_each_base_conjunct_contributes(tmp_path, mutation, body):
    assert (
        len(tb.check_file(_write(tmp_path, _BASE_VIOLATING, name="base.yaml"))) == 1
    ), f"base fixture must be flagged for the {mutation} mutant to mean anything"
    assert tb.check_file(_write(tmp_path, body, name="mutant.yaml")) == [], mutation


# ── the reported_job_names contract check_untrusted_exec dedups on ───────────


def test_reported_job_names_covers_the_base_finding(tmp_path):
    """The exported contract is "the jobs this lint reports", so a base finding
    belongs in it — otherwise the sibling lint could report the same job again for
    the same hole."""
    body = _BASE_VIOLATING
    path = _write(tmp_path, body)
    assert tb.reported_job_names(yaml.safe_load(body), body) == ["stage"]
    assert len(tb.check_file(path)) == 1


def test_untrusted_exec_is_unaffected_by_the_base_finding(tmp_path):
    """The base-ref shape is not an untrusted-HEAD checkout, so the sibling lint
    was silent on it before this rule existed and stays silent now — the widening
    hands it no new job to skip."""
    path = _write(tmp_path, _BASE_VIOLATING)
    assert len(tb.check_file(path)) == 1
    assert ue.check_file(path) == []


def test_head_dedup_handoff_still_holds(tmp_path):
    """The pre-existing handoff: a job this lint reports for the HEAD ref is the
    sibling's to skip, and the same job resurfaces there once `# trusted-base-ok`
    silences this one."""
    body = (
        "on:\n  pull_request_target:\n"
        "jobs:\n  stage:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
        "      - run: pnpm build\n"
        "        env:\n          TOKEN: ${{ secrets.NPM_TOKEN }}\n"
    )
    reported = _write(tmp_path, body, name="reported.yaml")
    assert len(tb.check_file(reported)) == 1
    assert ue.check_file(reported) == []

    opted = _write(
        tmp_path,
        "# trusted-base-ok: only runs the base branch's trusted copy\n" + body,
        name="opted.yaml",
    )
    assert tb.check_file(opted) == []
    assert len(ue.check_file(opted)) == 1


# ── dogfood: the repo's own workflows are clean (release-prep opts out) ───────


def test_own_workflows_are_clean(monkeypatch):
    monkeypatch.setattr(tb, "REPO_ROOT", REPO_ROOT)
    monkeypatch.setattr(tb, "WORKFLOWS_DIR", REPO_ROOT / ".github" / "workflows")
    monkeypatch.setattr(tb, "ACTIONS_DIR", REPO_ROOT / ".github" / "actions")
    offenders = []
    for path in tb.workflow_files():
        offenders += [f"{path.name}: {msg}" for _, msg in tb.check_file(path)]
    assert offenders == [], offenders

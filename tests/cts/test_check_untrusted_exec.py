"""Tests for ci_truth_serum/check_untrusted_exec.py — the (security) pre-commit lint
that flags a job which EXECUTES code resolved from an untrusted (pull-request head)
checkout while secrets are live in that job.

Drives check_file(path) directly so each of the three conjuncts — untrusted
checkout, live secrets, execution form — is asserted in isolation, and the
non-vacuity section below proves each one CONTRIBUTES to the verdict.

Note on the fixtures: check_untrusted_exec deliberately skips a job that
check_trusted_base already reports, so a pull_request(_target) fixture that keeps
check_trusted_base silent is the only way to observe this lint's own verdict. The
realistic shape for that is the file-scoped `# trusted-base-ok:` opt-out, whose
claim ("it only runs the base branch's trusted copy") is exactly what this lint
exists to audit — so most fixtures carry it. The dedup handoff itself is asserted
in its own section."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

ue = load_hook("check_untrusted_exec.py", "check_untrusted_exec")
tb = load_hook("check_trusted_base.py", "check_trusted_base")

_TRUSTED_BASE_OK = "# trusted-base-ok: only runs the base branch's trusted copy\n"
_PR_HEAD_SHA = "${{ github.event.pull_request.head.sha }}"
_SECRET_ENV = "        env:\n          TOKEN: ${{ secrets.NPM_TOKEN }}\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _wf(ref: str, step: str, header: str = _TRUSTED_BASE_OK) -> str:
    """A pull_request_target workflow whose one job checks out REF then runs STEP.

    STEP is the full YAML source of the second step (already indented under
    `steps:`), so a test can vary the execution form and the step env together."""
    return (
        header + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        f"          ref: {ref}\n" + step
    )


def _run_step(command: str, env: str = _SECRET_ENV) -> str:
    return f"      - run: {command}\n" + env


# ── the flagged shape ────────────────────────────────────────────────────────


def test_local_composite_action_with_secret_is_flagged(tmp_path):
    """Form 1: `uses: ./…` reads a composite manifest out of the workspace, so the
    PR author supplies its steps."""
    path = _write(
        tmp_path,
        _wf(
            _PR_HEAD_SHA,
            "      - uses: ./.github/actions/foo\n" + _SECRET_ENV,
        ),
    )
    result = ue.check_file(path)
    assert len(result) == 1
    _line, message = result[0]
    assert "job 'build'" in message
    assert "local composite action `uses: ./.github/actions/foo`" in message
    assert "these secrets are live in the job: NPM_TOKEN" in message


@pytest.mark.parametrize(
    ("ref", "command"),
    [
        ("${{ github.head_ref }}", "pnpm build"),
        ("${{ github.event.pull_request.head.ref }}", "npm run test"),
        ("${{ matrix.pr.head_ref }}", "bash ./scripts/x.sh"),
    ],
)
def test_untrusted_ref_spellings_are_flagged(tmp_path, ref, command):
    path = _write(tmp_path, _wf(ref, _run_step(command)))
    assert len(ue.check_file(path)) == 1


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("pnpm build", "`pnpm build`"),
        ("npm run test", "`npm run test`"),
        ("pnpm exec some-bin", "`pnpm exec some-bin`"),
        ("yarn lint", "`yarn lint`"),
        ("bash ./scripts/x.sh", "`bash ./scripts/x.sh`"),
        ("node scripts/x.mjs", "`node scripts/x.mjs`"),
        ("./bin/deploy", "`./bin/deploy`"),
        ("make release", "`make release`"),
        ("npx some-bin", "`npx some-bin`"),
    ],
)
def test_execution_forms_are_flagged_with_their_label(tmp_path, command, label):
    path = _write(tmp_path, _wf(_PR_HEAD_SHA, _run_step(command)))
    result = ue.check_file(path)
    assert len(result) == 1
    assert label in result[0][1]


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("python -m tools.render", "`python -m tools.render`"),
        ("python3 -u -m tools", "`python3 -m tools`"),
        ("/usr/bin/python3 scripts/x.py", "`python3 scripts/x.py`"),
        ("pytest -q tests", "`pytest`"),
        ("python -m pytest", "`python -m pytest`"),
        ("env -u UV_NO_SYNC FOO=1 bash ./scripts/x.sh", "`bash ./scripts/x.sh`"),
        ("uv run --python '>=3.13' --no-project bin/x", "`uv run bin/x`"),
        ("uv run --extra dev pytest -q", "`uv run pytest`"),
        ("uv run -m tools.render", "`uv run -m tools.render`"),
        ("uv run python -m tools", "`uv run python -m tools`"),
    ],
)
def test_module_and_wrapper_forms_are_flagged(tmp_path, monkeypatch, command, label):
    """`python -m` imports from the working directory first, so a module the
    checkout holds runs the checkout's code; `env` and `uv run` only start it."""
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(ue, "REPO_ROOT", tmp_path)
    result = ue.check_file(_write(tmp_path, _wf(_PR_HEAD_SHA, _run_step(command))))
    assert len(result) == 1
    assert label in result[0][1]


@pytest.mark.parametrize(
    "command",
    [
        "python -m pip install x",
        "uv run --with ruff ruff check",
        "uvx ruff check",
        "python script_name_without_suffix -m tools",
    ],
)
def test_installed_modules_and_tools_are_not_counted(tmp_path, monkeypatch, command):
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(ue, "REPO_ROOT", tmp_path)
    path = _write(tmp_path, _wf(_PR_HEAD_SHA, _run_step(command)))
    assert ue.check_file(path) == []


def test_workflow_run_head_without_pinning_if_is_flagged(tmp_path):
    """A privileged follow-up workflow reaching the same head. Nothing pins WHICH
    upstream run may reach the job, so the checkout is attacker-controlled."""
    path = _write(
        tmp_path,
        "on:\n  workflow_run:\n    workflows: [ci]\n    types: [completed]\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.workflow_run.head_sha }}\n"
        + _run_step("pnpm build"),
    )
    assert len(ue.check_file(path)) == 1


def test_github_token_counts_when_permissions_grant_write(tmp_path):
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "permissions:\n  contents: write\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n"
        "      - run: pnpm build\n"
        "        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n",
    )
    result = ue.check_file(path)
    assert len(result) == 1
    assert "these secrets are live in the job: GITHUB_TOKEN" in result[0][1]


def test_github_token_is_not_a_secret_under_read_only_permissions(tmp_path):
    """A read-scoped default token is not a credential worth stealing; counting it
    would report every ordinary CI job."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "permissions:\n  contents: read\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n"
        "      - run: pnpm build\n"
        "        env:\n          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n",
    )
    assert ue.check_file(path) == []


def test_reported_line_is_the_offending_step(tmp_path):
    body = _wf(_PR_HEAD_SHA, _run_step("pnpm build"))
    # 1 opt-out, 2 `on:`, 3 trigger, 4 `jobs:`, 5 `build:`, 6 runs-on, 7 `steps:`,
    # 8-10 the checkout step, 11 the `- run: pnpm build` step.
    assert body.splitlines()[10] == "      - run: pnpm build"
    result = ue.check_file(_write(tmp_path, body))
    assert len(result) == 1
    assert result[0][0] == 11


# ── safe shapes (not flagged) ────────────────────────────────────────────────


def test_untrusted_checkout_and_secret_without_execution_form_is_clean(tmp_path):
    """Checking out the PR head next to a secret is check_trusted_base's business;
    with nothing executed out of the workspace this lint has no evidence."""
    path = _write(
        tmp_path,
        _wf(
            _PR_HEAD_SHA,
            "      - uses: actions/setup-node@8f152de45cc393bb48ce5d89d36b731f54556e65\n"
            "      - run: echo hi\n" + _SECRET_ENV,
        ),
    )
    assert ue.check_file(path) == []


def test_untrusted_checkout_and_execution_without_secret_is_clean(tmp_path):
    path = _write(tmp_path, _wf(_PR_HEAD_SHA, _run_step("pnpm build", env="")))
    assert ue.check_file(path) == []


@pytest.mark.parametrize(
    "ref",
    ["main", "${{ github.event.pull_request.base.ref }}"],
)
def test_trusted_checkout_ref_is_clean(tmp_path, ref):
    path = _write(tmp_path, _wf(ref, _run_step("pnpm build")))
    assert ue.check_file(path) == []


def test_default_checkout_without_with_ref_is_clean(tmp_path):
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n" + _run_step("pnpm build"),
    )
    assert ue.check_file(path) == []


def test_remote_sha_pinned_action_is_not_a_local_composite(tmp_path):
    path = _write(
        tmp_path,
        _wf(
            _PR_HEAD_SHA,
            "      - uses: owner/action@8f152de45cc393bb48ce5d89d36b731f54556e65\n"
            + _SECRET_ENV,
        ),
    )
    assert ue.check_file(path) == []


@pytest.mark.parametrize(
    "command",
    [
        'bash "${{ steps.staged.outputs.dir }}/x.sh"',
        'bash "$RUNNER_TEMP/x.sh"',
    ],
)
def test_expansion_anchored_run_is_not_an_execution_form(tmp_path, command):
    """A path opening with an expansion resolves somewhere this lint cannot know,
    so it is not counted (it is also not a rescue — see the other-form tests)."""
    path = _write(tmp_path, _wf(_PR_HEAD_SHA, _run_step(command)))
    assert ue.check_file(path) == []


@pytest.mark.parametrize(
    "command",
    ["pnpm install", "npm ci", "npm view pnpm version", "yarn install"],
)
def test_install_and_registry_subcommands_are_uncovered(tmp_path, command):
    """Deliberate precision trade-off: `install` is the single most common CI step,
    so flagging it would report the idiom rather than the defect."""
    path = _write(tmp_path, _wf(_PR_HEAD_SHA, _run_step(command)))
    assert ue.check_file(path) == []


def test_workflow_run_head_pinned_to_push_is_clean(tmp_path):
    """`workflow_run.event == 'push'` pins the upstream run to a commit on a branch
    the repo controls, so the head is trusted after all."""
    path = _write(
        tmp_path,
        "on:\n  workflow_run:\n    workflows: [ci]\n    types: [completed]\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    if: ${{ github.event.workflow_run.event == 'push' }}\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "        with:\n"
        "          ref: ${{ github.event.workflow_run.head_sha }}\n"
        + _run_step("pnpm build"),
    )
    assert ue.check_file(path) == []


# ── handoff with check_trusted_base (no duplicate finding) ───────────────────

_UNOPTED = _wf(_PR_HEAD_SHA, _run_step("pnpm build"), header="")


def test_job_already_reported_by_trusted_base_is_skipped(tmp_path):
    """One hole must not yield two findings, so the job check_trusted_base reports
    is this lint's to skip."""
    path = _write(tmp_path, _UNOPTED)
    assert len(tb.check_file(path)) == 1
    assert ue.check_file(path) == []


def test_trusted_base_optout_hands_the_job_to_this_lint(tmp_path):
    """The auditor-of-the-opt-out property: the same workflow, silenced for
    check_trusted_base by `# trusted-base-ok`, MUST surface here — that opt-out's
    usual claim is exactly what this lint tests."""
    path = _write(tmp_path, _TRUSTED_BASE_OK + _UNOPTED)
    assert tb.check_file(path) == []
    assert len(ue.check_file(path)) == 1


# ── opt-out (reason required, job-scoped) ────────────────────────────────────


def test_optout_with_reason_suppresses(tmp_path):
    path = _write(
        tmp_path,
        _wf(
            _PR_HEAD_SHA,
            "      # untrusted-exec-ok: the composite is vendored, not from the PR\n"
            + _run_step("pnpm build"),
        ),
    )
    assert ue.check_file(path) == []


def test_reasonless_optout_does_not_suppress(tmp_path):
    """A bare marker states nothing. The reason-required tail must not reach past
    the end of its own line to borrow the NEXT line's first character as a
    "reason" — that would silence this security lint with an empty claim."""
    path = _write(
        tmp_path,
        _wf(_PR_HEAD_SHA, "      # untrusted-exec-ok:\n" + _run_step("pnpm build")),
    )
    assert len(ue.check_file(path)) == 1


def test_optout_token_in_a_string_value_does_not_suppress(tmp_path):
    """A token smuggled into live data must never disable a lint — that is a
    fail-open."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        '    env:\n      NOTE: "untrusted-exec-ok: fake reason in a value"\n'
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n" + _run_step("pnpm build"),
    )
    assert len(ue.check_file(path)) == 1


def test_a_hash_inside_a_string_value_does_not_suppress(tmp_path):
    """A `#` inside a quoted scalar is CONTENT, not a comment. The job's author
    writes its `env:` values, so honouring one would switch this check off."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        '    env:\n      NOTE: "# untrusted-exec-ok: fake reason in a value"\n'
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n" + _run_step("pnpm build"),
    )
    assert len(ue.check_file(path)) == 1


def test_a_marker_in_a_run_script_suppresses(tmp_path):
    """A `run:` value is a shell script, where a `#` opens a real comment. The
    author writes the marker beside the command that runs the PR's code."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n"
        "      - run: |\n"
        "          # untrusted-exec-ok: the sandbox holds no secret\n"
        "          pnpm build\n",
    )
    assert ue.check_file(path) == []


def test_a_marker_in_a_quoted_one_line_run_suppresses(tmp_path):
    """The surface is the `run:` VALUE, not the `|` that may write it."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n"
        "      - run: 'pnpm build  # untrusted-exec-ok: the sandbox holds no secret'\n",
    )
    assert ue.check_file(path) == []


def test_a_marker_in_an_if_expression_does_not_suppress(tmp_path):
    """An `if:` body is an expression and never reaches a shell, so a `#` in it
    is content. Block style was the old test, and it read a marker out of this."""
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n"
        "    if: >\n"
        "      github.event_name == 'pull_request_target'\n"
        "      # untrusted-exec-ok: not a script\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n" + _run_step("pnpm build"),
    )
    assert len(ue.check_file(path)) == 1


def test_optout_in_a_sibling_job_does_not_suppress(tmp_path):
    path = _write(
        tmp_path,
        _TRUSTED_BASE_OK + "on:\n  pull_request_target:\n"
        "jobs:\n"
        "  other:\n    runs-on: ubuntu-latest\n"
        "    # untrusted-exec-ok: this job is not the one at risk\n"
        "    steps:\n      - run: echo hi\n"
        "  build:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {_PR_HEAD_SHA}\n" + _run_step("pnpm build"),
    )
    result = ue.check_file(path)
    assert len(result) == 1
    assert "job 'build'" in result[0][1]


# ── malformed / non-dict ─────────────────────────────────────────────────────


def test_malformed_yaml_is_reported_not_raised(tmp_path):
    path = _write(tmp_path, "on: [pull_request\njobs: {\n")
    result = ue.check_file(path)
    assert len(result) == 1
    assert result[0][0] is None
    assert "could not parse as YAML" in result[0][1]


def test_non_dict_yaml_is_ignored(tmp_path):
    path = _write(tmp_path, "- a\n- b\n", name="list.yaml")
    assert ue.check_file(path) == []


# ── main wiring ──────────────────────────────────────────────────────────────


def _workflow_tree(tmp_path: Path, monkeypatch, body: str) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "bad.yaml").write_text(body, encoding="utf-8")
    monkeypatch.setattr(ue, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ue, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(ue, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    return wf


def test_main_reports_and_returns_nonzero(tmp_path, monkeypatch, capsys):
    _workflow_tree(tmp_path, monkeypatch, _wf(_PR_HEAD_SHA, _run_step("pnpm build")))
    assert ue.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=11::" in out
    assert "executes code resolved from that checkout" in out
    assert "1 violation(s) found." in out


def test_main_clean_tree_returns_zero(tmp_path, monkeypatch, capsys):
    _workflow_tree(tmp_path, monkeypatch, _wf("main", _run_step("pnpm build")))
    assert ue.main() == 0
    assert capsys.readouterr().out == ""


# ── non-vacuity: each of the three rules must CONTRIBUTE ─────────────────────
#
# One violating fixture, mutated once per rule to remove exactly that rule's
# precondition. If a mutant still reports, the rule is not doing any work.

_VIOLATING = _wf(_PR_HEAD_SHA, _run_step("pnpm build"))


@pytest.mark.parametrize(
    ("mutation", "body"),
    [
        ("untrusted-checkout-removed", _wf("main", _run_step("pnpm build"))),
        (
            "live-secrets-removed",
            _wf(_PR_HEAD_SHA, _run_step("pnpm build", env="")),
        ),
        ("execution-form-removed", _wf(_PR_HEAD_SHA, _run_step("echo hi"))),
    ],
)
def test_each_rule_contributes(tmp_path, mutation, body):
    assert len(ue.check_file(_write(tmp_path, _VIOLATING, name="base.yaml"))) == 1, (
        f"base fixture must be flagged for the {mutation} mutant to mean anything"
    )
    assert ue.check_file(_write(tmp_path, body, name="mutant.yaml")) == [], mutation


# ── the merge commit: run before the job re-checks out a fixed branch ────────
#
# Under `pull_request`, a checkout with no `ref:` stages the PR's merge commit. A
# later checkout of the default branch says the job does not trust that tree, but
# a step that already ran from it could write $GITHUB_ENV, so the re-checkout is
# too late.

_DEFAULT_BRANCH = "${{ github.event.repository.default_branch }}"


def _merge_wf(
    first: str = "      - uses: actions/checkout@v4\n",
    early: str = "      - run: bash .github/scripts/comment.sh\n",
    recheckout_ref: str | None = _DEFAULT_BRANCH,
    trigger: str = "pull_request",
    secret_env: str = _SECRET_ENV,
) -> str:
    """A job that checks out FIRST, runs EARLY, re-checks out RECHECKOUT_REF, then
    runs a script with SECRET_ENV live."""
    recheckout = (
        "      - uses: actions/checkout@v4\n"
        f"        with:\n          ref: {recheckout_ref}\n"
        if recheckout_ref is not None
        else ""
    )
    return (
        f"on:\n  {trigger}:\n"
        "jobs:\n  delta:\n    runs-on: ubuntu-latest\n"
        "    steps:\n"
        + first
        + early
        + recheckout
        + "      - run: bash .github/scripts/analyze.sh\n"
        + secret_env
    )


@pytest.mark.parametrize("recheckout_ref", [_DEFAULT_BRANCH, "main"])
def test_execution_before_a_default_branch_recheckout_is_flagged(
    tmp_path, recheckout_ref
):
    result = ue.check_file(_write(tmp_path, _merge_wf(recheckout_ref=recheckout_ref)))
    assert len(result) == 1
    line, message = result[0]
    assert line == 8  # the step that runs comment.sh
    assert "job 'delta' stages the pull request's merge commit" in message
    assert "`bash .github/scripts/comment.sh`" in message
    assert "analyze.sh" not in message
    assert "these secrets are live in the job: NPM_TOKEN" in message


@pytest.mark.parametrize("ref", ["${{ github.sha }}", "${{ github.ref }}"])
def test_merge_ref_spellings_stage_the_merge_commit(tmp_path, ref):
    first = f"      - uses: actions/checkout@v4\n        with:\n          ref: {ref}\n"
    assert len(ue.check_file(_write(tmp_path, _merge_wf(first=first)))) == 1


def test_a_local_action_before_the_recheckout_is_flagged(tmp_path):
    early = "      - uses: ./.github/actions/setup\n"
    ((_line, message),) = ue.check_file(_write(tmp_path, _merge_wf(early=early)))
    assert "local composite action `uses: ./.github/actions/setup`" in message


def test_inline_reads_before_the_recheckout_are_clean(tmp_path):
    """The fixed shape: only inline commands touch the merge commit."""
    early = '      - run: git diff --name-only "$BASE"...HEAD\n'
    assert ue.check_file(_write(tmp_path, _merge_wf(early=early))) == []


def test_a_subdirectory_checkout_is_neither_untrusted_root_nor_rescue(tmp_path):
    in_subdir = "        with:\n          path: pr\n"
    first = "      - uses: actions/checkout@v4\n" + in_subdir
    assert ue.check_file(_write(tmp_path, _merge_wf(first=first))) == []
    body = _merge_wf().replace(
        f"          ref: {_DEFAULT_BRANCH}\n",
        f"          ref: {_DEFAULT_BRANCH}\n          path: trusted\n",
    )
    assert ue.check_file(_write(tmp_path, body, name="sub.yaml")) == []


def test_an_opaque_recheckout_ref_is_not_guessed_at(tmp_path):
    body = _merge_wf(recheckout_ref="${{ needs.pick.outputs.ref }}")
    assert ue.check_file(_write(tmp_path, body)) == []


_MERGE_VIOLATING = _merge_wf()


@pytest.mark.parametrize(
    ("mutation", "body"),
    [
        ("pull-request-trigger-removed", _merge_wf(trigger="pull_request_target")),
        ("live-secrets-removed", _merge_wf(secret_env="")),
        ("recheckout-removed", _merge_wf(recheckout_ref=None)),
        ("early-execution-removed", _merge_wf(early="      - run: echo hi\n")),
        (
            "first-checkout-pinned",
            _merge_wf(
                first="      - uses: actions/checkout@v4\n"
                "        with:\n          ref: main\n"
            ),
        ),
    ],
    ids=lambda value: value if "\n" not in value else "",
)
def test_each_merge_commit_rule_contributes(tmp_path, mutation, body):
    base = _write(tmp_path, _MERGE_VIOLATING, name="base.yaml")
    assert len(ue.check_file(base)) == 1, mutation
    assert ue.check_file(_write(tmp_path, body, name="mutant.yaml")) == [], mutation


@pytest.mark.parametrize(
    "token_step",
    ["", "        env:\n          GH_TOKEN: ${{ github.token }}\n"],
)
def test_a_write_scoped_token_counts_whether_or_not_it_is_named(tmp_path, token_step):
    """The runner receives the job's token for every job, so a write scope is in
    reach of the PR's code even when no step names the token."""
    body = _merge_wf(secret_env=token_step).replace(
        "jobs:\n", "permissions:\n  pull-requests: write\njobs:\n", 1
    )
    ((_line, message),) = ue.check_file(_write(tmp_path, body))
    assert "these secrets are live in the job: GITHUB_TOKEN" in message


def test_a_job_permissions_block_replaces_the_workflows(tmp_path):
    body = _merge_wf(secret_env="").replace(
        "jobs:\n  delta:\n",
        "permissions: write-all\njobs:\n  delta:\n"
        "    permissions:\n      contents: read\n",
        1,
    )
    assert ue.check_file(_write(tmp_path, body)) == []

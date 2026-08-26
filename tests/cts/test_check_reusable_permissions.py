"""Tests for ci_truth_serum/check_reusable_permissions.py — the lint requiring a
job that calls a local reusable workflow to grant every permission that workflow
asks for (a callee requesting more than its caller holds fails the whole run at
start, and the error names the callee, not the caller).

Two layers: unit tests of the grant algebra (parse, compare, union, the
requirement walk) and file-level tests driving `check_file` / `main` over real
workflow trees in tmp dirs, with the module's discovery constants redirected so
the real repo never leaks in.
"""

import textwrap
from pathlib import Path

from tests._helpers import load_hook

crp = load_hook("check_reusable_permissions.py", "check_reusable_permissions")


# ── grant: what a `permissions:` value states ────────────────────────────
def test_a_mapping_grants_only_the_scopes_it_lists():
    held = crp.grant({"contents": "read", "issues": "write"}, crp.CALLER_UNKNOWN)
    assert held == (0, {"contents": 1, "issues": 2})
    # An unlisted scope is `none`, not "unchanged" — the baseline says so.
    assert crp.level(held, "pull-requests") == 0


def test_the_line_tag_the_loader_adds_is_not_a_scope():
    """LineLoader tags every mapping with `__line__`; reading it as a scope would
    invent a permission no workflow declares."""
    assert crp.grant({"__line__": 7, "contents": "read"}, crp.CALLER_UNKNOWN) == (
        0,
        {"contents": 1},
    )


def test_the_all_keywords_set_the_baseline():
    assert crp.grant("write-all", crp.CALLER_UNKNOWN) == (2, {})
    assert crp.grant("read-all", crp.CALLER_UNKNOWN) == (1, {})
    assert crp.level(crp.grant("read-all", crp.CALLER_UNKNOWN), "issues") == 1


def test_an_absent_key_states_nothing_so_the_holder_inherits():
    assert crp.grant(None, crp.CALLER_UNKNOWN) is None


def test_an_unreadable_value_takes_the_strictest_reading_of_its_side():
    """A typo must fail closed on both sides: the caller grants nothing, and the
    callee needs write, so the comparison can never pass on an unread value."""
    for value in ("all", "read", 42, ["contents"]):
        assert crp.grant(value, crp.CALLER_UNKNOWN) == (0, {}), value
        assert crp.grant(value, crp.CALLEE_UNKNOWN) == (2, {}), value
    assert crp.grant({"contents": "raed"}, crp.CALLER_UNKNOWN) == (0, {"contents": 0})
    assert crp.grant({"contents": "raed"}, crp.CALLEE_UNKNOWN) == (0, {"contents": 2})


def test_union_takes_the_higher_level_of_each_scope():
    first = (0, {"contents": 1, "issues": 2})
    second = (1, {"contents": 2})
    assert crp.union(first, second) == (1, {"contents": 2, "issues": 2})


# ── local_callee ─────────────────────────────────────────────────────────
def test_local_callee_reads_a_relative_workflow_path():
    assert (
        crp.local_callee({"uses": "./.github/workflows/x.yaml"})
        == ".github/workflows/x.yaml"
    )
    assert crp.local_callee({"uses": "./.github/workflows/x.yml"}) is not None


def test_a_callee_in_another_repository_is_skipped():
    """Its permissions block is not in this tree, so there is nothing to compare."""
    assert crp.local_callee({"uses": "org/repo/.github/workflows/x.yaml@v1"}) is None
    assert crp.local_callee({"uses": "actions/checkout@v4"}) is None
    assert crp.local_callee({"uses": "./.github/actions/setup"}) is None
    assert crp.local_callee("not-a-job") is None


# ── shortfalls ───────────────────────────────────────────────────────────
def test_a_caller_that_holds_the_scope_has_no_shortfall():
    assert crp.shortfalls((0, {"contents": 1}), (0, {"contents": 2})) == []


def test_a_needed_scope_at_none_asks_for_nothing():
    assert crp.shortfalls((0, {"contents": 0}), (0, {})) == []


def test_a_caller_with_no_block_falls_short_of_every_needed_scope():
    missing = crp.shortfalls((0, {"contents": 1, "issues": 2}), None)
    assert len(missing) == 2
    assert "no declared level" in missing[0]


def test_a_baseline_requirement_needs_the_same_baseline():
    assert crp.shortfalls((1, {}), (0, {"contents": 1})) == [
        "`read-all` on every other scope (this caller: none-all)"
    ]
    assert crp.shortfalls((1, {}), (2, {})) == []


# ── fixture machinery ────────────────────────────────────────────────────
CALLEE = textwrap.dedent(
    """\
    name: decide
    on:
      workflow_call:
    permissions:
      contents: read
      pull-requests: read
    jobs:
      decide:
        runs-on: ubuntu-latest
        steps: []
    """
)


def _caller(job_permissions: str = "", workflow_permissions: str = "") -> str:
    return (
        "name: x\non:\n  pull_request:\n"
        + workflow_permissions
        + "jobs:\n  decide:\n"
        + job_permissions
        + "    uses: ./.github/workflows/callee.yaml\n"
    )


JOB_GRANT = "    permissions:\n      contents: read\n      pull-requests: read\n"
JOB_SHORT = "    permissions:\n      contents: read\n"
WORKFLOW_GRANT = "permissions:\n  contents: read\n  pull-requests: read\n"


def _tree(tmp_path: Path, monkeypatch, files: dict[str, str]) -> Path:
    """A repo root holding FILES (repo-relative), with the module's discovery
    constants pointed at it."""
    root = tmp_path / "repo"
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    monkeypatch.setattr(crp, "REPO_ROOT", root)
    monkeypatch.setattr(crp, "WORKFLOWS_DIR", root / ".github" / "workflows")
    return root


def _check(tmp_path, monkeypatch, caller: str, extra: dict[str, str] | None = None):
    files = {
        ".github/workflows/caller.yaml": caller,
        ".github/workflows/callee.yaml": CALLEE,
        **(extra or {}),
    }
    root = _tree(tmp_path, monkeypatch, files)
    return crp.check_file(root / ".github" / "workflows" / "caller.yaml")


# ── the violating shapes ─────────────────────────────────────────────────
def test_a_job_granting_less_than_the_callee_needs_is_an_error(tmp_path, monkeypatch):
    found = _check(tmp_path, monkeypatch, _caller(JOB_SHORT))
    assert len(found) == 1
    line, message = found[0]
    assert line is not None
    assert "`pull-requests: read`" in message
    assert "this caller: none" in message


def test_a_caller_with_no_permissions_block_at_all_is_an_error(tmp_path, monkeypatch):
    """The token then carries the repository default, which the file does not
    state and an administrator can change without touching the workflow."""
    found = _check(tmp_path, monkeypatch, _caller())
    assert len(found) == 1
    assert "repository default" in found[0][1]
    assert "no declared level" in found[0][1]


def test_a_read_only_caller_falls_short_of_a_write_callee(tmp_path, monkeypatch):
    callee = CALLEE.replace("pull-requests: read", "pull-requests: write")
    found = _check(
        tmp_path,
        monkeypatch,
        _caller(JOB_GRANT),
        {".github/workflows/callee.yaml": callee},
    )
    assert len(found) == 1
    assert "`pull-requests: write` (this caller: read)" in found[0][1]


def test_a_job_level_block_overrides_a_sufficient_workflow_level_one(
    tmp_path, monkeypatch
):
    """A job block replaces the workflow block; it does not add to it."""
    found = _check(tmp_path, monkeypatch, _caller(JOB_SHORT, WORKFLOW_GRANT))
    assert len(found) == 1
    assert "`pull-requests: read`" in found[0][1]


def test_a_requirement_reached_through_a_second_hop_is_reported(tmp_path, monkeypatch):
    """A middle workflow that declares nothing passes its caller's token straight
    down, so the deep requirement lands on the top caller."""
    middle = (
        "name: middle\non:\n  workflow_call:\njobs:\n"
        "  pass:\n    uses: ./.github/workflows/callee.yaml\n"
    )
    caller = _caller(JOB_SHORT).replace("callee.yaml", "middle.yaml")
    found = _check(
        tmp_path, monkeypatch, caller, {".github/workflows/middle.yaml": middle}
    )
    assert len(found) == 1
    assert "`pull-requests: read`" in found[0][1]


def test_a_missing_local_callee_is_its_own_hard_error(tmp_path, monkeypatch):
    caller = _caller(JOB_GRANT).replace("callee.yaml", "gone.yaml")
    found = _check(tmp_path, monkeypatch, caller)
    assert len(found) == 1
    assert "not a file in this repository" in found[0][1]


def test_an_unreadable_caller_value_grants_nothing(tmp_path, monkeypatch):
    """The value is a block, so the job does not inherit; nothing about it says
    the scope is held, so it must not satisfy the comparison."""
    found = _check(tmp_path, monkeypatch, _caller("    permissions: all\n"))
    assert len(found) == 1
    assert "this caller: none" in found[0][1]


def test_a_callee_baseline_keyword_demands_the_same_baseline(tmp_path, monkeypatch):
    callee = "name: c\non:\n  workflow_call:\npermissions: write-all\njobs:\n  a:\n    steps: []\n"
    found = _check(
        tmp_path,
        monkeypatch,
        _caller(JOB_GRANT),
        {".github/workflows/callee.yaml": callee},
    )
    assert len(found) == 1
    assert "`write-all` on every other scope" in found[0][1]


# ── the clean shapes (false-positive guards) ─────────────────────────────
def test_a_job_granting_exactly_what_the_callee_needs_is_clean(tmp_path, monkeypatch):
    assert _check(tmp_path, monkeypatch, _caller(JOB_GRANT)) == []


def test_a_sufficient_workflow_level_block_covers_a_job_that_declares_none(
    tmp_path, monkeypatch
):
    assert _check(tmp_path, monkeypatch, _caller("", WORKFLOW_GRANT)) == []


def test_write_all_covers_every_scope(tmp_path, monkeypatch):
    assert _check(tmp_path, monkeypatch, _caller("", "permissions: write-all\n")) == []


def test_a_callee_that_declares_no_permissions_needs_nothing(tmp_path, monkeypatch):
    """It runs on whatever token it is handed, so a caller with no block is fine."""
    callee = (
        "name: c\non:\n  workflow_call:\njobs:\n  a:\n    runs-on: x\n    steps: []\n"
    )
    assert (
        _check(
            tmp_path,
            monkeypatch,
            _caller(),
            {".github/workflows/callee.yaml": callee},
        )
        == []
    )


def test_a_callee_in_another_repository_is_not_compared(tmp_path, monkeypatch):
    caller = (
        "name: x\non:\n  pull_request:\njobs:\n  decide:\n"
        "    uses: org/repo/.github/workflows/x.yaml@v1\n"
    )
    assert _check(tmp_path, monkeypatch, caller) == []


def test_a_job_that_calls_nothing_is_not_compared(tmp_path, monkeypatch):
    caller = "name: x\non:\n  pull_request:\njobs:\n  build:\n    runs-on: x\n    steps: []\n"
    assert _check(tmp_path, monkeypatch, caller) == []


def test_a_callee_job_scope_below_the_workflow_block_does_not_raise_the_need(
    tmp_path, monkeypatch
):
    """A job block replaces the workflow block in the callee too, so a job that
    lowers a scope asks for less, not more."""
    callee = (
        "name: c\non:\n  workflow_call:\npermissions:\n  contents: write\n"
        "jobs:\n  a:\n    permissions:\n      contents: read\n    steps: []\n"
    )
    found = _check(
        tmp_path,
        monkeypatch,
        _caller("    permissions:\n      contents: read\n"),
        {".github/workflows/callee.yaml": callee},
    )
    assert found == []


def test_a_call_cycle_ends_the_walk(tmp_path, monkeypatch):
    """GitHub rejects the cycle itself; this lint must not recurse forever on it."""
    callee = (
        "name: c\non:\n  workflow_call:\njobs:\n"
        "  again:\n    uses: ./.github/workflows/callee.yaml\n"
    )
    assert (
        _check(
            tmp_path,
            monkeypatch,
            _caller(JOB_GRANT),
            {".github/workflows/callee.yaml": callee},
        )
        == []
    )


# ── opt-out ──────────────────────────────────────────────────────────────
def test_a_reasoned_opt_out_suppresses_the_finding(tmp_path, monkeypatch):
    caller = _caller(f"    # {crp.OPT_OUT}: the callee scope is unused on this event\n")
    assert _check(tmp_path, monkeypatch, caller) == []


def test_an_opt_out_with_no_reason_suppresses_nothing_and_is_reported(
    tmp_path, monkeypatch
):
    caller = _caller(f"    # {crp.OPT_OUT}: todo\n" + JOB_SHORT)
    messages = [message for _line, message in _check(tmp_path, monkeypatch, caller)]
    assert len(messages) == 2
    assert any("must say why" in m for m in messages)
    assert any("`pull-requests: read`" in m for m in messages)


def test_the_marker_inside_a_run_body_suppresses_nothing(tmp_path, monkeypatch):
    """A `#` line in a `run:` body is text the shell reads, not a classification
    of the job — matching it anywhere in the byte stream would be a fail-open."""
    caller = (
        "name: x\non:\n  pull_request:\njobs:\n  decide:\n"
        + JOB_SHORT
        + "    uses: ./.github/workflows/callee.yaml\n"
        "  build:\n    runs-on: x\n    steps:\n"
        f"      - run: |\n          # {crp.OPT_OUT}: printed, not declared\n"
    )
    found = _check(tmp_path, monkeypatch, caller)
    assert len(found) == 1
    assert "`pull-requests: read`" in found[0][1]


def test_a_longer_slug_containing_the_token_suppresses_nothing(tmp_path, monkeypatch):
    caller = _caller(f"    # not-{crp.OPT_OUT}: a different annotation\n" + JOB_SHORT)
    found = _check(tmp_path, monkeypatch, caller)
    assert len(found) == 1
    assert "`pull-requests: read`" in found[0][1]


def test_an_opt_out_in_another_job_does_not_reach_this_one(tmp_path, monkeypatch):
    caller = (
        "name: x\non:\n  pull_request:\njobs:\n"
        f"  other:  # {crp.OPT_OUT}: a real reason\n    runs-on: x\n    steps: []\n"
        "  decide:\n" + JOB_SHORT + "    uses: ./.github/workflows/callee.yaml\n"
    )
    found = _check(tmp_path, monkeypatch, caller)
    assert len(found) == 1
    assert "`pull-requests: read`" in found[0][1]


# ── parse failures and non-workflow documents ────────────────────────────
def test_unparseable_yaml_is_reported_rather_than_passed_as_clean(
    tmp_path, monkeypatch
):
    found = _check(tmp_path, monkeypatch, "jobs:\n  a: [\n   unbalanced\n")
    assert len(found) == 1
    line, message = found[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_a_document_with_no_jobs_yields_nothing(tmp_path, monkeypatch):
    for body in ("- a\n- list\n", "just a scalar\n", "jobs: null\n", "name: x\n"):
        assert _check(tmp_path, monkeypatch, body) == [], body


def test_a_job_that_is_not_a_mapping_is_skipped(tmp_path, monkeypatch):
    assert _check(tmp_path, monkeypatch, "jobs:\n  a: 'string'\n  b: null\n") == []


# ── main ─────────────────────────────────────────────────────────────────
def test_main_annotates_each_violation_and_exits_one(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            ".github/workflows/caller.yaml": _caller(JOB_SHORT),
            ".github/workflows/callee.yaml": CALLEE,
        },
    )
    assert crp.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/caller.yaml,line=" in out
    assert "1 reusable-call permission violation(s) found." in out


def test_main_is_clean_when_every_caller_grants_enough(tmp_path, monkeypatch, capsys):
    _tree(
        tmp_path,
        monkeypatch,
        {
            ".github/workflows/caller.yaml": _caller(JOB_GRANT),
            ".github/workflows/callee.yaml": CALLEE,
        },
    )
    assert crp.main() == 0
    assert "::error" not in capsys.readouterr().out


def test_main_says_so_over_a_tree_with_no_workflow(tmp_path, monkeypatch, capsys):
    """Exit 0 is honest — no workflow, nothing to violate — so the note is what
    tells a caller that apart from a real pass."""
    _tree(tmp_path, monkeypatch, {"README.md": "x\n"})
    assert crp.main() == 0
    assert "scanned nothing" in capsys.readouterr().err

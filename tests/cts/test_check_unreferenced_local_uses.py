"""Tests for ci_truth_serum/check_unreferenced_local_uses.py — the lint reporting
a local composite action, or a `workflow_call`-only workflow, that no `uses:`
reaches.

Two layers: unit tests of the readers (`uses_values`, `referenced_path`,
`calls_only`, `key_lines`) and tree-level tests driving `check_repo` / `main`
over real trees in tmp dirs, with the module's discovery constants redirected so
the real repo never leaks in.
"""

from pathlib import Path

from tests._helpers import load_hook

ulu = load_hook("check_unreferenced_local_uses.py", "check_unreferenced_local_uses")


# ── uses_values ──────────────────────────────────────────────────────────
def test_all_three_places_a_uses_can_sit_are_read():
    """A job's own `uses:` calls a workflow, a step's calls an action, and an
    action's `runs.steps` calls another action — all three are real edges."""
    doc = {
        "jobs": {
            "gate": {"uses": "./.github/workflows/c.yaml"},
            "work": {"steps": [{"uses": "./.github/actions/a"}, {"run": "x"}]},
        },
        "runs": {"steps": [{"uses": "./.github/actions/b"}]},
    }
    assert sorted(ulu.uses_values(doc)) == [
        "./.github/actions/a",
        "./.github/actions/b",
        "./.github/workflows/c.yaml",
    ]


def test_a_document_with_no_uses_yields_nothing():
    for doc in (
        None,
        "scalar",
        {"jobs": None},
        {"jobs": {"a": "not-a-mapping"}},
        {"jobs": {"a": {"steps": "not-a-list"}}},
        {"runs": {"using": "composite"}},
        {"jobs": {"__line__": 3, "a": {}}},
    ):
        assert ulu.uses_values(doc) == [], doc


# ── referenced_path ──────────────────────────────────────────────────────
def test_a_local_uses_names_its_repo_relative_path():
    assert ulu.referenced_path("./.github/actions/a") == ".github/actions/a"
    assert ulu.referenced_path("  ./.github/actions/a/  ") == ".github/actions/a"
    assert ulu.referenced_path("./.github/workflows/c.yml") == ".github/workflows/c.yml"


def test_an_owner_qualified_path_counts_as_its_path_half():
    """A repository may call its own reusable workflow by full path. Offline
    that cannot be told from a foreign repository of the same shape, so the
    path half counts either way — an undercount never invents a finding."""
    assert (
        ulu.referenced_path("org/repo/.github/workflows/c.yaml@v1")
        == ".github/workflows/c.yaml"
    )


def test_a_published_action_names_no_path_in_this_tree():
    assert ulu.referenced_path("actions/checkout@v4") is None
    assert ulu.referenced_path("actions/checkout") is None
    assert ulu.referenced_path("org/repo") is None


def test_an_expression_uses_resolves_to_nothing_this_check_can_read():
    assert ulu.referenced_path("./.github/actions/${{ inputs.name }}") is None


# ── calls_only ───────────────────────────────────────────────────────────
def test_every_shape_of_a_sole_workflow_call_trigger_is_read():
    """`on:` is YAML 1.1 true, so reading only the string key would see no
    trigger at all and pass every reusable workflow as reachable."""
    for triggers in ({"workflow_call": None}, ["workflow_call"], "workflow_call"):
        assert ulu.calls_only({True: triggers}) is True, triggers
        assert ulu.calls_only({"on": triggers}) is True, triggers


def test_the_line_tag_the_loader_adds_is_not_a_second_trigger():
    assert ulu.calls_only({"on": {"__line__": 2, "workflow_call": None}}) is True


def test_a_second_trigger_makes_the_workflow_reachable_without_a_caller():
    for triggers in (
        {"workflow_call": None, "workflow_dispatch": None},
        ["workflow_call", "push"],
        "push",
        None,
    ):
        assert ulu.calls_only({"on": triggers}) is False, triggers


# ── key_lines ────────────────────────────────────────────────────────────
KEYED = "name: c\non:\n  workflow_call:\n    inputs: {}\njobs: {}\n"


def test_key_lines_walks_the_path_and_reports_each_key():
    assert ulu.key_lines(KEYED, "on", "workflow_call") == [2, 3]
    assert ulu.key_lines(KEYED, "name") == [1]


def test_a_path_that_stops_early_returns_the_keys_it_reached():
    """`on: workflow_call` has no nested mapping, so the outer key is the only
    anchor there is — and it is where a reason goes."""
    assert ulu.key_lines("on: workflow_call\n", "on", "workflow_call") == [1]
    assert ulu.key_lines(KEYED, "on", "nope") == [2]
    assert ulu.key_lines(KEYED, "nope") == []


def test_unparseable_text_has_no_key_lines():
    assert ulu.key_lines("jobs:\n  a: [\n", "jobs") == []


# ── fixture machinery ────────────────────────────────────────────────────
ACTION = "name: a\nruns:\n  using: composite\n  steps:\n    - run: 'x'\n"
CALLEE = "name: c\non:\n  workflow_call:\njobs:\n  x:\n    runs-on: ubuntu-latest\n"


def _caller(uses: str) -> str:
    return (
        "name: x\non:\n  pull_request:\njobs:\n  work:\n"
        "    runs-on: ubuntu-latest\n    steps:\n"
        f"      - uses: {uses}\n"
    )


def _job_caller(uses: str) -> str:
    return f"name: x\non:\n  pull_request:\njobs:\n  gate:\n    uses: {uses}\n"


def _root(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    return root


def _check(tmp_path, monkeypatch, files: dict[str, str]):
    root = _root(tmp_path, files)
    monkeypatch.setattr(ulu, "REPO_ROOT", root)
    monkeypatch.setattr(ulu, "WORKFLOWS_DIR", root / ".github" / "workflows")
    monkeypatch.setattr(ulu, "ACTIONS_DIR", root / ".github" / "actions")
    return ulu.check_repo(ulu.WORKFLOWS_DIR, ulu.ACTIONS_DIR)


# ── the violating shapes ─────────────────────────────────────────────────
def test_an_action_no_step_uses_is_reported(tmp_path, monkeypatch):
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/actions/orphan/action.yaml": ACTION,
            ".github/workflows/w.yaml": _caller("actions/checkout@v4"),
        },
    )
    assert len(found) == 1
    path, line, message = found[0]
    assert path.name == "action.yaml"
    assert line == 1
    assert "no workflow or action in this repository uses" in message
    assert "`uses: ./.github/actions/orphan`" in message


def test_a_workflow_call_only_workflow_no_job_calls_is_reported(tmp_path, monkeypatch):
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/workflows/callee.yaml": CALLEE,
            ".github/workflows/w.yaml": _caller("actions/checkout@v4"),
        },
    )
    assert len(found) == 1
    path, line, message = found[0]
    assert path.name == "callee.yaml"
    assert line == 3  # the `workflow_call:` key, not the `on:` above it
    assert "declares `workflow_call` as its only trigger" in message


def test_the_scalar_and_list_trigger_forms_are_reported_too(tmp_path, monkeypatch):
    for triggers in ("on: workflow_call\n", "on: [workflow_call]\n"):
        found = _check(
            tmp_path,
            monkeypatch,
            {".github/workflows/callee.yaml": "name: c\n" + triggers + "jobs: {}\n"},
        )
        assert [f.line for f in found] == [2], triggers


def test_a_uses_written_in_a_run_script_reaches_nothing(tmp_path, monkeypatch):
    """The reference is read from the parsed `uses:` key. The same words inside
    a script are text a shell prints, and they start no action."""
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/actions/orphan/action.yaml": ACTION,
            ".github/workflows/w.yaml": "name: x\non:\n  push:\njobs:\n  a:\n"
            "    runs-on: ubuntu-latest\n    steps:\n"
            "      - run: 'echo uses: ./.github/actions/orphan'\n",
        },
    )
    assert [f.path.name for f in found] == ["action.yaml"]


def test_a_uses_written_in_a_comment_reaches_nothing(tmp_path, monkeypatch):
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/actions/orphan/action.yaml": ACTION,
            ".github/workflows/w.yaml": "# uses: ./.github/actions/orphan\n"
            + _caller("actions/checkout@v4"),
        },
    )
    assert [f.path.name for f in found] == ["action.yaml"]


def test_a_workflow_named_action_yaml_is_not_read_as_an_action(tmp_path, monkeypatch):
    """The location says what a file is, never the basename. Reading this name
    as an action would report the whole workflows directory as one."""
    found = _check(
        tmp_path,
        monkeypatch,
        {".github/workflows/action.yaml": _caller("actions/checkout@v4")},
    )
    assert found == []


def test_a_definition_with_no_anchor_can_still_carry_the_opt_out(tmp_path, monkeypatch):
    """Every check here owes an escape hatch. A file whose keys the composed
    tree cannot name would otherwise have nowhere to write one."""
    action = f"# {ulu.OPT_OUT}: the deploy repo uses this action\nruns: {{}}\n"
    assert (
        _check(tmp_path, monkeypatch, {".github/actions/x/action.yaml": action}) == []
    )


def test_a_definition_with_no_key_to_anchor_on_is_reported_at_the_file(
    tmp_path, monkeypatch
):
    """An action file with no `name:` gives the composed tree no key to answer
    with. Line 0 reports the finding at the file, which a file with no such
    line still has."""
    found = _check(
        tmp_path, monkeypatch, {".github/actions/x/action.yaml": "runs: {}\n"}
    )
    assert [(f.path.name, f.line) for f in found] == [("action.yaml", 0)]
    assert "no workflow or action in this repository uses" in found[0].message


def test_two_dead_definitions_are_both_reported(tmp_path, monkeypatch):
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/actions/orphan/action.yaml": ACTION,
            ".github/workflows/callee.yaml": CALLEE,
        },
    )
    assert sorted(f.path.name for f in found) == ["action.yaml", "callee.yaml"]


# ── the clean shapes (false-positive guards) ─────────────────────────────
def test_an_action_a_workflow_step_uses_is_clean(tmp_path, monkeypatch):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/actions/used/action.yaml": ACTION,
                ".github/workflows/w.yaml": _caller("./.github/actions/used"),
            },
        )
        == []
    )


def test_a_trailing_slash_on_the_reference_still_resolves(tmp_path, monkeypatch):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/actions/used/action.yaml": ACTION,
                ".github/workflows/w.yaml": _caller("./.github/actions/used/"),
            },
        )
        == []
    )


def test_an_action_only_another_action_uses_is_clean(tmp_path, monkeypatch):
    """A `uses: ./…` inside an action resolves against the caller's workspace,
    so an action reached only from another action is live."""
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/actions/inner/action.yaml": ACTION,
            ".github/actions/outer/action.yaml": "name: o\nruns:\n  using: composite\n"
            "  steps:\n    - uses: ./.github/actions/inner\n",
            ".github/workflows/w.yaml": _caller("./.github/actions/outer"),
        },
    )
    assert found == []


def test_a_workflow_a_job_calls_is_clean(tmp_path, monkeypatch):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/workflows/callee.yaml": CALLEE,
                ".github/workflows/w.yaml": _job_caller(
                    "./.github/workflows/callee.yaml"
                ),
            },
        )
        == []
    )


def test_a_workflow_this_repo_calls_by_full_path_is_clean(tmp_path, monkeypatch):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/workflows/callee.yaml": CALLEE,
                ".github/workflows/w.yaml": _job_caller(
                    "org/repo/.github/workflows/callee.yaml@v1"
                ),
            },
        )
        == []
    )


def test_a_workflow_with_a_second_trigger_needs_no_caller(tmp_path, monkeypatch):
    """`workflow_dispatch` starts it without a caller, so an absent caller says
    nothing about whether it runs."""
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/workflows/callee.yaml": "name: c\non:\n  workflow_call:\n"
                "  workflow_dispatch:\njobs: {}\n"
            },
        )
        == []
    )


def test_a_workflow_that_declares_no_workflow_call_is_not_examined(
    tmp_path, monkeypatch
):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {".github/workflows/w.yaml": _caller("actions/checkout@v4")},
        )
        == []
    )


def test_the_yml_spelling_is_read_on_both_sides(tmp_path, monkeypatch):
    assert (
        _check(
            tmp_path,
            monkeypatch,
            {
                ".github/actions/used/action.yml": ACTION,
                ".github/workflows/callee.yml": CALLEE,
                ".github/workflows/w.yml": _job_caller("./.github/workflows/callee.yml")
                + "  work:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - uses: ./.github/actions/used\n",
            },
        )
        == []
    )


# ── opt-out ──────────────────────────────────────────────────────────────
def test_a_reasoned_opt_out_on_the_key_line_suppresses_the_finding(
    tmp_path, monkeypatch
):
    action = f"name: a  # {ulu.OPT_OUT}: the deploy repo uses this action\nruns: {{}}\n"
    assert (
        _check(tmp_path, monkeypatch, {".github/actions/x/action.yaml": action}) == []
    )


def test_a_reasoned_opt_out_above_the_workflow_call_key_suppresses(
    tmp_path, monkeypatch
):
    callee = (
        "name: c\non:\n"
        f"  # {ulu.OPT_OUT}: the sibling repository calls this workflow\n"
        "  workflow_call:\njobs: {}\n"
    )
    assert _check(tmp_path, monkeypatch, {".github/workflows/c.yaml": callee}) == []


def test_a_reasoned_opt_out_above_the_on_key_suppresses(tmp_path, monkeypatch):
    """Both anchors are asked, so a reason written above `on:` reads the same as
    one written above `workflow_call:`."""
    callee = (
        "name: c\n"
        f"# {ulu.OPT_OUT}: the sibling repository calls this workflow\n"
        "on:\n  workflow_call:\njobs: {}\n"
    )
    assert _check(tmp_path, monkeypatch, {".github/workflows/c.yaml": callee}) == []


def test_an_opt_out_with_no_reason_suppresses_nothing_and_is_reported(
    tmp_path, monkeypatch
):
    action = f"name: a  # {ulu.OPT_OUT}: todo\nruns: {{}}\n"
    found = _check(tmp_path, monkeypatch, {".github/actions/x/action.yaml": action})
    assert len(found) == 1
    assert "states only 'todo'" in found[0].message


def test_a_longer_slug_containing_the_token_suppresses_nothing(tmp_path, monkeypatch):
    action = f"name: a  # not-{ulu.OPT_OUT}: a different annotation\nruns: {{}}\n"
    found = _check(tmp_path, monkeypatch, {".github/actions/x/action.yaml": action})
    assert len(found) == 1
    assert "no workflow or action in this repository uses" in found[0].message


def test_an_opt_out_elsewhere_in_the_file_does_not_reach_the_definition(
    tmp_path, monkeypatch
):
    """A marker matched anywhere in the byte stream would let a reason written
    about one thing suppress a finding about another."""
    callee = (
        "name: c\non:\n  workflow_call:\njobs:\n"
        f"  x:  # {ulu.OPT_OUT}: a real reason, about the job\n"
        "    runs-on: ubuntu-latest\n"
    )
    found = _check(tmp_path, monkeypatch, {".github/workflows/c.yaml": callee})
    assert len(found) == 1
    assert "declares `workflow_call` as its only trigger" in found[0].message


# ── parse failures ───────────────────────────────────────────────────────
def test_an_unparseable_file_is_reported_rather_than_passed_as_clean(
    tmp_path, monkeypatch
):
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/workflows/broken.yaml": "jobs:\n  a: [\n   unbalanced\n",
            ".github/workflows/w.yaml": _caller("actions/checkout@v4"),
        },
    )
    assert [(f.path.name, f.line) for f in found] == [("broken.yaml", 0)]
    assert "could not parse as YAML" in found[0].message


def test_an_unparseable_file_withholds_every_unreferenced_verdict(
    tmp_path, monkeypatch
):
    """Its `uses:` values cannot be read, so each definition it reaches would
    read as dead. One syntax error would cascade into a finding per action."""
    found = _check(
        tmp_path,
        monkeypatch,
        {
            ".github/workflows/broken.yaml": "jobs:\n  a: [\n   unbalanced\n",
            ".github/actions/orphan/action.yaml": ACTION,
            ".github/workflows/callee.yaml": CALLEE,
        },
    )
    assert [f.path.name for f in found] == ["broken.yaml"]
    assert "reports no unreferenced definition at all" in found[0].message


def test_an_unparseable_action_is_reported_too(tmp_path, monkeypatch):
    found = _check(
        tmp_path,
        monkeypatch,
        {".github/actions/x/action.yaml": "runs:\n  steps: [\n"},
    )
    assert [f.path.name for f in found] == ["action.yaml"]
    assert "could not parse as YAML" in found[0].message


# ── main ─────────────────────────────────────────────────────────────────
def _main(tmp_path, monkeypatch, files: dict[str, str]) -> int:
    root = _root(tmp_path, files)
    monkeypatch.setattr(ulu, "REPO_ROOT", root)
    monkeypatch.setattr(ulu, "WORKFLOWS_DIR", root / ".github" / "workflows")
    monkeypatch.setattr(ulu, "ACTIONS_DIR", root / ".github" / "actions")
    return ulu.main()


def test_main_annotates_each_violation_and_exits_one(tmp_path, monkeypatch, capsys):
    assert _main(tmp_path, monkeypatch, {".github/workflows/c.yaml": CALLEE}) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/c.yaml,line=3::" in out
    assert "1 unreferenced local definition(s) found." in out


def test_main_is_clean_when_every_definition_has_a_reference(
    tmp_path, monkeypatch, capsys
):
    assert (
        _main(
            tmp_path,
            monkeypatch,
            {
                ".github/workflows/callee.yaml": CALLEE,
                ".github/workflows/w.yaml": _job_caller(
                    "./.github/workflows/callee.yaml"
                ),
            },
        )
        == 0
    )
    assert "::error" not in capsys.readouterr().out


def test_main_says_so_over_a_tree_with_no_workflow(tmp_path, monkeypatch, capsys):
    """Exit 0 is honest — no workflow, nothing to violate — so the note is what
    tells a caller that apart from a real pass."""
    assert _main(tmp_path, monkeypatch, {"README.md": "x\n"}) == 0
    assert "scanned nothing" in capsys.readouterr().err

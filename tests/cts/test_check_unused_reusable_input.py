"""Tests for ci_truth_serum/check_unused_reusable_input.py — the lint reporting a
`workflow_call` input that no local caller passes.

Two layers: unit tests of the readers (`call_inputs`, `local_callee`, `calls`,
`marker_window`) and tree-level tests driving `check_repo` / `main` over real
workflow trees in tmp dirs, with the module's discovery constants redirected so
the real repo never leaks in.
"""

from pathlib import Path

from tests._helpers import load_hook

uri = load_hook("check_unused_reusable_input.py", "check_unused_reusable_input")


# ── call_inputs ──────────────────────────────────────────────────────────
def test_the_boolean_key_pyyaml_resolves_on_to_is_read():
    """`on:` is YAML 1.1 true, so reading only the string key would see no
    trigger at all and pass every reusable workflow as clean."""
    doc = {True: {"workflow_call": {"inputs": {"a": {}}}}}
    assert set(uri.call_inputs(doc)) == {"a"}
    assert set(uri.call_inputs({"on": {"workflow_call": {"inputs": {"a": {}}}}})) == {
        "a"
    }


def test_the_line_tag_the_loader_adds_is_not_an_input():
    doc = {"on": {"workflow_call": {"inputs": {"__line__": 4, "a": {}}}}}
    assert set(uri.call_inputs(doc)) == {"a"}


def test_a_document_declaring_no_call_inputs_yields_nothing():
    for doc in (
        None,
        "scalar",
        {"on": {"pull_request": None}},
        {"on": {"workflow_call": None}},
        {"on": {"workflow_call": {"inputs": None}}},
        {"on": "push"},
    ):
        assert uri.call_inputs(doc) == {}, doc


# ── local_callee / calls ─────────────────────────────────────────────────
def test_local_callee_reads_a_relative_workflow_path():
    assert (
        uri.local_callee({"uses": "./.github/workflows/x.yaml"})
        == ".github/workflows/x.yaml"
    )
    assert uri.local_callee({"uses": "./.github/workflows/x.yml"}) is not None


def test_a_callee_outside_this_tree_names_no_local_path():
    assert uri.local_callee({"uses": "org/repo/.github/workflows/x.yaml@v1"}) is None
    assert uri.local_callee({"uses": "actions/checkout@v4"}) is None
    assert uri.local_callee({"uses": "./.github/actions/setup"}) is None
    assert uri.local_callee("not-a-job") is None


def test_calls_reports_one_entry_per_calling_job():
    """Two jobs may call the same workflow and pass different inputs; the union
    of what any caller passes is what marks an input used."""
    doc = {
        "jobs": {
            "a": {"uses": "./.github/workflows/c.yaml", "with": {"x": 1}},
            "b": {"uses": "./.github/workflows/c.yaml", "with": {"y": 2}},
            "c": {"runs-on": "x"},
        }
    }
    assert sorted(uri.calls(doc)) == [
        (".github/workflows/c.yaml", {"x"}),
        (".github/workflows/c.yaml", {"y"}),
    ]


def test_a_call_passing_no_with_block_passes_no_input():
    doc = {"jobs": {"a": {"uses": "./.github/workflows/c.yaml"}}}
    assert uri.calls(doc) == [(".github/workflows/c.yaml", set())]


# ── marker_window ────────────────────────────────────────────────────────
TEXT = "\n".join(
    [
        "inputs:",
        "  first:",
        "    type: string",
        "      deeper: not-a-direct-child",
        "",
        "  second:",
        "    type: string",
    ]
)


def test_the_window_is_the_key_line_and_its_direct_children():
    window = uri.marker_window(TEXT, 2)
    assert window == ["  first:", "    type: string"]


def test_the_window_stops_at_the_next_sibling_key():
    assert "  second:" not in uri.marker_window(TEXT, 2)


def test_key_line_walks_up_from_the_block_the_loader_tagged():
    """LineLoader tags a mapping with its first key's line, so an input's parsed
    line is one below the `alpha:` line a marker sits on."""
    assert uri.key_line(TEXT, 3) == 2
    assert uri.key_line(TEXT, 7) == 6


def test_key_line_has_no_answer_above_the_top_level():
    assert uri.key_line(TEXT, 1) is None
    assert uri.key_line(TEXT, 999) is None


def test_a_line_outside_the_text_has_no_window():
    assert uri.marker_window(TEXT, 0) == []
    assert uri.marker_window(TEXT, 999) == []


# ── fixture machinery ────────────────────────────────────────────────────
def _callee(inputs: str) -> str:
    return "name: c\non:\n  workflow_call:\n    inputs:\n" + inputs


ONE_INPUT = "      alpha:\n        type: string\n        default: ''\n"


def _caller(with_block: str = "") -> str:
    return (
        "name: x\non:\n  pull_request:\njobs:\n  gate:\n"
        "    uses: ./.github/workflows/callee.yaml\n" + with_block
    )


PASSES_ALPHA = "    with:\n      alpha: hello\n"


def _check(tmp_path, monkeypatch, files: dict[str, str]):
    root = tmp_path / "repo"
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    monkeypatch.setattr(uri, "REPO_ROOT", root)
    monkeypatch.setattr(uri, "WORKFLOWS_DIR", root / ".github" / "workflows")
    return uri.check_repo(root / ".github" / "workflows")


def _tree(caller: str, callee: str) -> dict[str, str]:
    return {
        ".github/workflows/caller.yaml": caller,
        ".github/workflows/callee.yaml": callee,
    }


# ── the violating shapes ─────────────────────────────────────────────────
def test_an_input_no_caller_passes_is_reported(tmp_path, monkeypatch):
    found = _check(tmp_path, monkeypatch, _tree(_caller(), _callee(ONE_INPUT)))
    assert len(found) == 1
    path, line, message = found[0]
    assert path.name == "callee.yaml"
    assert line == 5
    assert (
        "input `alpha` is declared but no job in this repository passes it" in message
    )


def test_a_required_input_nothing_passes_says_the_call_could_not_start(
    tmp_path, monkeypatch
):
    callee = _callee("      alpha:\n        type: string\n        required: true\n")
    found = _check(tmp_path, monkeypatch, _tree(_caller(), callee))
    assert len(found) == 1
    assert "`required: true`" in found[0][2]


def test_only_the_unpassed_input_of_several_is_reported(tmp_path, monkeypatch):
    callee = _callee(ONE_INPUT + "      beta:\n        type: string\n")
    found = _check(tmp_path, monkeypatch, _tree(_caller(PASSES_ALPHA), callee))
    assert len(found) == 1
    assert "`beta`" in found[0][2]


def test_an_input_with_no_block_under_it_is_still_reported(tmp_path, monkeypatch):
    """It carries no mapping for the loader to tag, so the finding has no line —
    reporting it at the file is what keeps the empty declaration from passing."""
    found = _check(tmp_path, monkeypatch, _tree(_caller(), _callee("      alpha:\n")))
    assert len(found) == 1
    assert found[0][1] == 0
    assert "`alpha`" in found[0][2]


# ── the clean shapes (false-positive guards) ─────────────────────────────
def test_an_input_a_caller_passes_is_clean(tmp_path, monkeypatch):
    assert (
        _check(tmp_path, monkeypatch, _tree(_caller(PASSES_ALPHA), _callee(ONE_INPUT)))
        == []
    )


def test_a_second_caller_passing_it_is_enough(tmp_path, monkeypatch):
    """The union of every caller's `with:` names the used inputs, so one caller
    leaving an input at its default is not a finding."""
    files = _tree(_caller(), _callee(ONE_INPUT))
    files[".github/workflows/other.yaml"] = _caller(PASSES_ALPHA)
    assert _check(tmp_path, monkeypatch, files) == []


def test_a_reusable_workflow_no_local_job_calls_is_skipped(tmp_path, monkeypatch):
    """Its callers may live in another repository, so every one of its inputs
    would report and none of the reports could be checked here."""
    files = {".github/workflows/callee.yaml": _callee(ONE_INPUT)}
    assert _check(tmp_path, monkeypatch, files) == []


def test_a_workflow_that_declares_no_call_inputs_is_not_examined(tmp_path, monkeypatch):
    files = _tree(
        _caller(), "name: c\non:\n  workflow_call:\njobs:\n  a:\n    steps: []\n"
    )
    assert _check(tmp_path, monkeypatch, files) == []


def test_a_cross_repo_caller_in_this_tree_does_not_mark_the_input_used(
    tmp_path, monkeypatch
):
    """A `uses:` with an `@ref` names another repository's file, so its `with:`
    block says nothing about the local workflow of the same name."""
    files = _tree(_caller(), _callee(ONE_INPUT))
    files[".github/workflows/other.yaml"] = (
        "name: o\non:\n  pull_request:\njobs:\n  gate:\n"
        "    uses: org/repo/.github/workflows/callee.yaml@v1\n"
        "    with:\n      alpha: hello\n"
    )
    found = _check(tmp_path, monkeypatch, files)
    assert len(found) == 1
    assert "`alpha`" in found[0][2]


# ── opt-out ──────────────────────────────────────────────────────────────
def test_a_reasoned_opt_out_suppresses_the_finding(tmp_path, monkeypatch):
    callee = _callee(
        f"      alpha:  # {uri.OPT_OUT}: passed by the release workflow next week\n"
        "        type: string\n"
    )
    assert _check(tmp_path, monkeypatch, _tree(_caller(), callee)) == []


def test_an_opt_out_on_a_direct_child_line_suppresses_the_finding(
    tmp_path, monkeypatch
):
    callee = _callee(
        "      alpha:\n"
        f"        # {uri.OPT_OUT}: an external repository calls this workflow\n"
        "        type: string\n"
    )
    assert _check(tmp_path, monkeypatch, _tree(_caller(), callee)) == []


def test_an_opt_out_with_no_reason_suppresses_nothing_and_is_reported(
    tmp_path, monkeypatch
):
    callee = _callee(f"      alpha:  # {uri.OPT_OUT}: todo\n        type: string\n")
    found = _check(tmp_path, monkeypatch, _tree(_caller(), callee))
    assert len(found) == 1
    assert "states only 'todo'" in found[0][2]


def test_a_longer_slug_containing_the_token_suppresses_nothing(tmp_path, monkeypatch):
    callee = _callee(
        f"      alpha:  # not-{uri.OPT_OUT}: a different annotation\n"
        "        type: string\n"
    )
    found = _check(tmp_path, monkeypatch, _tree(_caller(), callee))
    assert len(found) == 1
    assert "is declared but no job" in found[0][2]


def test_an_opt_out_in_another_block_does_not_reach_this_input(tmp_path, monkeypatch):
    """An input may share a name with a job key, so a marker matched anywhere in
    the byte stream would suppress a real finding from the other block."""
    callee = _callee(ONE_INPUT) + (
        "jobs:\n"
        f"  alpha:  # {uri.OPT_OUT}: a real reason, about the job\n"
        "    runs-on: x\n    steps: []\n"
    )
    found = _check(tmp_path, monkeypatch, _tree(_caller(), callee))
    assert len(found) == 1
    assert "is declared but no job" in found[0][2]


# ── parse failures ───────────────────────────────────────────────────────
def test_unparseable_yaml_is_reported_rather_than_passed_as_clean(
    tmp_path, monkeypatch
):
    files = _tree(_caller(), _callee(ONE_INPUT))
    files[".github/workflows/broken.yaml"] = "jobs:\n  a: [\n   unbalanced\n"
    found = _check(tmp_path, monkeypatch, files)
    messages = [message for _p, _l, message in found]
    assert any("could not parse as YAML" in m for m in messages)


# ── main ─────────────────────────────────────────────────────────────────
def _root(tmp_path, monkeypatch, files: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    monkeypatch.setattr(uri, "REPO_ROOT", root)
    monkeypatch.setattr(uri, "WORKFLOWS_DIR", root / ".github" / "workflows")
    return root


def test_main_annotates_each_violation_and_exits_one(tmp_path, monkeypatch, capsys):
    _root(tmp_path, monkeypatch, _tree(_caller(), _callee(ONE_INPUT)))
    assert uri.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/callee.yaml,line=5::" in out
    assert "1 unused reusable-workflow input(s) found." in out


def test_main_is_clean_when_every_input_has_a_caller(tmp_path, monkeypatch, capsys):
    _root(tmp_path, monkeypatch, _tree(_caller(PASSES_ALPHA), _callee(ONE_INPUT)))
    assert uri.main() == 0
    assert "::error" not in capsys.readouterr().out


def test_main_says_so_over_a_tree_with_no_workflow(tmp_path, monkeypatch, capsys):
    """Exit 0 is honest — no workflow, nothing to violate — so the note is what
    tells a caller that apart from a real pass."""
    _root(tmp_path, monkeypatch, {"README.md": "x\n"})
    assert uri.main() == 0
    assert "scanned nothing" in capsys.readouterr().err

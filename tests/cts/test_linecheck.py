"""Tests for hooks/_linecheck.py — the machinery shared by the line-oriented
pre-commit lints (the read-each-path loop, the skip-on-unreadable, the
``<path>:<lineno>: <message>`` print loop and exit code) and the workflow-file
discovery glob shared by the YAML lints.

The per-script test modules keep only their own detection cases plus one thin
``main()`` wiring assertion; the generic loop behaviour asserted here is not
duplicated across them.
"""

import textwrap
from pathlib import Path

import pytest

from tests._helpers import load_hook

lc = load_hook("_linecheck.py", "_linecheck")


def _wf(body: str) -> str:
    return textwrap.dedent(body)


# ── run_line_checks ──────────────────────────────────────────────────────
def _even_lines(text: str) -> list[int]:
    """Toy detector: flag every line whose number is even (exercises the loop
    without coupling the loop test to any real lint's rules)."""
    return [n for n, _ in enumerate(text.splitlines(), 1) if n % 2 == 0]


def test_run_line_checks_prints_each_hit_and_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "f.txt"
    f.write_text("a\nb\nc\nd\n")  # lines 2 and 4 flagged
    status = lc.run_line_checks([str(f)], _even_lines, "bad thing")
    assert status == 1
    err = capsys.readouterr().err
    assert f"{f}:2: bad thing" in err
    assert f"{f}:4: bad thing" in err
    assert f"{f}:1:" not in err


def test_run_line_checks_returns_zero_when_no_hits(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("only one line\n")  # no even line -> no hit
    assert lc.run_line_checks([str(f)], _even_lines, "msg") == 0


def test_run_line_checks_skips_unreadable_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A missing path raises OSError inside the loop and is skipped, not crashed on;
    # a real hit in another path still fires and sets the exit code.
    bad = tmp_path / "hit.txt"
    bad.write_text("x\ny\n")  # line 2 flagged
    missing = tmp_path / "nope.txt"  # never created -> OSError -> skipped
    status = lc.run_line_checks([str(missing), str(bad)], _even_lines, "msg")
    assert status == 1
    assert f"{bad}:2: msg" in capsys.readouterr().err


def test_run_line_checks_skips_undecodable_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Non-UTF-8 bytes raise UnicodeDecodeError, which the loop swallows (the file
    # contributes nothing); the scan must not crash.
    f = tmp_path / "binary.txt"
    f.write_bytes(b"\xff\xfe\x00\x01")
    assert lc.run_line_checks([str(f)], _even_lines, "msg") == 0
    assert capsys.readouterr().err == ""


# ── workflow_files ───────────────────────────────────────────────────────
def _write(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body)
    return path


def test_workflow_files_collects_workflows_and_actions(tmp_path: Path) -> None:
    wf = tmp_path / "workflows"
    actions = tmp_path / "actions"
    _write(wf, "a.yaml", "on: push\n")
    _write(wf, "b.yml", "on: push\n")
    _write(actions / "setup", "action.yaml", "name: s\n")
    _write(actions / "other", "action.yml", "name: o\n")
    files = lc.workflow_files(wf, actions)
    assert files == sorted(files)  # path-sorted
    assert sorted(p.name for p in files) == [
        "a.yaml",
        "action.yaml",
        "action.yml",
        "b.yml",
    ]


def test_workflow_files_skips_absent_actions_dir(tmp_path: Path) -> None:
    wf = tmp_path / "workflows"
    _write(wf, "a.yaml", "on: push\n")
    assert [p.name for p in lc.workflow_files(wf, tmp_path / "nonexistent")] == [
        "a.yaml"
    ]


# ── has_decide_gate / has_always_reporter ────────────────────────────────
# The required-check-shape probes shared by check_always_reporter and
# check_concurrency; unit-tested here where they live.


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ({"decide": {"uses": "./.github/workflows/decide-reusable.yaml"}}, True),
        ({"work": {"if": "needs.decide.outputs.run == 'true'"}}, True),
        ({"build": {"runs-on": "ubuntu-latest"}}, False),
        ({"odd": "scalar"}, False),  # non-dict job config is skipped
        ({}, False),
    ],
)
def test_has_decide_gate(jobs: dict, expected: bool) -> None:
    assert lc.has_decide_gate(jobs) is expected


@pytest.mark.parametrize(
    "jobs, expected",
    [
        ({"reporter": {"if": "always()", "runs-on": "ubuntu-latest"}}, True),
        ({"work": {"if": "needs.decide.outputs.run == 'true'"}}, False),
        ({"job": {"if": "always() && some.condition"}}, False),  # exact match only
        ({"odd": "scalar"}, False),  # non-dict job config is skipped
        ({}, False),
    ],
)
def test_has_always_reporter(jobs: dict, expected: bool) -> None:
    assert lc.has_always_reporter(jobs) is expected


# ── _job_blocks / _classification_text ───────────────────────────────────────
# The comment-scope carver, shared by the required-check lint and the apply step.


def test_job_blocks_no_jobs_key_yields_no_blocks() -> None:
    assert lc._job_blocks("on:\n  push:\n") == {}


def test_job_blocks_jobs_key_but_no_job_lines_yields_no_blocks() -> None:
    # `jobs:` followed only by a comment → job_indent never determined.
    assert lc._job_blocks("jobs:\n  # nothing here\n") == {}


def test_job_blocks_stops_at_dedented_sibling_and_excludes_trailer() -> None:
    text = _wf(
        """\
        jobs:
          a:
            name: A  # required-check: true
            steps: []
          b:
            name: B
        # trailing top-level comment
        """
    )
    blocks = lc._job_blocks(text)
    assert set(blocks) == {"a", "b"}
    assert blocks["a"][0] == 2  # `a:` key line
    assert "required-check: true" in blocks["a"][1]
    assert "# trailing top-level comment" not in blocks["b"][1]


def test_job_blocks_stops_at_top_level_key_after_jobs() -> None:
    text = _wf(
        """\
        jobs:
          a:
            name: A  # required-check: true
        defaults:
          run:
            shell: bash
        """
    )
    blocks = lc._job_blocks(text)
    assert set(blocks) == {"a"}
    assert "defaults" not in blocks["a"][1]


def test_classification_text_empty_block_is_empty() -> None:
    assert lc._classification_text("") == ""


def test_classification_text_only_key_line_when_no_children() -> None:
    assert lc._classification_text("  solo:") == "  solo:"


# ── matrix_combinations / expand_name ────────────────────────────────────────


def test_matrix_axes_cartesian_product() -> None:
    assert lc.matrix_combinations({"a": [1, 2], "b": ["x", "y"]}) == [
        {"a": 1, "b": "x"},
        {"a": 1, "b": "y"},
        {"a": 2, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_matrix_empty_is_single_empty_combo() -> None:
    assert lc.matrix_combinations({}) == [{}]


def test_matrix_exclude_removes_combo() -> None:
    assert lc.matrix_combinations({"a": [1, 2], "exclude": [{"a": 1}]}) == [{"a": 2}]


def test_matrix_include_only_is_each_entry() -> None:
    assert lc.matrix_combinations(
        {"include": [{"arch": "amd64"}, {"arch": "arm64"}]}
    ) == [{"arch": "amd64"}, {"arch": "arm64"}]


def test_matrix_include_only_empty_is_single_empty_combo() -> None:
    # A matrix with an empty `include:` and no axes schedules one bare job.
    assert lc.matrix_combinations({"include": []}) == [{}]


def test_matrix_include_extends_matching_axis_combo() -> None:
    combos = lc.matrix_combinations({"a": [1, 2], "include": [{"a": 1, "extra": "z"}]})
    assert {"a": 1, "extra": "z"} in combos
    assert {"a": 2} in combos


def test_matrix_include_appends_when_no_axis_match() -> None:
    combos = lc.matrix_combinations({"a": [1], "include": [{"a": 9, "b": "q"}]})
    assert {"a": 1} in combos and {"a": 9, "b": "q"} in combos


def test_matrix_multi_axis_exclude_then_include_extends_every_match() -> None:
    # exclude drops one product row; the include's axis key (`a: 2`) matches the
    # two surviving `a==2` rows and extends BOTH (the extendable-loop), never
    # appending a duplicate — pins the exact GitHub-scheduled set.
    assert lc.matrix_combinations(
        {
            "a": [1, 2],
            "b": ["x", "y"],
            "exclude": [{"a": 1, "b": "y"}],
            "include": [{"a": 2, "extra": "z"}],
        }
    ) == [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "x", "extra": "z"},
        {"a": 2, "b": "y", "extra": "z"},
    ]


def test_expand_name_without_refs_is_identity() -> None:
    assert lc.expand_name("Static check", {}) == ["Static check"]


def test_expand_name_expands_each_matrix_value_sorted_unique() -> None:
    name = "Build (${{ matrix.arch }})"
    matrix = {"include": [{"arch": "amd64"}, {"arch": "arm64"}]}
    assert lc.expand_name(name, matrix) == ["Build (amd64)", "Build (arm64)"]


def test_expand_name_skips_combos_missing_the_referenced_key() -> None:
    name = "X (${{ matrix.arch }})"
    matrix = {"include": [{"other": "v"}, {"arch": "amd64"}]}
    assert lc.expand_name(name, matrix) == ["X (amd64)"]


def test_expand_name_two_refs_one_axis() -> None:
    name = "Build ${{ matrix.image }} (${{ matrix.arch }})"
    matrix = {"arch": ["amd64", "arm64"], "image": ["ccr", "monitor"]}
    assert lc.expand_name(name, matrix) == [
        "Build ccr (amd64)",
        "Build ccr (arm64)",
        "Build monitor (amd64)",
        "Build monitor (arm64)",
    ]


# ── required_check_contexts ──────────────────────────────────────────────────


def test_required_check_contexts_non_dict_doc() -> None:
    assert lc.required_check_contexts("- just\n- a list\n") == []


def test_required_check_contexts_jobs_not_a_mapping() -> None:
    assert lc.required_check_contexts("jobs: not-a-map\n") == []


def test_required_check_contexts_reads_marker_from_any_job_not_just_reporters() -> None:
    # A cheap always-run linter (no `if: always()`) still produces a required
    # check — the marker is read from EVERY job, the apply-side semantics.
    text = _wf(
        """\
        jobs:
          lint:
            name: Cheap gate
            runs-on: ubuntu-latest  # required-check: true
        """
    )
    assert lc.required_check_contexts(text) == ["Cheap gate"]


def test_required_check_contexts_marker_buried_in_step_does_not_count() -> None:
    text = _wf(
        """\
        jobs:
          deep:
            name: Deep
            steps:
              - run: "echo required-check: true"
        """
    )
    assert lc.required_check_contexts(text) == []


def test_required_check_contexts_skips_non_dict_job_and_unmarked_job() -> None:
    text = _wf(
        """\
        jobs:
          scalar: 3
          unmarked:
            name: Advisory
          required:
            name: Gated (${{ matrix.arch }})  # required-check: true
            strategy:
              matrix:
                include:
                  - arch: amd64
        """
    )
    assert lc.required_check_contexts(text) == ["Gated (amd64)"]


def test_required_check_contexts_falls_back_to_job_key_when_name_absent() -> None:
    text = _wf(
        """\
        jobs:
          bare:  # required-check: true
            runs-on: ubuntu-latest
        """
    )
    assert lc.required_check_contexts(text) == ["bare"]

"""Tests for ci_truth_serum/_linecheck.py — the machinery shared by the line-oriented
pre-commit lints (the read-each-path loop, the skip-on-unreadable, the
``<path>:<lineno>: <message>`` print loop and exit code) and the workflow-file
discovery glob shared by the YAML lints.

The per-script test modules keep only their own detection cases plus one thin
``main()`` wiring assertion; the generic loop behaviour asserted here is not
duplicated across them.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

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


# ── run_file_cli ─────────────────────────────────────────────────────────
def test_run_file_cli_refuses_an_empty_file_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc.sys, "argv", ["/x/ci_truth_serum/check_thing.py"])
    called = []
    assert lc.run_file_cli(lambda argv: called.append(argv) or 0) == 2
    assert called == []  # the check never ran, so it cannot have reported a pass
    err = capsys.readouterr().err
    assert "check_thing: no files to scan" in err
    assert "git ls-files -z | xargs -0 python -m ci_truth_serum.check_thing" in err


def test_run_file_cli_passes_the_files_through_and_returns_the_check_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lc.sys, "argv", ["check_thing.py", "a.sh", "b.sh"])
    seen: list[list[str]] = []
    assert lc.run_file_cli(lambda argv: seen.append(argv) or 1) == 1
    assert seen == [["a.sh", "b.sh"]]


def test_every_content_check_refuses_a_run_with_no_files() -> None:
    """No content check may report a clean pass over an empty file list.

    Driven from the tier registry, not a pasted list, so a content check added
    later without the guard fails here rather than shipping the false green.
    """
    run_tier = load_hook("run_tier.py", "run_tier")
    content = sorted(
        {
            m
            for members in run_tier.TIERS.values()
            for m, kind in members
            if kind != run_tier.WORKFLOW
        }
    )
    assert len(content) > 20, "registry lookup found almost nothing — check the kinds"
    statuses = {
        module: subprocess.run(
            [sys.executable, "-m", f"ci_truth_serum.{module}"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=False,
        ).returncode
        for module in content
    }
    assert statuses == dict.fromkeys(content, 2)


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


def test_workflow_files_says_so_when_it_discovers_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lc.workflow_files(tmp_path / "workflows", tmp_path / "actions") == []
    err = capsys.readouterr().err
    assert "this check scanned nothing" in err


def test_workflow_files_is_silent_when_it_discovers_something(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wf = tmp_path / "workflows"
    _write(wf, "a.yaml", "on: push\n")
    assert len(lc.workflow_files(wf, tmp_path / "actions")) == 1
    assert capsys.readouterr().err == ""


def test_every_workflow_check_says_so_over_a_tree_with_no_workflows(
    tmp_path: Path,
) -> None:
    """No workflow check may report a silent clean pass over a tree it never scanned.

    Exit 0 is honest here — a repository with no workflow has none to violate —
    so the assertion is on the notice, which is what tells the two cases apart.
    Driven from the tier registry so a workflow check added later is covered.
    """
    run_tier = load_hook("run_tier.py", "run_tier")
    workflow_checks = sorted(
        {
            m
            for members in run_tier.TIERS.values()
            for m, kind in members
            if kind == run_tier.WORKFLOW
        }
    )
    assert len(workflow_checks) > 15, "registry lookup found almost nothing"
    silent = []
    for module in workflow_checks:
        done = subprocess.run(
            [sys.executable, "-m", f"ci_truth_serum.{module}"],
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )
        if "scanned nothing" not in done.stderr:
            silent.append((module, done.returncode, done.stderr[:200]))
    assert silent == []


# ── tracked_shell_files ──────────────────────────────────────────────────
def _git_repo(tmp_path: Path, *files: str) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name in files:
        (tmp_path / name).write_text("#!/usr/bin/env bash\ntrue\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_tracked_shell_files_says_so_when_the_tree_has_none(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_git_repo(tmp_path, "notes.md"))
    assert lc.tracked_shell_files() == []
    assert "this check scanned nothing" in capsys.readouterr().err


def test_tracked_shell_files_is_silent_when_the_tree_has_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_git_repo(tmp_path, "run.sh"))
    assert lc.tracked_shell_files() == ["run.sh"]
    assert capsys.readouterr().err == ""


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
        # The ${{ }} wrapper is evaluated identically by GitHub — a reporter.
        ({"reporter": {"if": "${{ always() }}"}}, True),
        ({"work": {"if": "needs.decide.outputs.run == 'true'"}}, False),
        # A compound condition does not always run, so it is no reporter — even
        # when wrapped. This stays excluded by design.
        ({"job": {"if": "always() && some.condition"}}, False),
        ({"job": {"if": "${{ always() && some.condition }}"}}, False),
        ({"odd": "scalar"}, False),  # non-dict job config is skipped
        ({}, False),
    ],
)
def test_has_always_reporter(jobs: dict, expected: bool) -> None:
    assert lc.has_always_reporter(jobs) is expected


@pytest.mark.parametrize(
    "if_value, expected",
    [
        ("always()", True),
        ("${{ always() }}", True),
        ("${{always()}}", True),  # no inner spacing
        ("${{   always()   }}", True),  # extra inner spacing
        ("  always()  ", True),  # surrounding whitespace
        ("always() && needs.x.result == 'success'", False),  # compound: not a reporter
        ("${{ always() && needs.x.result == 'success' }}", False),  # wrapped compound
        ("success()", False),
        ("", False),
    ],
)
def test_is_always_reporter(if_value: str, expected: bool) -> None:
    assert lc.is_always_reporter(if_value) is expected


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


def test_expand_name_ref_is_a_strict_subset_of_the_matrix_axes() -> None:
    # The name references only `arch`, but the matrix also varies `os`. The combo
    # filter must accept any combo whose keys are a SUPERSET of the refs (refs <=
    # keys), then dedup once `os` is substituted away -- yielding one context per
    # distinct arch. Pins the `refs <= keys` subset test against the `refs == keys`
    # mutant, which would demand an exact key match and drop every combo (the combo
    # always carries the extra `os` key), collapsing the result to [].
    name = "Build (${{ matrix.arch }})"
    matrix = {"arch": ["amd64", "arm64"], "os": ["linux", "mac"]}
    assert lc.expand_name(name, matrix) == ["Build (amd64)", "Build (arm64)"]


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


# ── opted_out / concurrency_line / job_concurrency_line ──────────────────
# Shared by the concurrency lints (check_concurrency, check_static_concurrency,
# check_requires_concurrency, check_pending_cancel_concurrency), which each pass
# their own opt-out token. Direct tests live here because this module is the
# helpers' mutation oracle.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Token inside a real comment — standalone or trailing — opts out.
        ("# my-token\nconcurrency:\n  group: x\n", True),
        ("concurrency:  # my-token\n  group: x\n", True),
        # Token in a string VALUE must not opt out (fail-open otherwise).
        ('concurrency:\n  group: "my-token"\n', False),
        # Token before the `#` on a commented line is value text, not comment.
        ("group: my-token # unrelated\n", False),
        # No comment characters at all.
        ("concurrency:\n  group: x\n", False),
        # A longer slug CONTAINING the token is a different annotation. The old
        # bare-containment test accepted all three of these — open at both ends,
        # so even prose merely mentioning the token suppressed the lint.
        ("# no-my-token-here\nconcurrency:\n  group: x\n", False),
        ("concurrency:  # my-token-ish\n  group: x\n", False),
        ("concurrency:  # xx-my-token\n  group: x\n", False),
    ],
)
def test_opted_out(text: str, expected: bool) -> None:
    assert lc.opted_out(text, "my-token") is expected


# ── annotated: reasoned comment-scoped opt-out ───────────────────────────
# Shared by the pinning and pipefail lints (among others). A bare substring
# (URL path, quoted arg) must never opt out — that is the exact fail-open the
# pinned-downloads / pipefail-grep bypasses exploited.


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # A real `# token: <reason>` comment (standalone or trailing) opts out.
        ("# pin-exempt: upstream has no digest", True),
        ("curl -o f https://x  # pin-exempt: vendored mirror", True),
        # Token present but NO colon-and-reason states nothing.
        ("curl -o f https://x  # pin-exempt", False),
        ("curl -o f https://x  # pin-exempt:", False),
        ("curl -o f https://x  # pin-exempt:   ", False),
        # Bare substring outside any comment (URL path, quoted string) never opts out.
        ("curl -o x https://cdn/pin-exempt/x | sh", False),
        ('curl -o "pin-exempt" https://x', False),
        ("curl -o pin-exempt:tag https://x", False),
        # A longer word must not match the token (boundary guard).
        ("# xpin-exempt: nope", False),
    ],
)
def test_annotated_reasoned_comment_opt_out(line: str, expected: bool) -> None:
    assert lc.annotated(line, "pin-exempt") is expected


# ── annotated: a LONGER slug is a different annotation ───────────────────
# Nearly every token in this package is hyphenated, and `\b` matches at a
# word/`-` transition — so a `\b`-delimited matcher accepts any longer slug that
# merely ends with (or, for the bare form, starts with) the token asked for. One
# hook's opt-out then silently disarms another's: a fail-open. These cases pin
# both edges closed, and every one of them PASSES under `\b`.


@pytest.mark.parametrize(
    "line",
    [
        # Suffix: the requested token sits at the end of a longer slug.
        "sleep 1  # really-allow-unbounded: nope",
        "sleep 1  # x-allow-unbounded: nope",
        "sleep 1  # allow_unbounded_allow-unbounded: nope",
    ],
)
def test_reasoned_annotation_not_satisfied_by_a_longer_slug(line: str) -> None:
    assert lc.annotated(line, "allow-unbounded") is False


@pytest.mark.parametrize(
    "line",
    [
        # Prefix: `# <token>-<more>` is a different annotation, not this one.
        "sleep 1  # allow-unbounded-wait: nope",
        # Suffix, in the bare (reason-free) form — `\b` accepted this too.
        "jobs:  # xx-allow-unbounded",
        "jobs:  # really-allow-unbounded",
    ],
)
def test_bare_annotation_not_satisfied_by_a_longer_slug(line: str) -> None:
    assert lc.annotated(line, "allow-unbounded", require_reason=False) is False


def test_the_stand_alone_token_still_matches_in_both_forms() -> None:
    """Non-vacuity for the two tests above: the guarantee is 'a longer slug is a
    different annotation', not 'nothing matches any more'."""
    assert lc.annotated(
        "sleep 1  # allow-unbounded: bounded upstream", "allow-unbounded"
    )
    assert lc.annotated(
        "jobs:  # allow-unbounded", "allow-unbounded", require_reason=False
    )
    # The reason-required form's right edge is the literal `:`, so a token
    # followed directly by its colon is unaffected by the tail lookahead.
    assert lc.annotated("jobs:  # allow-unbounded:x", "allow-unbounded")
    # The left edge is an alternation, not a lookbehind, because `<!--` and `//`
    # end in characters a lookbehind would reject — a token abutting its own
    # introducer must still match.
    for intro in ("#", "<!--", "//"):
        assert lc.annotated(f"{intro}allow-unbounded: reason", "allow-unbounded"), intro
    # A separator that cannot appear in a token still reads as a real annotation.
    assert lc.annotated("# see notes.allow-unbounded: reason", "allow-unbounded")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Exact 1-based line of the TOP-LEVEL key, past earlier lines.
        ("name: x\non: push\nconcurrency:\n  group: x\n", 3),
        ("concurrency:\n  group: x\n", 1),
        # Spacing before the colon is tolerated.
        ("name: x\nconcurrency :\n  group: x\n", 2),
        # An INDENTED (job-level) key is not the top-level block.
        ("name: x\njobs:\n  a:\n    concurrency:\n      group: x\n", 1),
        # No key at all falls back to line 1.
        ("name: x\njobs: {}\n", 1),
    ],
)
def test_concurrency_line(text: str, expected: int) -> None:
    assert lc.concurrency_line(text) == expected


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        # Key on the 3rd body line of a job block starting at line 10 → 10 + 2.
        ((10, "  job:\n    needs: a\n    concurrency:\n      group: x\n"), 12),
        # Key on the line right after the job key.
        ((4, "  job:\n    concurrency:\n      group: x\n"), 5),
        # Block without the key, and no block at all, both fall back.
        ((10, "  job:\n    runs-on: ubuntu-latest\n"), 99),
        (None, 99),
    ],
)
def test_job_concurrency_line(block, expected: int) -> None:
    assert lc.job_concurrency_line(block, 99) == expected


# ── group_is_per_ref: the key must sit inside a ${{ }} expression span ────
@pytest.mark.parametrize(
    "group,per_ref",
    [
        ("ci-${{ github.ref }}", True),
        ("${{ github.workflow }}-${{ github.head_ref || github.run_id }}", True),
        ("pr-${{ github.event.number }}", True),
        # literal mention outside any expression span: one static string for
        # every ref — treating it as per-ref would fail open
        ("github.ref-shared", False),
        ("docs about ${{ github.workflow }} and github.head_ref", False),
        ("static-lock", False),
    ],
)
def test_group_is_per_ref_requires_expression_span(group: str, per_ref: bool) -> None:
    assert lc.group_is_per_ref(group) is per_ref


# ── is_test_path: one predicate, two consumers ───────────────────────────
# check_drift_guards scopes its phrase pass to tests; check_toolchain_skips
# scopes its skipif scan to what pytest collects. Two hand-rolled peers had
# already diverged, and BOTH were dead for the shortest members of the set — a
# scope filter's recall bug produces a green vacuous pass, never an error, so it
# is enumerated member by member here.


@pytest.mark.parametrize(
    "path,is_test",
    [
        # The shortest members of each class — every one of these was False under
        # at least one of the two hand-rolled predicates this replaced.
        ("test.py", True),
        ("x/test.py", True),
        ("conftest.py", True),
        ("spec.rb", True),
        ("x/spec.js", True),
        ("test/x.py", True),
        ("specs/thing.sh", True),
        # …and the long members that already worked.
        ("tests/x.py", True),
        ("x_test.py", True),
        ("x.test.js", True),
        ("tests/cts/test_x.sh", True),
        ("src/__tests__/bar.mjs", True),
        ("scripts/widget.spec.ts", True),
        # The left boundary stays mandatory, so a word merely ENDING in the stem
        # is not a test file.
        ("latest.py", False),
        ("src/protest.mjs", False),
        ("greatest.sh", False),
        ("spectrum.ts", False),
        ("testing.py", False),
        ("scripts/release.sh", False),
        # Windows separators normalize before matching.
        ("x\\tests\\y.py", True),
    ],
)
def test_is_test_path_covers_the_shortest_member_of_every_class(
    path: str, is_test: bool
) -> None:
    assert lc.is_test_path(path) is is_test


def test_exactly_one_definition_of_the_predicate_exists() -> None:
    """The consumers must IMPORT it, not re-derive it. Two hand-rolled peers is how
    the recall hole above survived in both; a third would do it again.

    Counts occurrences rather than listing files, because the failure this caught
    for real was two definitions in the SAME module: a git merge of two branches
    that independently consolidated the predicate auto-merged both blocks in, and
    the second silently won at import time. A per-file existence check passes on
    that; a count does not.
    """
    definitions = {
        path.name: path.read_text(encoding="utf-8").count("def is_test_path(")
        for path in sorted(HOOKS_DIR.glob("*.py"))
    }
    assert {k: v for k, v in definitions.items() if v} == {"_linecheck.py": 1}, (
        f"is_test_path must be defined exactly once, in _linecheck: {definitions}"
    )


# ── comment_body / is_python_source ──────────────────────────────────────
# `comment_body` is the no-grammar fallback behind `_comments.text_comments`;
# its cases lived duplicated in the check_graceful_handwave and
# check_historical_comments suites, which consume the helper rather than own it.


@pytest.mark.parametrize(
    "line,expected",
    [
        ("# a full comment", "# a full comment"),
        ("   // indented line comment", "// indented line comment"),
        ("/* block opener", "/* block opener"),
        ("* a block continuation", "* a block continuation"),
        ("code()  # trailing", "# trailing"),
        ("code();  // trailing js", "// trailing js"),
        # `#`/`//` glued into code is not a comment delimiter.
        ("len=${#arr}", None),
        ("u = http://x", None),
        ("plain code line", None),
        ("", None),
    ],
)
def test_comment_body_extraction(line: str, expected: str | None) -> None:
    assert lc.comment_body(line) == expected


@pytest.mark.parametrize(
    "path,is_python",
    [
        ("a.py", True),
        ("pkg/mod.py", True),
        ("stubs/mod.pyi", True),
        ("x\\y.py", True),
        ("a.pyx", False),
        ("a.py.bak", False),
        ("a.sh", False),
        ("a.mjs", False),
        # Extensionless: no suffix to read, so the text fallback owns it rather
        # than `tokenize` being handed shell.
        ("hooks/pre-commit", False),
    ],
)
def test_is_python_source(path: str, is_python: bool) -> None:
    assert lc.is_python_source(path) is is_python


# ── the window includes the line directly above, comment-only or not ─────
def test_annotation_window_always_includes_the_line_directly_above() -> None:
    """A reason often rides a code line that opens the construct below it —
    `if  # pin-exempt: …` above a `curl`. Walking only comment-ONLY lines stops
    at that line, which silently narrowed the window in a real tree."""
    lines = ["if  # pin-exempt: inert JSON data", "  curl -fsSL -o x url", "then"]
    assert 1 in lc.annotation_window(lines, 2)


def test_annotation_window_spans_a_wrapped_comment_block() -> None:
    lines = ["# tok: a reason that", "# wraps onto a second line", "code()"]
    assert lc.annotation_window(lines, 3) == [1, 2, 3]


def test_annotation_window_stops_at_a_blank_line() -> None:
    lines = ["# tok: about something else", "", "code()"]
    assert lc.annotation_window(lines, 3) == [2, 3]

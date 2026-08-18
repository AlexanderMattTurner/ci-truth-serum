"""Tests for ci_truth_serum/check_conclusion_coverage.py — the guard that fails a
consumer recognizing a strict subset of the terminal-red run conclusions.

The load-bearing suite here is the MUTATION set: a classifier that names the
whole declared set is built once per surface, then rebuilt with exactly one
required conclusion removed, and the guard must name that one conclusion and no
other. A guard tested only against `== 'failure'` would still pass after its
required set silently shrank to one member.

The two probes every shell lint in this pack must survive — the idiom inside a
logger's message string, and the idiom inside a heredoc body — have their own
cases below.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, commit_all, init_test_repo, load_hook

mod = load_hook("check_conclusion_coverage.py", "check_conclusion_coverage")

# Written out, NOT read from the module. Every case below builds its fixtures
# from this list, so deleting a member of TERMINAL_RED shrinks the guard AND the
# suite together if the suite derives the list — and the whole file stays green
# while the set silently narrows. The literal is what makes that impossible.
REQUIRED = ["action_required", "failure", "startup_failure", "timed_out"]
WORKFLOW_PATH = ".github/workflows/notify.yaml"


def test_the_declared_set_is_exactly_the_four_terminal_red_conclusions() -> None:
    assert sorted(mod.TERMINAL_RED) == REQUIRED


def test_the_conclusions_a_consumer_never_owes_stay_out_of_the_declared_set() -> None:
    """`cancelled` is the load-bearing one: a run a newer push supersedes ends
    there in normal operation, so requiring it would page on every rebase."""
    assert mod.TERMINAL_RED.isdisjoint({"cancelled", "stale", "success", "skipped"})
    assert {"cancelled", "stale", "success", "skipped"} <= mod.VOCABULARY


# ── builders: one complete classifier per surface ────────────────────────
def _workflow(names: list[str]) -> str:
    """A `workflow_run` listener that acts on NAMES."""
    return (
        "name: Notify\n"
        "on:\n"
        "  workflow_run:\n"
        "    workflows: [CI]\n"
        "    types: [completed]\n"
        "jobs:\n"
        "  notify:\n"
        f"    if: contains(fromJSON('{json.dumps(names)}'), "
        "github.event.workflow_run.conclusion)\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: ./notify.sh\n"
    )


def _shell(names: list[str]) -> str:
    """A router that dispatches on a conclusion read back from the API."""
    return (
        "#!/bin/bash\n"
        'conclusion=$(gh run view "$1" --json conclusion -q .conclusion)\n'
        'case "$conclusion" in\n'
        f"  {'|'.join(names)}) notify ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )


def _python(names: list[str]) -> str:
    """A main-branch scanner that decides which runs are breakage."""
    return (
        f"RED = frozenset({sorted(names)!r})\n"
        "\n"
        "def is_broken(run):\n"
        '    return run["conclusion"] in RED\n'
    )


BUILDERS = {
    "workflow": (_workflow, WORKFLOW_PATH),
    "shell": (_shell, "route.sh"),
    "python": (_python, "scan.py"),
}


# ── the complete classifier passes on every surface ──────────────────────
@pytest.mark.parametrize("surface", sorted(BUILDERS))
def test_a_classifier_naming_the_whole_set_passes(surface: str) -> None:
    build, path = BUILDERS[surface]
    assert mod.violations(build(REQUIRED), path) == []


# ── mutation coverage: drop exactly one required conclusion ──────────────
@pytest.mark.parametrize("surface", sorted(BUILDERS))
@pytest.mark.parametrize("dropped", REQUIRED)
def test_dropping_one_conclusion_names_that_one_conclusion(
    surface: str, dropped: str
) -> None:
    """The guard's verdict must move when the declared set moves, member by
    member — otherwise a required conclusion could be deleted from
    TERMINAL_RED with the whole suite still green."""
    build, path = BUILDERS[surface]
    kept = [name for name in REQUIRED if name != dropped]
    found = mod.violations(build(kept), path)
    if dropped == mod.TRIGGER:
        # Removing `failure` removes the question, not an answer: what is left
        # names specific causes, which this guard deliberately does not judge.
        assert found == []
        return
    assert len(found) == 1, found
    line, message = found[0]
    assert line >= 1
    # Exactly the dropped one, never a kept one: a message that named the whole
    # set would pass a substring test while saying nothing about what changed.
    assert f"GitHub also returns {[dropped]}" in message
    assert f"recognizes only {kept}" in message


# ── the three defects the guard was built from ───────────────────────────
LISTENER = _workflow(["failure"])
ROUTER = _shell(["failure"])
SCANNER = _python(["failure"])


@pytest.mark.parametrize(
    "name, text, path",
    [
        ("workflow listener", LISTENER, WORKFLOW_PATH),
        ("shell router", ROUTER, "route.sh"),
        ("python scanner", SCANNER, "scan.py"),
    ],
)
def test_the_originating_defect_is_flagged(name: str, text: str, path: str) -> None:
    found = mod.violations(text, path)
    assert len(found) == 1, name
    for missing in [item for item in REQUIRED if item != "failure"]:
        assert f"'{missing}'" in found[0][1], name


# ── workflow expressions ─────────────────────────────────────────────────
def _gate(expression: str) -> str:
    return (
        "name: Notify\non: [workflow_run]\njobs:\n  notify:\n"
        f"    if: {expression}\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: ./notify.sh\n"
    )


@pytest.mark.parametrize(
    "name, expression",
    [
        ("equality", "github.event.workflow_run.conclusion == 'failure'"),
        ("reversed operands", "'failure' == github.event.workflow_run.conclusion"),
        ("double quotes", 'github.event.workflow_run.conclusion == "failure"'),
        (
            "wrapped in an expression",
            "${{ github.event.workflow_run.conclusion == 'failure' }}",
        ),
        (
            "a fromJSON list that is still short",
            'contains(fromJSON(\'["failure","timed_out"]\'), '
            "github.event.workflow_run.conclusion)",
        ),
        (
            "a space-separated haystack",
            "contains('failure cancelled', github.event.workflow_run.conclusion)",
        ),
        (
            "a step's own conclusion",
            "steps.build.conclusion == 'failure'",
        ),
    ],
)
def test_a_short_workflow_gate_is_flagged(name: str, expression: str) -> None:
    assert len(mod.violations(_gate(expression), WORKFLOW_PATH)) == 1, name


@pytest.mark.parametrize(
    "name, expression",
    [
        ("a success test", "github.event.workflow_run.conclusion == 'success'"),
        (
            "the negation of success",
            "github.event.workflow_run.conclusion != 'success'",
        ),
        (
            "a supersession test",
            "github.event.workflow_run.conclusion == 'cancelled'",
        ),
        (
            "one specific cause",
            "github.event.workflow_run.conclusion == 'startup_failure'",
        ),
        ("a job result, not a run conclusion", "needs.build.result == 'failure'"),
        ("no conclusion at all", "github.event_name == 'push'"),
    ],
)
def test_a_gate_that_is_not_classifying_red_passes(name: str, expression: str) -> None:
    assert mod.violations(_gate(expression), WORKFLOW_PATH) == [], name


def test_the_finding_lands_on_the_comparison_inside_a_folded_scalar() -> None:
    """A folded scalar spans many lines and PyYAML reports only where the block
    starts, so the line must come from the comparison a reader edits."""
    text = (
        "name: Notify\non: [workflow_run]\njobs:\n  notify:\n"
        "    if: >-\n"
        "      github.event.workflow_run.event == 'push' &&\n"
        "      github.event.workflow_run.conclusion == 'failure'\n"
        "    runs-on: ubuntu-latest\n    steps:\n      - run: ./notify.sh\n"
    )
    assert [line for line, _ in mod.violations(text, WORKFLOW_PATH)] == [7]


def test_an_unparseable_workflow_is_a_finding_not_a_pass() -> None:
    """`no findings` on the very file under test is the false green this pack
    refuses."""
    found = mod.violations("jobs:\n  a: [\n", WORKFLOW_PATH)
    assert len(found) == 1
    assert "could not parse as YAML" in found[0][1]


def test_a_yaml_file_outside_the_workflow_directories_is_not_scanned() -> None:
    assert mod.violations(_gate("x.conclusion == 'failure'"), "config/app.yaml") == []


# ── shell ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("a double-bracket test", 'if [[ "$conclusion" == "failure" ]]; then n; fi\n'),
        ("a single-bracket test", 'if [ "$conclusion" = failure ]; then n; fi\n'),
        ("an inequality", 'if [[ "$conclusion" != "failure" ]]; then n; fi\n'),
        ("an uppercase variable", 'if [[ "$CONCLUSION" == failure ]]; then n; fi\n'),
        ("a prefixed variable", 'if [[ "$run_conclusion" == failure ]]; then n; fi\n'),
        (
            "a substitution read inline",
            'if [[ "$(gh run view --json conclusion -q .conclusion)" == failure ]]; '
            "then n; fi\n",
        ),
        ("a case arm", 'case "$conclusion" in\n  failure) n ;;\nesac\n'),
        ("a bare test outside any if", '[[ "$conclusion" == failure ]] && n\n'),
    ],
)
def test_a_short_shell_classifier_is_flagged(name: str, src: str) -> None:
    assert len(mod.violations(src, "route.sh")) == 1, name


@pytest.mark.parametrize(
    "name, src",
    [
        # The two probes: text a command prints, and data written to a file.
        (
            "a logger's message string",
            'gb_warn "the conclusion was failure, not success"\n',
        ),
        (
            "a heredoc body",
            "cat <<'EOF' >doc.txt\nif [[ \"$conclusion\" == failure ]]; then n; fi\nEOF\n",
        ),
        ("a comment", '# [[ "$conclusion" == failure ]] would be too narrow\n'),
        ("the negation of success", 'if [[ "$conclusion" != success ]]; then n; fi\n'),
        ("a supersession test", 'if [[ "$conclusion" == cancelled ]]; then s; fi\n'),
        (
            "one specific cause",
            'if [[ "$conclusion" == startup_failure ]]; then s; fi\n',
        ),
        (
            "a variable that holds no conclusion",
            'if [[ "$state" == failure ]]; then n; fi\n',
        ),
        ("a computed comparand", 'if [[ "$conclusion" == "$want" ]]; then n; fi\n'),
    ],
)
def test_a_shell_site_that_is_not_classifying_red_passes(name: str, src: str) -> None:
    assert mod.violations(src, "route.sh") == [], name


def test_an_elif_chain_is_one_decision() -> None:
    """A classifier spread over `elif` branches is complete; judging each branch
    on its own would report the complete classifier as three partial ones."""
    src = (
        'if [[ "$conclusion" == failure ]]; then a\n'
        'elif [[ "$conclusion" == timed_out || "$conclusion" == startup_failure ]]; then b\n'
        'elif [[ "$conclusion" == action_required ]]; then c\n'
        "fi\n"
    )
    assert mod.violations(src, "route.sh") == []


def test_a_nested_if_is_judged_on_its_own() -> None:
    """Non-vacuity for the grouping: an inner decision must not be absorbed by
    the outer chain's literals, or a real subset would hide inside a complete
    classifier."""
    src = (
        'if [[ "$conclusion" == failure || "$conclusion" == timed_out ]]; then\n'
        '  if [[ "$conclusion" == failure ]]; then a; fi\n'
        'elif [[ "$conclusion" == startup_failure ]]; then b\n'
        'elif [[ "$conclusion" == action_required ]]; then c\n'
        "fi\n"
    )
    assert [line for line, _ in mod.violations(src, "route.sh")] == [2]


def test_a_shell_file_is_only_read_as_shell() -> None:
    """A shell path with no shell suffix is recognized by its shebang."""
    src = '#!/usr/bin/env bash\nif [[ "$conclusion" == failure ]]; then n; fi\n'
    assert [line for line, _ in mod.violations(src, "bin/route")] == [2]


# ── Python ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        ("a subscript", 'if run["conclusion"] == "failure":\n    n()\n'),
        ("a get call", 'if run.get("conclusion") == "failure":\n    n()\n'),
        ("an attribute", 'if run.conclusion == "failure":\n    n()\n'),
        ("a plain name", 'if conclusion == "failure":\n    n()\n'),
        ("reversed operands", 'if "failure" == conclusion:\n    n()\n'),
        ("a membership test", 'if conclusion in {"failure", "timed_out"}:\n    n()\n'),
        ("a tuple", 'if conclusion in ("failure",):\n    n()\n'),
        ("a module constant", 'RED = {"failure"}\nif conclusion in RED:\n    n()\n'),
        (
            "a frozenset constant",
            'RED = frozenset({"failure"})\nif conclusion in RED:\n    n()\n',
        ),
        ("a bare expression", 'flag = run["conclusion"] == "failure"\n'),
    ],
)
def test_a_short_python_classifier_is_flagged(name: str, src: str) -> None:
    assert len(mod.violations(src, "scan.py")) == 1, name


@pytest.mark.parametrize(
    "name, src",
    [
        ("a success test", 'if conclusion == "success":\n    ok()\n'),
        ("the negation of success", 'if conclusion != "success":\n    n()\n'),
        ("a not-in test", 'if conclusion not in ("success", "cancelled"):\n    n()\n'),
        ("a supersession test", 'if conclusion == "cancelled":\n    skip()\n'),
        ("one specific cause", 'if run["conclusion"] == "startup_failure":\n    s()\n'),
        ("a different key", 'if run["status"] == "failure":\n    n()\n'),
        ("a string that only mentions it", 'msg = "conclusion was failure"\n'),
    ],
)
def test_a_python_site_that_is_not_classifying_red_passes(name: str, src: str) -> None:
    assert mod.violations(src, "scan.py") == [], name


def test_a_python_elif_chain_is_one_decision() -> None:
    src = (
        'if conclusion == "failure":\n    a()\n'
        'elif conclusion in ("timed_out", "startup_failure"):\n    b()\n'
        'elif conclusion == "action_required":\n    c()\n'
    )
    assert mod.violations(src, "scan.py") == []


def test_a_python_file_that_does_not_parse_still_reports_its_real_lines() -> None:
    """`_py_ast.trees` falls back to a per-line parse, so a half-written edit
    does not turn the file into a silent pass."""
    src = 'def broken(\nif run["conclusion"] == "failure":\n    n()\n'
    assert [line for line, _ in mod.violations(src, "scan.py")] == [2]


# ── the opt-out ──────────────────────────────────────────────────────────
def test_an_annotated_consumer_passes() -> None:
    src = (
        'if run["conclusion"] == "failure":  '
        "# allow-conclusion-subset: these are job records\n    n()\n"
    )
    assert mod.violations(src, "scan.py") == []


def test_the_annotation_works_on_the_line_above() -> None:
    src = (
        "# allow-conclusion-subset: these are job records\n"
        'if run["conclusion"] == "failure":\n    n()\n'
    )
    assert mod.violations(src, "scan.py") == []


def test_an_annotation_with_no_reason_does_not_suppress() -> None:
    src = 'if run["conclusion"] == "failure":  # allow-conclusion-subset\n    n()\n'
    assert len(mod.violations(src, "scan.py")) == 1


# ── the repository override ──────────────────────────────────────────────
def test_no_config_file_leaves_the_default_set(tmp_path: Path) -> None:
    assert mod.required_set(tmp_path / "absent.yml") == mod.TERMINAL_RED


def test_an_override_widens_the_set_for_every_surface(tmp_path: Path) -> None:
    config = tmp_path / "conclusion-coverage.yml"
    config.write_text("extra: [stale]\n", encoding="utf-8")
    required = mod.required_set(config)
    assert required == mod.TERMINAL_RED | {"stale"}
    for build, path in BUILDERS.values():
        # The set that passed under the default is now a strict subset.
        found = mod.violations(build(REQUIRED), path, required)
        assert len(found) == 1
        assert "'stale'" in found[0][1]
        assert mod.violations(build([*REQUIRED, "stale"]), path, required) == []


def test_an_empty_override_file_is_not_an_error(tmp_path: Path) -> None:
    config = tmp_path / "conclusion-coverage.yml"
    config.write_text("# nothing to add yet\n", encoding="utf-8")
    assert mod.required_set(config) == mod.TERMINAL_RED


@pytest.mark.parametrize(
    "name, body",
    [
        ("a list instead of a mapping", "- stale\n"),
        ("an unknown key", "extras: [stale]\n"),
        ("a scalar value", "extra: stale\n"),
        ("a name GitHub never returns", "extra: [red]\n"),
        ("unparseable YAML", "extra: [\n"),
    ],
)
def test_a_malformed_override_raises(tmp_path: Path, name: str, body: str) -> None:
    """Loud, never ignored: a repository that wrote this file meant to widen the
    set, and a typo that widened nothing leaves the tree passing a check it
    believes is stricter than it is."""
    config = tmp_path / "conclusion-coverage.yml"
    config.write_text(body, encoding="utf-8")
    with pytest.raises(mod.ConfigError):
        mod.required_set(config)


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_conclusion_coverage.py"), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
    )


def test_main_reports_the_path_and_line_and_exits_one(tmp_path: Path) -> None:
    scanner = tmp_path / "scan.py"
    scanner.write_text(SCANNER, encoding="utf-8")
    result = _run(str(scanner), cwd=tmp_path)
    assert result.returncode == 1
    assert f"{scanner}:4:" in result.stderr
    assert "timed_out" in result.stderr


def test_main_exits_zero_on_a_complete_classifier(tmp_path: Path) -> None:
    scanner = tmp_path / "scan.py"
    scanner.write_text(_python(REQUIRED), encoding="utf-8")
    assert _run(str(scanner), cwd=tmp_path).returncode == 0


def test_main_refuses_an_empty_file_list(tmp_path: Path) -> None:
    """Exit 2, not 0: a scan over nothing must never read as a clean pass."""
    result = _run(cwd=tmp_path)
    assert result.returncode == 2
    assert "no files to scan" in result.stderr


def test_main_reads_the_override_from_the_repository_root(tmp_path: Path) -> None:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "conclusion-coverage.yml").write_text(
        "extra: [stale]\n", encoding="utf-8"
    )
    scanner = tmp_path / "scan.py"
    scanner.write_text(_python(REQUIRED), encoding="utf-8")
    result = _run(str(scanner), cwd=tmp_path)
    assert result.returncode == 1
    assert "'stale'" in result.stderr


def test_a_shell_file_the_grammar_refuses_fails_loudly(tmp_path: Path) -> None:
    script = tmp_path / "huge.sh"
    script.write_text("cmd |" * 3000, encoding="utf-8")
    result = _run(str(script), cwd=tmp_path)
    assert result.returncode == 1
    assert "pipe bytes" in result.stderr


def test_main_skips_a_path_that_vanished(tmp_path: Path) -> None:
    """A deleted path in pre-commit's own file list is a rename race, not a
    verdict about content."""
    assert _run(str(tmp_path / "gone.py"), cwd=tmp_path).returncode == 0


# ── Python constant bindings ─────────────────────────────────────────────
def test_an_annotated_constant_is_resolved() -> None:
    """`RED: frozenset[str] = ...` is an `ast.AnnAssign`, not an `ast.Assign`.
    Reading only the plain spelling would hide exactly the subset this check
    rejects, because the comparison would show no literals at all."""
    src = (
        "RED: frozenset[str] = frozenset({'failure'})\n"
        "def is_broken(run):\n"
        '    return run["conclusion"] in RED\n'
    )
    assert [line for line, _ in mod.violations(src, "scan.py")] == [3]


def test_an_annotated_constant_naming_the_whole_set_passes() -> None:
    src = (
        f"RED: frozenset[str] = frozenset({REQUIRED!r})\n"
        "def is_broken(run):\n"
        '    return run["conclusion"] in RED\n'
    )
    assert mod.violations(src, "scan.py") == []


def test_one_function_s_constant_does_not_answer_for_another_s() -> None:
    """Two functions may each bind their own `RED`. Flattening the module would
    let the second, complete one excuse the first, partial one."""
    src = (
        "def narrow(run):\n"
        '    RED = {"failure"}\n'
        '    return run["conclusion"] in RED\n'
        "def wide(run):\n"
        f"    RED = set({REQUIRED!r})\n"
        '    return run["conclusion"] in RED\n'
    )
    assert [line for line, _ in mod.violations(src, "scan.py")] == [3]


def test_a_function_inherits_the_module_s_constant() -> None:
    src = (
        'RED = {"failure"}\ndef is_broken(run):\n    return run["conclusion"] in RED\n'
    )
    assert [line for line, _ in mod.violations(src, "scan.py")] == [3]


def test_a_local_binding_shadows_the_module_s() -> None:
    src = (
        f"RED = set({REQUIRED!r})\n"
        "def narrow(run):\n"
        '    RED = {"failure"}\n'
        '    return run["conclusion"] in RED\n'
    )
    assert [line for line, _ in mod.violations(src, "scan.py")] == [4]


# ── widening the set re-verifies the tree ────────────────────────────────
def _consumer_tree(root: Path) -> Path:
    """A repository holding one consumer that names the whole default set."""
    init_test_repo(root)
    (root / ".github").mkdir(parents=True)
    (root / "scan.py").write_text(_python(REQUIRED), encoding="utf-8")
    commit_all(root, "consumer")
    return root / ".github" / "conclusion-coverage.yml"


def test_widening_the_set_rechecks_every_tracked_consumer(tmp_path: Path) -> None:
    """The commit that widens the set changes NO consumer, so a changed-file run
    would scan none of them and report a clean pass over the very tree the new
    set just invalidated."""
    config = _consumer_tree(tmp_path)
    config.write_text("extra: [stale]\n", encoding="utf-8")
    result = _run(str(config.relative_to(tmp_path)), cwd=tmp_path)
    assert result.returncode == 1
    assert "scan.py:4:" in result.stderr
    assert "'stale'" in result.stderr
    assert "every tracked consumer" in result.stderr


def test_the_override_file_alone_is_not_read_as_a_consumer(tmp_path: Path) -> None:
    """Non-vacuity for the case above: the rescan is what finds the consumer,
    not the config file being scanned as one."""
    config = _consumer_tree(tmp_path)
    config.write_text("# no extras\n", encoding="utf-8")
    result = _run(str(config.relative_to(tmp_path)), cwd=tmp_path)
    assert result.returncode == 0
    assert "every tracked consumer" in result.stderr


def test_a_changed_consumer_alone_does_not_trigger_the_rescan(tmp_path: Path) -> None:
    scanner = tmp_path / "scan.py"
    scanner.write_text(_python(REQUIRED), encoding="utf-8")
    result = _run(str(scanner), cwd=tmp_path)
    assert result.returncode == 0
    assert "every tracked consumer" not in result.stderr

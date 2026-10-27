"""Example-based tests (mutation oracle) for ci_truth_serum/_cts_step_reach.py — the
walk from a `run:` body to every repo script it sources or runs.

Pins the contracts `check_uncached_download` depends on: which spellings of a
path resolve, which commands count as executed, and where the walk stops."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

sr = load_hook("_cts_step_reach.py", "cts_step_reach")


def write(root: Path, rel: str, body: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def commands(reached, name: str) -> list[list[str]]:
    """The words of every executed command whose first word is NAME."""
    return [
        sr.tokens(command)
        for namespace in reached.namespaces
        for command, _ in namespace.executed
        if sr.tokens(command)[:1] == [name]
    ]


def echo_value(root: Path, script: str) -> str | None:
    """The value of the first argument of the first `echo` SCRIPT runs, with
    SCRIPT written at ROOT/scripts/run.sh."""
    path = write(root, "scripts/run.sh", script + "\n")
    namespace = sr.Namespace(root, sr._unit(path, path), None)
    echo = next(c for c, _ in namespace.executed if sr.tokens(c)[0] == "echo")
    return namespace.value(sr.command_arguments(echo)[1], namespace.units[0])


# ── values ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('D="${BASH_SOURCE[0]%/*}"\necho "$D/x"', "scripts/x"),
        ('D="$(dirname "${BASH_SOURCE[0]}")"\necho "$D/x"', "scripts/x"),
        ('D="$(dirname "$0")"\necho "$D/x"', "scripts/x"),
        ('D="$(cd "$(dirname "$0")/.." && pwd)"\necho "$D/x"', "x"),
        ('D="$(git rev-parse --show-toplevel)"\necho "$D/x"', "x"),
        ('echo "$GITHUB_WORKSPACE/x"', "x"),
    ],
    ids=[
        "bash-source-strip",
        "dirname-bash-source",
        "dirname-0",
        "cd-pwd",
        "toplevel",
        "workspace",
    ],
)
def test_a_path_the_text_fixes_resolves(tmp_path, script, expected):
    value = echo_value(tmp_path, script)
    assert value is not None
    assert Path(value).resolve() == (tmp_path / expected).resolve()


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ('echo "${UNSET:-/opt}/x"', "/opt/x"),
        ("V=1.2\necho 'a'\"b-${V}\"", "ab-1.2"),
        ('V=1.2\necho "v$V"', "v1.2"),
    ],
    ids=["default", "concatenation", "simple-expansion"],
)
def test_a_literal_value_resolves(tmp_path, script, expected):
    assert echo_value(tmp_path, script) == expected


@pytest.mark.parametrize(
    "script",
    [
        'echo "$1/x"',
        'D=a\nD=b\necho "$D/x"',
        'D="$(date)"\necho "$D/x"',
        'echo "${D:-/opt}/x"\nD=a\nD=b',
        'echo "${BASH_SOURCE[1]}"',
        'echo "${D%.sh}"\nD=a.sh',
    ],
    ids=[
        "positional",
        "assigned-twice",
        "computed",
        "default-of-an-assigned-name",
        "other-index",
        "other-operator",
    ],
)
def test_a_value_only_the_run_knows_is_none(tmp_path, script):
    assert echo_value(tmp_path, script) is None


def test_a_run_body_has_no_bash_source(tmp_path):
    reached = sr.reach('echo "${BASH_SOURCE[0]}" "$0"', tmp_path, None)
    namespace = reached.namespaces[0]
    echo, unit = namespace.executed[0]
    args = sr.command_arguments(echo)[1:]
    assert [namespace.value(arg, unit) for arg in args] == [None, None]


def test_a_file_outside_the_repo_is_not_followed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    write(tmp_path, "outside.sh", "curl one\n")
    reached = sr.reach("bash ../outside.sh", root, None)
    assert len(reached.namespaces) == 1


# ── what runs ───────────────────────────────────────────────────────────────


def test_sourced_files_join_the_namespace_and_called_bodies_run(tmp_path):
    write(tmp_path, "lib.bash", "f() { curl one; }\ng() { curl two; }\n")
    write(tmp_path, "run.sh", "source lib.bash\nf\n")
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == [["curl", "one"]]
    assert [len(n.units) for n in reached.namespaces] == [1, 2]


def test_a_function_named_as_an_argument_runs(tmp_path):
    """`retry f` runs f through its wrapper."""
    write(tmp_path, "run.sh", "f() { curl one; }\nretry f\n")
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == [["curl", "one"]]


def test_a_call_before_its_definition_is_sourced_still_runs(tmp_path):
    write(tmp_path, "lib.bash", "f() { curl one; }\n")
    write(tmp_path, "run.sh", "trap f EXIT\nsource lib.bash\n")
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == [["curl", "one"]]


def test_a_nested_definition_runs_only_when_called(tmp_path):
    write(tmp_path, "run.sh", "outer() { inner() { curl one; }; }\nouter\n")
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == []


def test_a_shellcheck_directive_resolves_an_unresolvable_source(tmp_path):
    write(tmp_path, "lib/x.bash", "curl one\n")
    write(tmp_path, "lib/run.sh", '# shellcheck source=x.bash\nsource "$LIB/x.bash"\n')
    reached = sr.reach("bash lib/run.sh", tmp_path, None)
    assert commands(reached, "curl") == [["curl", "one"]]


def test_an_absolute_directive_is_not_followed(tmp_path):
    write(tmp_path, "x.bash", "curl one\n")
    write(tmp_path, "run.sh", '# shellcheck source=/dev/null\nsource "$LIB/x.bash"\n')
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == []


@pytest.mark.parametrize(
    ("run", "name"),
    [
        ("bash a.sh", "a.sh"),
        ("sh -e a.sh", "a.sh"),
        ("./a.sh", "a.sh"),
        ("bash bin/tool", "bin/tool"),
    ],
    ids=["bash", "sh-with-option", "direct", "no-suffix"],
)
def test_an_executed_script_starts_a_child_namespace(tmp_path, run, name):
    write(tmp_path, name, "curl one\n")
    reached = sr.reach(run, tmp_path, None)
    assert len(reached.namespaces) == 2
    assert commands(reached, "curl") == [["curl", "one"]]


def test_a_string_handed_to_a_shell_is_not_a_file(tmp_path):
    write(tmp_path, "a.sh", "curl one\n")
    reached = sr.reach("bash -c a.sh", tmp_path, None)
    assert len(reached.namespaces) == 1


def test_the_walk_stops_after_max_hops(tmp_path):
    for index in range(sr.MAX_HOPS + 2):
        write(tmp_path, f"s{index}.sh", f"bash s{index + 1}.sh\n")
    reached = sr.reach("bash s0.sh", tmp_path, None)
    assert len(reached.namespaces) == sr.MAX_HOPS + 1


def test_a_script_that_runs_itself_is_walked_once(tmp_path):
    write(tmp_path, "a.sh", "bash a.sh\n")
    reached = sr.reach("bash a.sh\nbash a.sh", tmp_path, None)
    assert len(reached.namespaces) == 2


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        ("python3 tools/x.py", "tools/x.py"),
        ("python3 -I tools/x.py arg", "tools/x.py"),
        ("uv run tools/x.py", "tools/x.py"),
        ("uv run --frozen tools/x.py", "tools/x.py"),
        ("uv run python3 tools/x.py", "tools/x.py"),
        ("uv run python -m tools.x", "tools/x.py"),
        ("python -m tools.x", "tools/x.py"),
    ],
    ids=[
        "python3",
        "option",
        "uv-run",
        "uv-run-option",
        "uv-run-python",
        "uv-run-module",
        "module",
    ],
)
def test_a_python_file_handed_to_an_interpreter_is_recorded(tmp_path, run, expected):
    write(tmp_path, "tools/x.py", "")
    reached = sr.reach(run, tmp_path, None)
    assert reached.python == (tmp_path / expected,)


@pytest.mark.parametrize(
    "run",
    [
        "python3 -c 'import x' tools/x.py",
        'python3 "$NOPE/x.py"',
        "python3 tools/gone.py",
        "uv run pytest tools/x.py",
        "uv run --frozen ruff check tools/x.py",
    ],
    ids=["dash-c", "unresolvable", "missing", "uv-run-pytest", "uv-run-ruff"],
)
def test_a_python_file_the_text_does_not_fix_is_not_recorded(tmp_path, run):
    write(tmp_path, "tools/x.py", "")
    assert sr.reach(run, tmp_path, None).python == ()


@pytest.mark.parametrize(
    "run",
    ['bash "$GITHUB_ACTION_PATH/x.sh"', "bash ${{ github.action_path }}/x.sh"],
    ids=["variable", "expression"],
)
def test_a_composite_action_path_resolves(tmp_path, run):
    write(tmp_path, ".github/actions/a/x.sh", "curl one\n")
    reached = sr.reach(run, tmp_path, ".github/actions/a")
    assert commands(reached, "curl") == [["curl", "one"]]


def test_a_second_assignment_in_a_sourced_file_unfixes_a_value(tmp_path):
    """The run body fixes V, then sources a file that assigns it again: no
    single value holds, even though V read as fixed before the source."""
    write(tmp_path, "pin.bash", "V=1\n")
    reached = sr.reach('V=2\nsource "pin$V.bash"\nsource pin.bash', tmp_path, None)
    namespace = reached.namespaces[0]
    assert namespace.assigned_in("V") == {tmp_path / "pin.bash"}
    assert namespace.variable("V", namespace.units[0]) is None

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
    namespace = sr.Namespace(root, sr._unit(path, path), None, root)
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


@pytest.mark.parametrize(
    ("environment", "expected"),
    [(frozenset({"CACHE_DIR"}), None), (frozenset({"OTHER"}), "/home/c/x")],
    ids=["set-by-the-step", "unset"],
)
def test_a_default_reads_only_when_the_step_environment_leaves_it_unset(
    tmp_path, environment, expected
):
    """The step's `env:` starts the process with CACHE_DIR set, so the default
    is not its value. A child script inherits the same environment."""
    write(tmp_path, "child.sh", 'echo "${CACHE_DIR:-/home/c}/x"\n')
    reached = sr.reach("bash child.sh", tmp_path, None, "", environment)
    namespace = reached.namespaces[1]
    echo, unit = namespace.executed[0]
    assert namespace.value(sr.command_arguments(echo)[1], unit) == expected


def namespace_of(root: Path, script: str):
    path = write(root, "scripts/run.sh", script + "\n")
    return sr.Namespace(root, sr._unit(path, path), None, root)


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        (
            (
                'read -r -a hosts <<<"${HOSTS:-https://a/x https://b/y}"\n'
                'for h in "${hosts[@]}"; do :; done'
            ),
            ["https://a/x", "https://b/y"],
        ),
        (
            'hosts=(https://a/x "https://b/y")\nfor h in "${hosts[@]}" c; do :; done',
            ["https://a/x", "https://b/y", "c"],
        ),
        ("for h in a 'b c'; do :; done", ["a", "b c"]),
        ('read -r -a hosts\nfor h in "${hosts[@]}"; do :; done', None),
        ('hosts=(a)\nhosts+=(b)\nfor h in "${hosts[@]}"; do :; done', None),
        ('for h in "$(cat f)"; do :; done', None),
        ("for h in a; do :; done\nfor h in b; do :; done", None),
        ("h=z\nfor h in a; do :; done", None),
        ("for h; do :; done", None),
    ],
    ids=[
        "read-a-from-a-here-string",
        "array-assignment-then-a-word",
        "literal-words",
        "read-a-from-stdin",
        "array-appended",
        "computed-word",
        "two-loops",
        "also-assigned",
        "positional-parameters",
    ],
)
def test_a_loop_variable_reads_the_words_the_text_fixes(tmp_path, script, expected):
    """`for h in WORDS` gives `h` each word in turn. The words are known only
    when one loop binds `h`, nothing assigns it, and the text fixes each word."""
    namespace = namespace_of(tmp_path, script)
    assert namespace.loop_items("h", namespace.units[0].root) == expected


@pytest.mark.parametrize(
    ("declare", "use", "expected"),
    [
        ("local url", 'echo "$url"', None),
        ("", 'echo "$url"', ["https://e/1.2.3/t"]),
        ("local url", "helper", ["https://e/1.2.3/t"]),
    ],
    ids=["local-loop-outside", "global-loop-outside", "local-loop-inside"],
)
def test_a_loop_in_a_function_binds_a_local_name_only_inside_it(
    tmp_path, declare, use, expected
):
    """A loop over a `local url` binds nothing outside its function. Without
    `local`, the loop sets the global `url`."""
    namespace = namespace_of(
        tmp_path,
        f'helper() {{ {declare}\nfor url in https://e/1.2.3/t; do echo "$url"; done; }}\n'
        f"read -r url <f\n{use}",
    )
    inside = use == "helper"
    echo = next(
        c
        for c, _ in namespace.executed
        if sr.tokens(c)[0] == "echo"
        and (sr._ancestor_of(c, "function_definition") is not None) == inside
    )
    assert namespace.loop_items("url", echo) == expected


@pytest.mark.parametrize(
    ("declare", "expected"),
    [
        ("local url", ['url="https://a/1"']),
        ("", ['url="https://a/1"', 'url="$1"']),
    ],
    ids=["local", "global"],
)
def test_a_local_declaration_hides_the_assignments_outside_its_function(
    tmp_path, declare, expected
):
    """A function that runs `local url` reads only its own `url`. Without the
    declaration, an assignment in any function can be the value it reads."""
    namespace = namespace_of(
        tmp_path,
        f'get() {{ {declare}\nurl="https://a/1"\necho "$url"; }}\n'
        'put() { url="$1"; }\nget\nput x',
    )
    echo = next(c for c, _ in namespace.executed if sr.tokens(c)[0] == "echo")
    seen = namespace.assignments_at("url", echo)
    assert [sr.node_text(node) for node, _ in seen] == expected


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


@pytest.mark.parametrize(
    "line",
    ['gb_warn "run f now"', "echo f", "cat <<'EOF' > doc.txt\nf\nEOF"],
    ids=["message-string", "printed-word", "heredoc-body"],
)
def test_a_function_name_in_text_is_not_a_call(tmp_path, line):
    """A message, a printed word and a heredoc body are text no shell runs."""
    write(tmp_path, "run.sh", f"f() {{ curl one; }}\n{line}\n")
    reached = sr.reach("bash run.sh", tmp_path, None)
    assert commands(reached, "curl") == []


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
        ("bash -eo pipefail a.sh", "a.sh"),
        ("timeout --kill-after=30 900 bash a.sh", "a.sh"),
        ("timeout -k 30 60s ./a.sh", "a.sh"),
        ('timeout "$CAP" bash a.sh', "a.sh"),
        ("sudo -u root -E bash a.sh", "a.sh"),
        ("env -u HOME A=1 bash a.sh", "a.sh"),
        ("nohup exec bash a.sh", "a.sh"),
        ("sudo -uadmin bash a.sh", "a.sh"),
        ("sudo -Eu admin bash a.sh", "a.sh"),
    ],
    ids=[
        "bash",
        "sh-with-option",
        "direct",
        "no-suffix",
        "option-with-a-value",
        "timeout",
        "timeout-short-option",
        "timeout-computed-duration",
        "sudo",
        "env",
        "two-wrappers",
        "sudo-attached-value",
        "sudo-value-ends-a-cluster",
    ],
)
def test_an_executed_script_starts_a_child_namespace(tmp_path, run, name):
    write(tmp_path, name, "curl one\n")
    reached = sr.reach(run, tmp_path, None)
    assert len(reached.namespaces) == 2
    assert commands(reached, "curl") == [["curl", "one"]]


@pytest.mark.parametrize(
    "run",
    [
        "command -v bash a.sh",
        "grep bash a.sh",
        "sudo bash -c a.sh",
        "timeout bash a.sh",
        "env -C sub bash a.sh",
        "env --chdir=sub bash a.sh",
        "sudo -D sub bash a.sh",
        "env -iC sub bash a.sh",
        "sudo -i bash a.sh",
        "sudo --login bash a.sh",
    ],
    ids=[
        "lookup",
        "argument-of-another-program",
        "string",
        "timeout-with-no-duration",
        "env-chdir",
        "env-chdir-long",
        "sudo-chdir",
        "env-chdir-in-a-cluster",
        "sudo-login",
        "sudo-login-long",
    ],
)
def test_a_wrapper_hides_no_script_and_invents_none(tmp_path, run):
    """`command -v` only prints, `grep` reads the file as text, `-c` hands the
    shell a string, and a `timeout` with no duration runs nothing. A wrapper
    that changes directory runs `sub/a.sh`, which the walk does not track. A
    `sudo -i` login shell starts in the target user's home."""
    write(tmp_path, "a.sh", "curl one\n")
    reached = sr.reach(run, tmp_path, None)
    assert len(reached.namespaces) == 1


@pytest.mark.parametrize(
    "functions",
    [
        'retry() { "$@"; }',
        'retry() { timeout 60 "$@" || return 1; }',
        'retry() { out="$("${@}")"; }',
        'attempt() { "$@"; }\nretry() { shift; attempt "$@"; }',
    ],
    ids=["direct", "behind-a-wrapper", "in-a-substitution", "through-a-call"],
)
def test_a_function_that_runs_its_arguments_runs_the_script(tmp_path, functions):
    write(tmp_path, "a.sh", "curl one\n")
    reached = sr.reach(f"{functions}\nretry bash a.sh", tmp_path, None)
    assert commands(reached, "curl") == [["curl", "one"]]


@pytest.mark.parametrize(
    "functions",
    [
        "",
        'retry() { source "$1"; }',
        'retry() { echo "$@"; }',
        'retry() { attempt "$@"; }\nattempt() { echo "$@"; }',
        'retry() { attempt "$@"; }\nattempt() { retry "$@"; }',
    ],
    ids=["undefined", "source", "printed", "through-a-printer", "cycle"],
)
def test_a_function_that_does_not_run_its_arguments_runs_no_script(tmp_path, functions):
    """`retry bash a.sh` launches a.sh only when `retry` runs `"$@"`."""
    write(tmp_path, "a.sh", "curl one\n")
    reached = sr.reach(f"{functions}\nretry bash a.sh", tmp_path, None)
    assert commands(reached, "curl") == []


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
        ("uv run --with requests tools/x.py", "tools/x.py"),
        ("uv run python3 tools/x.py", "tools/x.py"),
        ("uv run python -m tools.x", "tools/x.py"),
        ("python -m tools.x", "tools/x.py"),
        ("sudo -E python3 tools/x.py", "tools/x.py"),
    ],
    ids=[
        "python3",
        "option",
        "uv-run",
        "uv-run-option",
        "uv-run-option-with-a-value",
        "uv-run-python",
        "uv-run-module",
        "module",
        "behind-a-wrapper",
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
        "env -C sub python3 tools/x.py",
        "sudo -i python3 tools/x.py",
    ],
    ids=[
        "dash-c",
        "unresolvable",
        "missing",
        "uv-run-pytest",
        "uv-run-ruff",
        "env-chdir",
        "sudo-login",
    ],
)
def test_a_python_file_the_text_does_not_fix_is_not_recorded(tmp_path, run):
    write(tmp_path, "tools/x.py", "")
    assert sr.reach(run, tmp_path, None).python == ()


@pytest.mark.parametrize(
    "run",
    [
        'bash "$GITHUB_ACTION_PATH/x.sh"',
        "bash ${{ github.action_path }}/x.sh",
        "bash ${{ github.workspace }}/.github/actions/a/x.sh",
    ],
    ids=["variable", "expression", "workspace-expression"],
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


def test_a_string_keeps_the_blanks_between_two_expansions(tmp_path):
    """The grammar folds `  ` into the second expansion's node."""
    path = tmp_path / "s.sh"
    path.write_text('A=x\nB=y\necho "${A}  ${B}"\n', encoding="utf-8")
    namespace = sr.Namespace(tmp_path, sr._unit(path, path), None, tmp_path)
    command = namespace.executed[-1][0]
    assert namespace.words(command, namespace.executed[-1][1]) == ["echo", "x  y"]


def test_a_cd_inside_a_substitution_in_another_file_moves_nothing(tmp_path):
    """`D="$(cd … && pwd)"` runs its `cd` in the substitution's own shell, so a
    relative `bash setup.bash` in another file still resolves."""
    (tmp_path / "lib.sh").write_text(
        'D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\nrun_setup() { bash setup.bash; }\n',
        encoding="utf-8",
    )
    (tmp_path / "setup.bash").write_text("true\n", encoding="utf-8")
    reached = sr.reach("source lib.sh\nrun_setup\n", tmp_path, None)
    assert [ns.units[0].path for ns in reached.namespaces] == [
        None,
        tmp_path / "setup.bash",
    ]


def test_a_cd_in_a_process_substitution_does_not_move_the_shell(tmp_path):
    (tmp_path / "install.sh").write_text("true\n", encoding="utf-8")
    reached = sr.reach(
        "diff <(cd a && ls) <(cd b && ls)\nbash install.sh\n", tmp_path, None
    )
    assert [ns.units[0].path for ns in reached.namespaces] == [
        None,
        tmp_path / "install.sh",
    ]


@pytest.mark.parametrize(
    "script",
    [
        "if ! { cd a; }; then bash install.sh; fi\n",
        "! { cd a; } && bash install.sh\n",
    ],
)
def test_a_negated_cd_runs_its_arm_from_the_old_directory(tmp_path, script):
    """The arm runs only when the `cd` failed, so the shell has not moved."""
    (tmp_path / "a").mkdir()
    (tmp_path / "install.sh").write_text("true\n", encoding="utf-8")
    (tmp_path / "a" / "install.sh").write_text("true\n", encoding="utf-8")
    reached = sr.reach(script, tmp_path, None)
    assert [ns.units[0].path for ns in reached.namespaces] == [
        None,
        tmp_path / "install.sh",
    ]


def test_a_call_chain_deeper_than_the_limit_has_no_directory(tmp_path):
    """Asked cold, a chain of calls deeper than `_MAX_CALL_DEPTH` gives no
    directory instead of running out of Python stack."""
    depth = 300
    script = "\n".join(
        ["f0() { curl -o t https://e/t; }"]
        + [f"f{i}() {{ f{i - 1}; }}" for i in range(1, depth)]
        + [f"f{depth - 1}"]
    )
    namespace = sr.reach(script, tmp_path, None).namespaces[0]
    namespace._called.clear()
    namespace._directories.clear()
    [(fetch, unit)] = [
        (c, u) for c, u in namespace.executed if sr.tokens(c)[0] == "curl"
    ]
    assert namespace.directory_at(fetch, unit) is None
    namespace._called.clear()
    namespace._directories.clear()
    [(call, unit)] = [
        (c, u) for c, u in namespace.executed if sr.tokens(c)[0] == f"f{depth - 2}"
    ]
    assert namespace.directory_at(call, unit) == tmp_path

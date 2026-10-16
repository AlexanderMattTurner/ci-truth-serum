"""Tests for ci_truth_serum/trace_ci.py — the tool that follows CI into the
makefiles it runs and reports where CI can pass over a failure.

The unit tests drive each parser directly. The end-to-end tests build a small
repository in a temporary directory, run the real GNU make against it, and read
the findings that `main` prints. GNU make and git must be on PATH: a suite that
skipped without them would report a green it did not earn.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import init_test_repo, load_hook

mod = load_hook("trace_ci.py", "trace_ci")

TAB = "\t"


def test_gnu_make_and_git_are_present() -> None:
    """The end-to-end cases below need both. Fail here rather than skip."""
    assert shutil.which("git"), "git is not on PATH"
    assert mod.gnu_make()


# ── discover ────────────────────────────────────────────────────────────


def test_discover_classifies_ci_files_by_path() -> None:
    found = mod.discover(
        [
            ".circleci/config.yml",
            "nested/.circleci/config.yml",
            ".github/workflows/ci.yaml",
            ".github/workflows/sub/not-a-workflow.yml",
            ".gitlab-ci.yml",
            "Makefile",
            "mk/common.mk",
            "apps/x/Dockerfile",
            "Jenkinsfile",
            "README.md",
        ]
    )
    assert found == {
        "circleci": [".circleci/config.yml", "nested/.circleci/config.yml"],
        "github-actions": [".github/workflows/ci.yaml"],
        "gitlab-ci": [".gitlab-ci.yml"],
        "make": ["Makefile", "mk/common.mk"],
        "dockerfile": ["apps/x/Dockerfile"],
        "jenkins": ["Jenkinsfile"],
    }


# ── CI steps ────────────────────────────────────────────────────────────

CIRCLECI = """\
version: 2.1
jobs:
  test:
    docker:
    - image: cimg/python:3.12
    steps:
    - checkout
    - run:
        name: two lines
        command: |
          cd apps/x
          make check
    - run: echo plain
  other:
    shell: /bin/sh
    working_directory: ~/project/sub
    steps:
    - run: make lint
"""


def test_circleci_steps_keep_the_source_line_of_each_command_line() -> None:
    steps, images = mod.circleci_steps(CIRCLECI, "c.yml")
    assert [(s.job, s.shell, s.lines, s.working_directory) for s in steps] == [
        ("test", mod.CIRCLECI_DEFAULT_SHELL, (11, 12), ""),
        ("test", mod.CIRCLECI_DEFAULT_SHELL, (13,), ""),
        ("other", "/bin/sh", (18,), "sub"),
    ]
    assert images == [mod.Image("c.yml", "test", "cimg/python:3.12", 5)]


def test_circleci_merge_keys_and_a_relative_step_directory() -> None:
    text = """\
defaults: &defaults
  working_directory: ~/project/app
  docker:
  - image: cimg/python:3.12
jobs:
  test:
    <<: *defaults
    steps:
    - run: make check
    - run:
        command: make lint
        working_directory: sub
"""
    steps, images = mod.circleci_steps(text, "c.yml")
    assert [s.working_directory for s in steps] == ["app", "app/sub"]
    assert images == [mod.Image("c.yml", "test", "cimg/python:3.12", 4)]


@pytest.mark.parametrize(
    ("job", "dirs"),
    [
        pytest.param(
            "    working_directory: ~/repo\n    steps:\n    - checkout\n"
            "    - run: make\n    - run:\n        command: make\n"
            "        working_directory: ~/repo/sub\n",
            ["", "sub"],
            id="the checkout goes to the job's directory",
        ),
        pytest.param(
            "    working_directory: ~/w\n    steps:\n    - checkout:\n"
            "        path: code\n    - run:\n        command: make\n"
            "        working_directory: code/app\n",
            ["app"],
            id="a checkout path is read from the job's directory",
        ),
        pytest.param(
            "    steps:\n    - checkout\n    - run:\n        command: make\n"
            "        working_directory: /tmp/x\n    - run:\n        command: make\n"
            "        working_directory: $HOME/x\n    - run:\n        command: make\n"
            "        working_directory: ..\n",
            [None, None, None],
            id="a directory outside the checkout or with an expansion is unknown",
        ),
        pytest.param(
            "    working_directory: rel\n    steps:\n    - run: make\n",
            [None],
            id="a relative job directory is unknown",
        ),
    ],
)
def test_circleci_steps_find_the_step_directory_in_the_checkout(
    job: str, dirs: list
) -> None:
    steps, _ = mod.circleci_steps(f"jobs:\n  j:\n{job}", "c.yml")
    assert [s.working_directory for s in steps] == dirs


def test_circleci_reads_a_reusable_command_at_each_call() -> None:
    text = """\
commands:
  lint:
    steps:
    - run: make lint
  unused:
    steps:
    - run: make never
jobs:
  test:
    working_directory: ~/project/app
    environment:
      MODE: ci
      TOKEN: $SECRET
    steps:
    - lint
    - when:
        condition: true
        steps:
        - lint:
            x: 1
    - run:
        command: make check
        environment:
          STEP: "1"
"""
    steps, _ = mod.circleci_steps(text, "c.yml")
    assert [(s.command, s.working_directory) for s in steps] == [
        ("make lint", "app"),
        ("make lint", "app"),
        ("make check", "app"),
    ]
    assert dict(steps[0].env) == {"CI": "true", "CIRCLECI": "true", "MODE": "ci"}
    assert dict(steps[2].env)["STEP"] == "1"


def test_a_circleci_command_that_calls_itself_is_refused() -> None:
    text = "commands:\n  a:\n    steps:\n    - a\njobs:\n  j:\n    steps:\n    - a\n"
    with pytest.raises(mod.TraceError, match="a -> a"):
        mod.circleci_steps(text, "c.yml")


def test_a_circleci_run_step_without_a_command_is_refused() -> None:
    text = "jobs:\n  j:\n    steps:\n    - run:\n        name: x\n"
    with pytest.raises(mod.TraceError, match="c.yml:4"):
        mod.circleci_steps(text, "c.yml")


def test_workflow_steps_read_the_environment() -> None:
    text = """\
on: push
env:
  A: top
  T: ${{ secrets.T }}
jobs:
  a:
    env:
      B: job
    steps:
    - run: make check
      env:
        A: step
"""
    (step,) = mod.workflow_steps(text, "w.yml")
    assert dict(step.env) == {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "A": "step",
        "B": "job",
    }


def test_workflow_steps_read_the_working_directory_defaults() -> None:
    text = """\
on: push
defaults:
  run:
    working-directory: top
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
    - run: make check
    - run: make lint
      working-directory: step
    - run: make lint
      working-directory: ../other
    - run: make lint
      working-directory: ${{ matrix.dir }}
"""
    steps = mod.workflow_steps(text, "w.yml")
    assert [(s.working_directory, s.lint) for s in steps] == [
        ("top", False),
        ("step", False),
        (None, False),
        (None, False),
    ]


def _step(command: str) -> "mod.CiStep":
    lines = tuple(range(1, len(command.splitlines()) + 1))
    return mod.CiStep("c.yml", "j", "/bin/bash", command, lines, "", True)


def test_make_calls_follow_a_plain_cd(tmp_path: Path) -> None:
    calls, untraced = mod.make_calls(_step("cd apps/x\nmake check lint"), tmp_path)
    assert [(c.cwd, c.args, c.line) for c in calls] == [
        ((tmp_path / "apps/x").resolve(), ("check", "lint"), 2)
    ]
    assert untraced == []


@pytest.mark.parametrize(
    "command",
    ["make $(TARGET)", 'cd "$DIR"\nmake check', "cd -\nmake check"],
)
def test_make_calls_name_a_call_they_cannot_resolve(
    command: str, tmp_path: Path
) -> None:
    calls, untraced = mod.make_calls(_step(command), tmp_path)
    assert calls == []
    assert untraced == [len(command.splitlines())]


@pytest.mark.parametrize(
    "command",
    [
        "make ${{ matrix.target }}",
        "make test-${{ matrix.py }}",
        "make '${{ matrix.target }}'",
        "make << parameters.target >>",
        "bash -c 'cd sub && make check'",
        'if [ -n "$CI" ]; then cd sub; fi; make check',
        "f() { cd sub; }; make check",
        "docker run img make check",
        "cd .. && make check",
        "make -C /tmp check",
    ],
)
def test_make_calls_name_a_call_they_cannot_place(command: str, tmp_path: Path) -> None:
    assert mod.make_calls(_step(command), tmp_path) == ([], [1])


@pytest.mark.parametrize(
    ("command", "dirs"),
    [
        ("(cd docs && make html)\nmake check", ["docs", "."]),
        ("cd docs && make html\nmake check", ["docs", "docs"]),
        ("pushd sub\nmake check\npopd\nmake lint", ["sub", "."]),
        ("cd sub | cat\nmake check", ["."]),
        ("make -C sub check\nmake --directory=sub/x lint", ["sub", "sub/x"]),
        ("if true; then cd sub; make check; fi", ["sub"]),
        # A background command runs in a subshell, so its `cd` moves nothing.
        ("cd sub &\nmake check", ["."]),
        # A `cd` in one branch does not run on the path of another branch.
        (
            'if [ -n "$FULL" ]; then cd sub; make check; else make quick; fi',
            ["sub", "."],
        ),
        ("case $X in a) cd sub; make a;; b) make b;; esac", ["sub", "."]),
    ],
)
def test_make_calls_keep_a_directory_change_in_its_own_shell(
    command: str, dirs: list[str], tmp_path: Path
) -> None:
    calls, untraced = mod.make_calls(_step(command), tmp_path)
    assert [c.cwd for c in calls] == [(tmp_path / d).resolve() for d in dirs]
    assert untraced == []


@pytest.mark.parametrize(
    ("command", "args", "env"),
    [
        ("timeout 600 make check", ("check",), {}),
        ("env CI=1 make check", ("check",), {"CI": "1"}),
        ("MODE=ci make check", ("check",), {"MODE": "ci"}),
        ("uv run make -k check", ("-k", "check"), {}),
    ],
)
def test_make_calls_read_a_wrapper_and_the_variables_before_make(
    command: str, args: tuple, env: dict, tmp_path: Path
) -> None:
    calls, untraced = mod.make_calls(_step(command), tmp_path)
    assert [(c.args, dict(c.env)) for c in calls] == [(args, env)]
    assert untraced == []


def test_a_make_package_name_is_not_a_call(tmp_path: Path) -> None:
    command = "sudo apt-get install -y make"
    assert mod.make_calls(_step(command), tmp_path) == ([], [])


def test_a_heredoc_that_appends_to_a_file_is_no_ci_expression() -> None:
    assert mod.ci_shell_text("cat <<EOF >> out\nx\nEOF") == "cat <<EOF >> out\nx\nEOF"


def test_a_make_word_inside_a_printed_message_is_not_a_call(tmp_path: Path) -> None:
    calls, untraced = mod.make_calls(_step('echo "run make check"'), tmp_path)
    assert (calls, untraced) == ([], [])


# ── make trace ──────────────────────────────────────────────────────────

TRACED_MAKEFILE = f"""\
.PHONY: check
check:
#% a help comment between the header and the recipe
{TAB}@echo one
{TAB}if true; then \\
{TAB}  echo two; \\
{TAB}fi
{TAB}-echo three
"""


def test_trace_names_the_first_recipe_line_and_maps_each_command(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(TRACED_MAKEFILE, encoding="utf-8")
    result = mod.make_trace(mod.gnu_make(), tmp_path, ["check"])
    assert result.returncode == 0
    assert [(c.first_line, c.index, c.text) for c in result.commands] == [
        (4, 0, "echo one"),
        (4, 1, "if true; then \\\n  echo two; \\\nfi"),
        (4, 2, "echo three"),
    ]
    recipe = mod.recipe_lines(TRACED_MAKEFILE, 4)
    assert recipe == [
        mod.RecipeLine(4, 1, "@"),
        mod.RecipeLine(5, 3, ""),
        mod.RecipeLine(8, 1, "-"),
    ]


def test_recipe_lines_name_the_lines_a_dry_run_runs() -> None:
    text = (
        f"check:\n{TAB}+touch x\n{TAB}$(MAKE) -C sub\n{TAB}echo ${{MAKE}}\n{TAB}echo\n"
    )
    assert [r.runs for r in mod.recipe_lines(text, 2)] == [True, True, True, False]


def test_recipe_lines_read_a_custom_recipe_prefix() -> None:
    text = "check:\n>@one\n>-two\n"
    assert mod.recipe_lines(text, 2, ">") == [
        mod.RecipeLine(2, 1, "@"),
        mod.RecipeLine(3, 1, "-"),
    ]


def test_parse_trace_skips_the_commands_of_a_builtin_rule(tmp_path: Path) -> None:
    output = (
        "Makefile:3: target 'all' does not exist\n"
        "echo one\n"
        "<builtin>: update target 'prog' due to: prog.c\n"
        "cc prog.c -o prog\n"
    )
    commands, hidden = mod.parse_trace(output, tmp_path)
    assert [(c.first_line, c.text) for c in commands] == [(3, "echo one")]
    assert not hidden


def test_recipe_lines_step_over_a_conditional() -> None:
    text = f"check:\n{TAB}one\nifdef CI\n{TAB}two\nelse\n{TAB}-three\nendif\nother:\n{TAB}x\n"
    assert mod.recipe_lines(text, 2) == [
        mod.RecipeLine(2, 1, ""),
        mod.RecipeLine(4, 1, ""),
        mod.RecipeLine(6, 1, "-"),
    ]


def test_make_shell_reads_the_values_the_makefile_sets(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "SHELL := /bin/bash\n.SHELLFLAGS := -eo pipefail -c\nall:\n\t@true\n",
        encoding="utf-8",
    )
    shell = mod.make_shell(mod.gnu_make(), tmp_path)
    assert shell == mod.MakeShell("/bin/bash", "-eo pipefail -c", False)
    assert mod.shell_options(shell.shell, shell.flags) == (
        "/bin/bash",
        ["-eo", "pipefail"],
        True,
        True,
    )


def test_make_shell_expands_a_deferred_value_and_reads_the_named_makefile(
    tmp_path: Path,
) -> None:
    (tmp_path / "Makefile").write_text(
        "BASH_PATH := /bin/bash\nSHELL = $(BASH_PATH)\nall:\n\t@true\n",
        encoding="utf-8",
    )
    (tmp_path / "ci.mk").write_text(
        "SHELL := /bin/bash\n.SHELLFLAGS := -eo pipefail -c\n.ONESHELL:\ncheck:\n\t@true\n",
        encoding="utf-8",
    )
    make = mod.gnu_make()
    assert mod.make_shell(make, tmp_path) == mod.MakeShell("/bin/bash", "-c", False)
    assert mod.make_shell(make, tmp_path, ["-f", "ci.mk"]) == mod.MakeShell(
        "/bin/bash", "-eo pipefail -c", True
    )


def test_make_shell_sets_the_goals_a_makefile_tests(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        "ifneq ($(MAKECMDGOALS),check)\n$(error run make check)\nendif\n"
        "SHELL := /bin/bash\ncheck:\n\t@true\n",
        encoding="utf-8",
    )
    make = mod.gnu_make()
    assert mod.make_shell(make, tmp_path, (), ("check",)).shell == "/bin/bash"
    with pytest.raises(mod.TraceError, match="run make check"):
        mod.make_shell(make, tmp_path)


@pytest.mark.parametrize(
    ("args", "split"),
    [
        (("check", "lint"), ((), ("check", "lint"))),
        (("-f", "ci.mk", "check", "V=1"), (("-f", "ci.mk", "V=1"), ("check",))),
        (("--file", "x.mk", "-k", "all"), (("--file", "x.mk", "-k"), ("all",))),
    ],
)
def test_split_make_args(args: tuple, split: tuple) -> None:
    assert mod.split_make_args(args) == split


@pytest.mark.parametrize(
    ("shell", "flags", "expected"),
    [
        ("/bin/sh", "-c", ("/bin/sh", [], False, False)),
        ("/bin/bash -eo pipefail", "", ("/bin/bash", ["-eo", "pipefail"], True, True)),
        ("/bin/sh", "-ec", ("/bin/sh", ["-e"], True, False)),
        # dash has no pipefail, so the option is not believed there.
        ("/bin/sh", "-o pipefail -c", ("/bin/sh", ["-o", "pipefail"], False, False)),
        ("bash -e {0}", "", ("bash", ["-e"], True, False)),
        (
            "/usr/bin/env bash",
            "-eu -o pipefail -c",
            ("bash", ["-eu", "-o", "pipefail"], True, True),
        ),
    ],
)
def test_shell_options(shell: str, flags: str, expected: tuple) -> None:
    assert mod.shell_options(shell, flags) == expected


@pytest.mark.parametrize(
    ("command", "errexit", "pipefail", "kinds"),
    [
        ("a; b", False, False, ["sequence"]),
        ("a; b", True, False, []),
        ("a | b", True, False, ["pipe"]),
        ("a | b", True, True, []),
        ("a; test $? -eq 2", False, False, []),
        ("echo x | b", False, True, []),
        ("if a | grep -q x; then echo y; fi", True, False, ["pipe"]),
        # The two probes: the banned idiom in a printed message and in a heredoc.
        ('echo "a; b | c"', False, False, []),
        ("cat <<EOF\na | b\nEOF", True, False, []),
    ],
)
def test_dropped_statuses(
    command: str, errexit: bool, pipefail: bool, kinds: list[str]
) -> None:
    assert [k for _, k, _ in mod.dropped_statuses(command, errexit, pipefail)] == kinds


@pytest.mark.parametrize(
    ("command", "interpreter", "kinds"),
    [
        ("set -e; a; b", "bash", []),
        ("a; set -e; b; c", "bash", ["sequence"]),
        ("set -o pipefail; a | b", "bash", []),
        # dash has no pipefail, so `set -o pipefail` does not help there.
        ("set -o pipefail; a | b", "/bin/sh", ["pipe"]),
        ("export X=1; cmd", "bash", []),
        ("X=1; cmd", "bash", []),
        ("a; rc=$?", "bash", []),
        ("a; [ $? -eq 0 ] || exit 1", "bash", []),
        ("a; (( $? == 0 )) || exit 1", "bash", []),
        ('a; echo "done $?"', "bash", ["sequence"]),
        # The assignment takes the status of the substitution, so it can fail.
        ("X=$(git ls-files); pre-commit run", "bash", ["sequence"]),
        ("set -e; set +e; a; b", "bash", ["sequence"]),
        # Each body is a sequence too, and a loop reports only its last pass.
        ("for f in a b; do lint $f; report; done", "bash", ["sequence", "loop"]),
        ("if x; then a; b; fi", "bash", ["sequence"]),
        ("if x; then a; fi; b", "bash", ["sequence"]),
        ("case z in a) p; q;; esac", "bash", ["sequence"]),
        ("{ a; b; } > f", "bash", ["sequence"]),
        # A `||` handles the failure on its left.
        ("rc=0; a || rc=1; b || rc=1; exit $rc", "bash", []),
        ("for d in a b; do make -C $d || rc=1; done; exit $rc", "bash", []),
        ("cd x || exit 1; make", "bash", []),
        ("if ! a; then exit 1; fi; b", "bash", []),
    ],
)
def test_dropped_statuses_read_set_and_assignments(
    command: str, interpreter: str, kinds: list[str]
) -> None:
    found = mod.dropped_statuses(command, False, False, interpreter)
    assert [k for _, k, _ in found] == kinds


# ── $(shell …) ──────────────────────────────────────────────────────────


def test_shell_calls_find_each_call_and_its_line() -> None:
    text = (
        "A := $(shell git ls-files '*.txt')\n"
        "# B := $(shell in a comment)\n"
        "C := ${shell echo $(X) }\n"
        "D := $(patsubst %,x/%,$(shell find . -name '*.sh'))\n"
        "E := $(shell never closes\n"
    )
    assert mod.shell_calls(text) == [
        mod.ShellCall(1, "git ls-files '*.txt'"),
        mod.ShellCall(3, "echo $(X) "),
        mod.ShellCall(4, "find . -name '*.sh'"),
    ]


@pytest.mark.parametrize(
    ("body", "target"),
    [
        (
            "git -C $(REPO_ROOT) ls-files -z",
            ("git", ("-C", "ls-files", "-z")),
        ),
        (
            "find $(DIR) -name '*.json' 2>/dev/null | grep -v x",
            ("find", ("-name", "*.json")),
        ),
        ("grep '^v' f | awk '{print $$2}'", ("grep", ("^v", "f"))),
        ("$(TOOL) --list", None),
        ("cd $(DIR) && git ls-files", ("git", ("ls-files",))),
    ],
)
def test_fault_target(body: str, target: tuple | None) -> None:
    assert mod.fault_target(body) == target


@given(st.text())
def test_shell_calls_never_crash_and_return_substrings(text: str) -> None:
    for call in mod.shell_calls(text):
        assert call.body in text
        assert 1 <= call.line <= text.count("\n") + 1


@given(st.text(max_size=200))
def test_fault_target_never_crashes(body: str) -> None:
    target = mod.fault_target(body)
    assert target is None or isinstance(target[0], str)


# ── end to end ──────────────────────────────────────────────────────────


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    init_test_repo(tmp_path)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


CONFIG = """\
version: 2.1
jobs:
  test:
    docker:
    - image: cimg/base@sha256:0000000000000000000000000000000000000000000000000000000000000000
    steps:
    - checkout
    - run: |
        pip install uv
        make check
"""

VACUOUS_MAKEFILE = f"""\
FILES := $(shell git ls-files '*.txt')
check:
{TAB}@echo linting $(FILES)
{TAB}-false
{TAB}producer | consumer
"""


def test_end_to_end_reports_each_defect_at_its_source_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path,
        {
            ".circleci/config.yml": CONFIG,
            "Makefile": VACUOUS_MAKEFILE,
            "a.txt": "x\n",
            "other/Makefile": f"check:\n{TAB}@true\n",
            "accepted/Makefile": f"# trace-ci-ok: run by hand before a release\ncheck:\n{TAB}@true\n",
            "nogoal/Makefile": f"build:\n{TAB}@true\n",
        },
    )
    rc = mod.main(["--repo", str(repo), "--format", "json"])
    out = capsys.readouterr()
    findings = [(f["path"], f["line"], f["kind"]) for f in json.loads(out.out)]
    assert rc == 1
    assert findings == [
        (".circleci/config.yml", 9, "lint"),
        ("Makefile", 1, "vacuous-on-failure"),
        ("Makefile", 4, "ignored-error"),
        ("Makefile", 5, "lost-status-pipe"),
        ("other/Makefile", 1, "uncovered"),
    ]
    message = {f["kind"]: f["message"] for f in json.loads(out.out)}
    assert "install command names no version" in message["lint"]
    assert "1 fewer arguments" in message["vacuous-on-failure"]


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {
                "Makefile": f"check:\n{TAB}$(MAKE) -C sub lint\n{TAB}-echo after\n",
                "sub/Makefile": f"lint:\n{TAB}echo one\n{TAB}echo two\n",
            },
            [("Makefile", 3, "ignored-error")],
            id="a sub-make does not take the parent's later lines",
        ),
        pytest.param(
            {
                "Makefile": f"EMPTY :=\ncheck:\n{TAB}$(EMPTY)\n{TAB}producer | consumer\n{TAB}-false\n"
            },
            [("Makefile", 3, "lost-status-pipe"), ("Makefile", 5, "ignored-error")],
            id="an empty line moves the findings to the first recipe line",
        ),
        pytest.param(
            {
                "Makefile": f"check:\n{TAB}$(MAKE) -C sub -f ci.mk V=1 lint\n",
                "sub/ci.mk": "ifneq ($(MAKECMDGOALS),lint)\n$(error run lint)\nendif\n"
                f"SHELL := /bin/bash\n.SHELLFLAGS := -eo pipefail -c\nlint:\n{TAB}a | b\n",
            },
            [],
            id="a sub-make's shell is read with its own options and goals",
        ),
        pytest.param(
            {
                "Makefile": f"check:\n{TAB}$(MAKE) --no-print-directory -C sub lint\n",
                "sub/Makefile": f"lint:\n{TAB}-a | b\n",
            },
            [(".circleci/config.yml", 10, "untraced")],
            id="a sub-make that hides its directory is not traced",
        ),
    ],
)
def test_end_to_end_maps_recipe_lines_that_make_does_not_print_one_to_one(
    files: dict[str, str],
    expected: list[tuple],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = CONFIG.replace("pip install uv", "pip install uv==0.9.0")
    repo = _repo(tmp_path, {".circleci/config.yml": config, **files})
    mod.main(["--repo", str(repo), "--format", "json", "--goal", ""])
    found = [
        (f["path"], f["line"], f["kind"]) for f in json.loads(capsys.readouterr().out)
    ]
    assert found == expected


def _config(command: str, job: str = "") -> str:
    """A CircleCI config whose one job runs COMMAND on line 8, plus the lines
    of JOB, which go before `docker:`."""
    image = "cimg/base@sha256:" + "0" * 64
    return (
        f"version: 2.1\njobs:\n  test:\n{job}    docker:\n    - image: {image}\n"
        f"    steps:\n    - checkout\n    - run: {command}\n"
    )


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"Makefile": f".ONESHELL:\ncheck:\n{TAB}false\n{TAB}echo ok\n"},
            [("Makefile", 3, "lost-status-sequence")],
            id="one shell runs a .ONESHELL recipe",
        ),
        pytest.param(
            {"Makefile": ".RECIPEPREFIX := >\ncheck:\n>-false\n"},
            [("Makefile", 3, "ignored-error")],
            id="a custom recipe prefix",
        ),
        pytest.param(
            {
                "Makefile": f"check:\n{TAB}@echo 'x; y | z'; $(MAKE) -C sub lint\n{TAB}a | b\n",
                "sub/Makefile": f"lint:\n{TAB}@true\n",
            },
            [("Makefile", 2, "untraced")],
            id="the output of a $(MAKE) line is not read as commands",
        ),
        pytest.param(
            {"Makefile": f"check:\n{TAB}@echo '# trace-ci-ok: x'\n{TAB}-false\n"},
            [("Makefile", 3, "ignored-error")],
            id="a marker in a string accepts nothing",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config(
                    'make "$T"\n    - run:\n        name: "# trace-ci-ok: no"\n        command: make $T'
                ),
                "Makefile": f"check:\n{TAB}@true\n",
            },
            [
                (".circleci/config.yml", 8, "untraced"),
                (".circleci/config.yml", 11, "untraced"),
                # No call that trace-ci can trace reaches the root makefile.
                ("Makefile", 1, "uncovered"),
            ],
            id="a marker in a YAML value accepts nothing",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config(
                    "make $T  # trace-ci-ok: CI sets the target"
                ),
                "Makefile": f"check:\n{TAB}@true\n",
            },
            [("Makefile", 1, "uncovered")],
            id="a marker in a YAML comment accepts the finding",
        ),
        pytest.param(
            {
                "Makefile": f"check:\n{TAB}$(MAKE) -C sub check\n",
                "sub/Makefile": f"check:\n{TAB}-false\n",
                "other/Makefile": f"build:\n{TAB}@true\n",
                "other/check/x": "",
            },
            [("sub/Makefile", 2, "ignored-error")],
            id="a sub-make reaches its directory, and a directory is no goal",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config("make -f sub/Makefile check"),
                "Makefile": f"check:\n{TAB}@true\n",
                "sub/Makefile": f"check:\n{TAB}@true\n",
            },
            [("Makefile", 1, "uncovered")],
            id="make -f reaches the makefile's directory and not its own",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config(
                    "make check", "    environment:\n      MODE: ci\n"
                ),
                "Makefile": f"ifeq ($(MODE),ci)\ncheck:\n{TAB}a | b\nelse\ncheck:\n{TAB}@true\nendif\n",
            },
            [("Makefile", 3, "lost-status-pipe")],
            id="the job environment reaches make",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config("MODE=ci make check"),
                "Makefile": f"ifeq ($(MODE),ci)\ncheck:\n{TAB}a | b\nendif\n",
            },
            [("Makefile", 3, "lost-status-pipe")],
            id="a variable before make reaches make",
        ),
        pytest.param(
            {
                ".circleci/config.yml": _config("make -s check"),
                "Makefile": f"check:\n{TAB}cd sub && $(MAKE) lint\n",
                "sub/Makefile": f"lint:\n{TAB}-false\n",
            },
            [("sub/Makefile", 2, "ignored-error")],
            id="make -s still shows the sub-make directories",
        ),
    ],
)
def test_end_to_end_cases(
    files: dict[str, str],
    expected: list[tuple],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path, {".circleci/config.yml": _config("make check"), **files})
    mod.main(["--repo", str(repo), "--format", "json", "--no-fault-injection"])
    found = [
        (f["path"], f["line"], f["kind"]) for f in json.loads(capsys.readouterr().out)
    ]
    assert found == expected


def test_two_calls_of_one_command_fail_together_and_are_reported_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    makefile = f"""\
UNUSED = $(shell git ls-files '*.txt')
FILES := $(shell git ls-files '*.txt')
check:
{TAB}@echo linting $(FILES)
"""
    repo = _repo(
        tmp_path,
        {
            ".circleci/config.yml": _config("make check"),
            "Makefile": makefile,
            "a.txt": "x\n",
        },
    )
    mod.main(["--repo", str(repo), "--format", "json"])
    found = json.loads(capsys.readouterr().out)
    assert [(f["path"], f["line"], f["kind"]) for f in found] == [
        ("Makefile", 1, "vacuous-on-failure")
    ]
    assert "fail at Makefile:2 too" in found[0]["message"]


def test_a_shell_call_whose_failure_stops_make_is_not_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    makefile = f"""\
FILES := $(shell git ls-files '*.txt')
$(if $(FILES),,$(error no files to lint))
check:
{TAB}@echo linting $(FILES)
"""
    repo = _repo(
        tmp_path,
        {
            ".circleci/config.yml": CONFIG.replace(
                "pip install uv", "pip install uv==0.9.0"
            ),
            "Makefile": makefile,
            "a.txt": "x\n",
        },
    )
    rc = mod.main(["--repo", str(repo)])
    out = capsys.readouterr()
    assert (rc, out.out) == (0, "")
    assert "0 finding(s)" in out.err


def test_the_annotation_accepts_a_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    makefile = VACUOUS_MAKEFILE.replace(
        "FILES := $(shell git ls-files '*.txt')",
        "# trace-ci-ok: an empty list is a real empty list here\n"
        "FILES := $(shell git ls-files '*.txt')",
    )
    repo = _repo(
        tmp_path,
        {".circleci/config.yml": CONFIG, "Makefile": makefile, "a.txt": "x\n"},
    )
    mod.main(["--repo", str(repo), "--format", "json", "--no-fault-injection"])
    without_injection = {f["kind"] for f in json.loads(capsys.readouterr().out)}
    mod.main(["--repo", str(repo), "--format", "json"])
    with_injection = {f["kind"] for f in json.loads(capsys.readouterr().out)}
    assert "vacuous-on-failure" not in with_injection
    assert with_injection == without_injection


def test_a_repo_with_no_followed_ci_file_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(
        tmp_path,
        {".gitlab-ci.yml": "test:\n  script: make check\n", "Makefile": "check:\n"},
    )
    assert mod.main(["--repo", str(repo)]) == 2
    err = capsys.readouterr().err
    assert "gitlab-ci (.gitlab-ci.yml)" in err
    assert "nothing to trace" in err


def test_a_selection_with_no_content_lint_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, {".circleci/config.yml": CONFIG, "Makefile": "check:\n"})
    assert mod.main(["--repo", str(repo), "--select", "check:check-job-timeout"]) == 2
    assert "matches no lint" in capsys.readouterr().err

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


def test_a_circleci_run_step_without_a_command_is_refused() -> None:
    text = "jobs:\n  j:\n    steps:\n    - run:\n        name: x\n"
    with pytest.raises(mod.TraceError, match="c.yml:4"):
        mod.circleci_steps(text, "c.yml")


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
"""
    steps = mod.workflow_steps(text, "w.yml")
    assert [(s.working_directory, s.lint) for s in steps] == [
        ("top", False),
        ("step", False),
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


@pytest.mark.parametrize(
    ("shell", "flags", "expected"),
    [
        ("/bin/sh", "-c", ("/bin/sh", [], False, False)),
        ("/bin/bash -eo pipefail", "", ("/bin/bash", ["-eo", "pipefail"], True, True)),
        ("/bin/sh", "-ec", ("/bin/sh", ["-e"], True, False)),
        # dash has no pipefail, so the option is not believed there.
        ("/bin/sh", "-o pipefail -c", ("/bin/sh", ["-o", "pipefail"], False, False)),
        ("bash -e {0}", "", ("bash", ["-e"], True, False)),
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

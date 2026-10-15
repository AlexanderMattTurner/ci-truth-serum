#!/usr/bin/env python3
"""Trace the commands that CI really runs, and find where CI can pass over a failure.

The lints in this pack read files by kind: a GitHub workflow, a shell script, a
`Dockerfile`. Much CI is in other files. A CircleCI job can run `make check`,
and the real work is in the `Makefile` recipes that `make check` reaches. No
lint reads those recipes as CI, so a defect in them is not seen. This tool
starts from the CI files and follows what they run::

    trace-ci                     # the repository in the current directory
    trace-ci --repo path/to/repo --select tier:1 --select tier:2
    trace-ci --format json

It does four things:

1. It finds the CI files by their paths. The CircleCI and GitHub Actions files
   are followed. Other CI systems are named on stderr, because this tool does
   not read them yet.
2. It runs `make --dry-run --trace` for each `make` that a CI step calls. GNU
   make prints each command that it would run, and the `Makefile` line that the
   command came from. Each command goes into a temporary file, with the shell
   options that it really runs under. The content lints of this pack then read
   those files, and each finding points back to the source line.
3. It makes each `$(shell …)` call in the makefiles fail, one at a time, and
   traces CI again. GNU make does not stop when a `$(shell …)` command fails. It
   uses the empty output. If `make` still exits 0 but runs fewer commands or
   fewer arguments, the CI check can pass when it checked nothing. A worked
   case: `FILES := $(shell git ls-files)` feeds `pre-commit run --files
   $(FILES)`. If `git` fails, pre-commit gets no files, skips every hook, and
   exits 0.
4. It finds a directory whose `Makefile` has a `check` goal that no CI step
   reaches (`--goal` changes the name).

It also reports two make-only defects in the commands that CI reaches. GNU make
runs each recipe line with `/bin/sh -c`, so `a; b` reports only the status of
`b`, and `a | b` reports only the status of `b`. A recipe line that starts with
`-` has its failure ignored.

Run this tool only on a tree that you trust as much as you trust `make`. A dry
run still runs each `$(shell …)` call, and each recipe line that holds `$(MAKE)`
or starts with `+`.

To accept a finding, put `# trace-ci-ok: <reason>` on its line or on the line
above it. For an uncovered directory, put the comment on line 1 of its
`Makefile`. A finding from a content lint takes that lint's own annotation, in
the CI step or the recipe line.

Exit status: 0 when there is no finding, 1 when there are findings, and 2 when
the tool cannot do the trace (no GNU make, no CI file that it follows, or a
selection that matches no content lint).
"""

import argparse
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import NamedTuple

import yaml
from identify import identify

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    iter_nodes,
    node_text,
    parse,
    program_name,
    unquote,
)
from _cts_registry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    DOCKERFILE,
    SHELL,
    SHELL_OR_DOCKERFILE,
    SHELL_OR_WORKFLOW_YAML,
    SHELL_PYTHON_OR_WORKFLOW_YAML,
    Check,
)
from run_selection import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    SelectorError,
    resolve_all,
)

ANNOTATION = re.compile(r"#\s*trace-ci-ok:\s*\S")

# CI configuration files, by path. The globs are the `fileMatch` values of the
# SchemaStore catalog (https://www.schemastore.org/api/json/catalog.json), which
# the YAML language servers use to find these files. `identify` names makefiles,
# Dockerfiles and Jenkinsfiles, but it tags every CI YAML file as plain `yaml`.
CI_GLOBS: dict[str, tuple[str, ...]] = {
    "github-actions": (
        "**/.github/workflows/*.yml",
        "**/.github/workflows/*.yaml",
        "**/.gitea/workflows/*.yml",
        "**/.gitea/workflows/*.yaml",
        "**/.forgejo/workflows/*.yml",
        "**/.forgejo/workflows/*.yaml",
    ),
    "circleci": ("**/.circleci/config.yml",),
    "gitlab-ci": (
        "**/.gitlab-ci.yml",
        "**/.gitlab-ci.yaml",
        "**/*.gitlab-ci.yml",
        "**/*.gitlab-ci.yaml",
    ),
    "azure-pipelines": ("azure-pipelines.yml", "azure-pipelines.yaml"),
    "bitbucket-pipelines": ("bitbucket-pipelines.yml",),
    "buildkite": (
        "buildkite.yml",
        "buildkite.yaml",
        "**/.buildkite/pipeline.yml",
        "**/.buildkite/pipeline.yaml",
        "**/.buildkite/pipeline.*.yml",
        "**/.buildkite/pipeline.*.yaml",
    ),
    "drone": (".drone.yml",),
    "travis": (".travis.yml",),
    "appveyor": ("appveyor.yml",),
    "cirrus": (".cirrus.yml",),
    "woodpecker": ("**/.woodpecker.yml", "**/.woodpecker.yaml"),
}
# The CI systems whose steps this tool reads.
FOLLOWED = frozenset({"github-actions", "circleci"})

# CircleCI runs a step with this shell when the step and its job name none.
CIRCLECI_DEFAULT_SHELL = "/bin/bash -eo pipefail"
# The file classes of the content lints that read a synthetic file.
CONTENT_KINDS = frozenset(
    {
        SHELL,
        DOCKERFILE,
        SHELL_OR_DOCKERFILE,
        SHELL_OR_WORKFLOW_YAML,
        SHELL_PYTHON_OR_WORKFLOW_YAML,
    }
)
# A pipeline stage or a statement that runs one of these commands has no failure
# worth a finding.
NO_FAILURE = frozenset({"echo", "printf", "true", ":", "yes"})
# Commands that `/bin/sh` runs itself. A stub earlier on PATH cannot replace them.
SHELL_BUILTINS = frozenset(
    {
        "echo",
        "printf",
        "test",
        "[",
        "cd",
        "pwd",
        "read",
        "true",
        "false",
        ":",
        "eval",
        "exec",
        "export",
        "set",
        "type",
        "command",
    }
)

TRACE_RULE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+): (?:target '.*' does not exist|update target '.*' due to: .*)$"
)
MAKE_MESSAGE = re.compile(r"^\S*make(?:\[\d+\])?: ")
ENTER = re.compile(r"^\S*make(?:\[\d+\])?: Entering directory '(?P<dir>.*)'$")
LEAVE = re.compile(r"^\S*make(?:\[\d+\])?: Leaving directory '(?P<dir>.*)'$")
MAKE_REF_PLACEHOLDER = "__cts_make_ref__"
# Nodes whose value only the shell can compute. A `make` call that holds one
# cannot be traced, because trace-ci does not know the goal or the argument.
EXPANSIONS = (
    "command_substitution",
    "process_substitution",
    "arithmetic_expansion",
    "expansion",
    "simple_expansion",
)


class TraceError(RuntimeError):
    """The trace cannot continue. The message names what is missing."""


@dataclass(frozen=True)
class CiStep:
    """One `run:` step of a CI file."""

    source: str
    job: str
    shell: str
    command: str
    lines: tuple[int, ...]
    working_directory: str
    lint: bool


@dataclass(frozen=True)
class Image:
    """One container image that a CI job runs in."""

    source: str
    job: str
    image: str
    line: int


@dataclass(frozen=True)
class MakeCall:
    """One `make` that a CI step runs."""

    step: CiStep
    cwd: Path
    args: tuple[str, ...]
    line: int


@dataclass(frozen=True)
class Traced:
    """One command that a dry run of make prints."""

    makefile: Path
    first_line: int
    index: int
    text: str
    cwd: Path


@dataclass(frozen=True)
class Trace:
    returncode: int
    commands: tuple[Traced, ...]
    stderr: str


@dataclass(frozen=True)
class RecipeLine:
    start: int
    count: int
    prefix: str


@dataclass(frozen=True)
class ShellCall:
    """One `$(shell …)` call in a makefile: its line and the shell text in it."""

    line: int
    body: str


@dataclass(frozen=True)
class TraceFinding:
    path: str
    line: int
    kind: str
    message: str


# --------------------------------------------------------------------------- #
# Find the CI files
# --------------------------------------------------------------------------- #
def _glob_match(path: str, pattern: str) -> bool:
    """Whether PATH matches PATTERN, where a leading `**/` matches zero or more
    directories. `fnmatch` alone lets `*` cross a `/`, so each side is split."""
    if pattern.startswith("**/"):
        tail = pattern[3:].split("/")
        parts = path.split("/")
        return len(parts) >= len(tail) and all(
            fnmatch.fnmatchcase(p, t) for p, t in zip(parts[-len(tail) :], tail)
        )
    parts, pat = path.split("/"), pattern.split("/")
    return len(parts) == len(pat) and all(
        fnmatch.fnmatchcase(p, t) for p, t in zip(parts, pat)
    )


def discover(files: Sequence[str]) -> dict[str, list[str]]:
    """The CI files in FILES, keyed by CI system, plus `make` and `dockerfile`."""
    found: dict[str, list[str]] = {}
    for path in files:
        for kind, globs in CI_GLOBS.items():
            if any(_glob_match(path, g) for g in globs):
                found.setdefault(kind, []).append(path)
        tags = identify.tags_from_filename(path)
        if "makefile" in tags:
            found.setdefault("make", []).append(path)
        if "dockerfile" in tags:
            found.setdefault("dockerfile", []).append(path)
        if "jenkins" in tags:
            found.setdefault("jenkins", []).append(path)
    return found


# --------------------------------------------------------------------------- #
# Read the CI steps
# --------------------------------------------------------------------------- #
def _child(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v
    return None


def _items(node: yaml.Node | None) -> list[tuple[str, yaml.Node]]:
    if not isinstance(node, yaml.MappingNode):
        return []
    return [(k.value, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)]


def _seq(node: yaml.Node | None) -> list[yaml.Node]:
    return list(node.value) if isinstance(node, yaml.SequenceNode) else []


def _scalar(node: yaml.Node | None) -> str | None:
    return node.value if isinstance(node, yaml.ScalarNode) else None


def scalar_lines(node: yaml.ScalarNode) -> tuple[int, ...]:
    """The source line of each line of NODE's value.

    Only a literal block (`|`) keeps its lines as written. Any other style can
    fold lines together, so each line of it maps to the line where it starts."""
    count = max(len(node.value.splitlines()), 1)
    if node.style == "|":
        first = node.start_mark.line + 2
        return tuple(range(first, first + count))
    return (node.start_mark.line + 1,) * count


def circleci_steps(text: str, source: str) -> tuple[list[CiStep], list[Image]]:
    """The `run:` steps and the job images of a CircleCI configuration."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    steps: list[CiStep] = []
    images: list[Image] = []
    for section in ("jobs", "commands"):
        for job, body in _items(_child(root, section)):
            job_shell = _scalar(_child(body, "shell"))
            job_wd = _scalar(_child(body, "working_directory"))
            for entry in _seq(_child(body, "docker")):
                image = _child(entry, "image")
                if isinstance(image, yaml.ScalarNode):
                    images.append(
                        Image(source, job, image.value, image.start_mark.line + 1)
                    )
            for step in _seq(_child(body, "steps")):
                run = _child(step, "run")
                if run is None:
                    continue
                command, shell, wd = run, job_shell, job_wd
                if isinstance(run, yaml.MappingNode):
                    command = _child(run, "command")
                    shell = _scalar(_child(run, "shell")) or shell
                    wd = _scalar(_child(run, "working_directory")) or wd
                if not isinstance(command, yaml.ScalarNode):
                    raise TraceError(
                        f"{source}:{step.start_mark.line + 1}: this `run:` step has no "
                        "command string, so trace-ci cannot read what it runs"
                    )
                steps.append(
                    CiStep(
                        source,
                        job,
                        shell or CIRCLECI_DEFAULT_SHELL,
                        command.value,
                        scalar_lines(command),
                        _circleci_dir(wd, source, run.start_mark.line + 1),
                        lint=True,
                    )
                )
    return steps, images


def _circleci_dir(wd: str | None, source: str, line: int) -> str:
    """WD relative to the checkout. CircleCI checks the code out to `~/project`."""
    if wd is None or wd in ("~/project", "."):
        return ""
    if wd.startswith("~/project/"):
        return wd[len("~/project/") :]
    if not wd.startswith(("/", "~", "$")):
        return wd
    raise TraceError(
        f"{source}:{line}: working_directory {wd!r} is outside ~/project, so "
        "trace-ci cannot find it in the checkout"
    )


def workflow_steps(text: str, source: str) -> list[CiStep]:
    """The `run:` steps of a GitHub Actions workflow.

    The lints of this pack read these steps already, so the steps are traced for
    the `make` calls in them and are not written out for the lints again."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    top_run = _child(_child(root, "defaults"), "run")
    steps: list[CiStep] = []
    for job, body in _items(_child(root, "jobs")):
        job_run = _child(_child(body, "defaults"), "run")
        for step in _seq(_child(body, "steps")):
            run = _child(step, "run")
            if not isinstance(run, yaml.ScalarNode):
                continue
            wd = next(
                (
                    v
                    for v in (
                        _scalar(_child(step, "working-directory")),
                        _scalar(_child(job_run, "working-directory")),
                        _scalar(_child(top_run, "working-directory")),
                    )
                    if v is not None
                ),
                "",
            )
            shell = next(
                (
                    v
                    for v in (
                        _scalar(_child(step, "shell")),
                        _scalar(_child(job_run, "shell")),
                        _scalar(_child(top_run, "shell")),
                    )
                    if v is not None
                ),
                "bash -e {0}",
            )
            steps.append(
                CiStep(source, job, shell, run.value, scalar_lines(run), wd, lint=False)
            )
    return steps


def make_calls(step: CiStep, repo: Path) -> tuple[list[MakeCall], list[int]]:
    """The `make` calls in STEP, and the lines of the calls that cannot be traced.

    A `cd` with one plain argument moves the directory for the commands after it.
    A `make` whose arguments hold an expansion, or that comes after a `cd` that
    this tool cannot follow, is returned as a line that cannot be traced."""
    cwd: Path | None = (repo / step.working_directory).resolve()
    if "$" in step.working_directory:
        cwd = None
    calls: list[MakeCall] = []
    untraced: list[int] = []
    for node in iter_nodes(parse(step.command), "command"):
        words = command_words(node)
        if not words:
            continue
        line = step.lines[min(node.start_point[0], len(step.lines) - 1)]
        name = program_name(words[0])
        args = [unquote(w) for w in words[1:]]
        if name == "cd":
            plain = (
                len(args) == 1
                and not args[0].startswith(("~", "-"))
                and "$" not in words[1]
            )
            cwd = (cwd / args[0]).resolve() if plain and cwd is not None else None
        elif name in ("make", "gmake"):
            if cwd is None or any(True for _ in iter_nodes(node, *EXPANSIONS)):
                untraced.append(line)
            else:
                calls.append(MakeCall(step, cwd, tuple(args), line))
    return calls, untraced


# --------------------------------------------------------------------------- #
# Trace make
# --------------------------------------------------------------------------- #
def gnu_make() -> str:
    """The path of a GNU make that has `--trace` (GNU make 4.0 or later)."""
    for name in ("gmake", "make"):
        path = shutil.which(name)
        if path is None:
            continue
        version = subprocess.run(
            [path, "--version"], capture_output=True, text=True, check=False
        ).stdout
        match = re.match(r"GNU Make (?P<major>\d+)\.", version)
        if match and int(match.group("major")) >= 4:
            return path
    raise TraceError("trace-ci needs GNU make 4.0 or later on PATH (for --trace)")


def _make_env(path_prefix: Path | None = None) -> dict[str, str]:
    """The environment for a dry run: no make flags from a parent make, and C
    messages, which the trace parser reads."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEFILES", "GNUMAKEFLAGS")
    }
    env["LC_ALL"] = "C"
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return env


def make_trace(
    make: str, cwd: Path, args: Sequence[str], path_prefix: Path | None = None
) -> Trace:
    proc = subprocess.run(
        [make, "--dry-run", "--trace", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        env=_make_env(path_prefix),
        check=False,
    )
    return Trace(proc.returncode, tuple(parse_trace(proc.stdout, cwd)), proc.stderr)


def parse_trace(output: str, cwd: Path) -> list[Traced]:
    """The commands in the stdout of `make --dry-run --trace`.

    GNU make names the FIRST RECIPE LINE of each rule that it runs, not the rule
    header. A command that ends in `\\` goes on to the next line."""
    dirs = [cwd]
    found: list[Traced] = []
    where: tuple[Path, int] | None = None
    index = 0
    pending: list[str] = []
    for raw in output.splitlines():
        if pending:
            pending.append(raw)
            if not raw.endswith("\\"):
                found.append(Traced(*where, index, "\n".join(pending), dirs[-1]))
                index, pending = index + 1, []
            continue
        if m := ENTER.match(raw):
            dirs.append(Path(m.group("dir")))
            continue
        if LEAVE.match(raw):
            if len(dirs) > 1:
                dirs.pop()
            continue
        if MAKE_MESSAGE.match(raw):
            continue
        if m := TRACE_RULE.match(raw):
            path = Path(m.group("file"))
            where = (
                (path if path.is_absolute() else dirs[-1] / path).resolve(),
                int(m.group("line")),
            )
            index = 0
            continue
        if where is None:
            continue
        if raw.endswith("\\"):
            pending = [raw]
        else:
            found.append(Traced(*where, index, raw, dirs[-1]))
            index += 1
    if pending:
        # The output ended inside a continued command. Keep what it printed.
        found.append(Traced(*where, index, "\n".join(pending), dirs[-1]))
    return found


def recipe_lines(text: str, first_line: int) -> list[RecipeLine]:
    """The logical recipe lines of the rule whose first recipe line is FIRST_LINE."""
    lines = text.splitlines()
    out: list[RecipeLine] = []
    i = first_line - 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("\t"):
            start = i
            while lines[i].endswith("\\") and i + 1 < len(lines):
                i += 1
            prefix = (
                re.match(r"[@+\-\s]*", line[1:])
                .group(0)
                .replace(" ", "")
                .replace("\t", "")
            )
            out.append(RecipeLine(start + 1, i - start + 1, prefix))
        elif line.strip() and not line.startswith("#"):
            break
        i += 1
    return out


@dataclass(frozen=True)
class MakeShell:
    """The shell that a make runs its recipe lines with."""

    shell: str
    flags: str
    oneshell: bool


def make_shell(make: str, cwd: Path) -> MakeShell:
    """SHELL, .SHELLFLAGS and .ONESHELL as the makefile in CWD sets them.

    `--eval` runs before the makefiles are read, so it would show the defaults.
    The database that `-p` prints shows the values after the makefiles."""
    proc = subprocess.run(
        [make, "-p", "--dry-run", "-C", str(cwd), ".cts-trace-probe"],
        capture_output=True,
        text=True,
        env=_make_env(),
        check=False,
    )
    values: dict[str, str] = {}
    oneshell = False
    for line in proc.stdout.splitlines():
        m = re.match(r"^(?P<name>SHELL|\.SHELLFLAGS) :?= ?(?P<value>.*)$", line)
        if m:
            values[m.group("name")] = m.group("value")
        oneshell = oneshell or line == ".ONESHELL:"
    if "SHELL" not in values:
        raise TraceError(
            f"`make -p` in {cwd} printed no SHELL value:\n{proc.stderr.strip()}"
        )
    return MakeShell(values["SHELL"], values.get(".SHELLFLAGS", "-c"), oneshell)


class ShellOptions(NamedTuple):
    """How a shell runs: its interpreter, the options for `set`, and whether a
    failed command stops it (errexit) or fails a pipeline (pipefail)."""

    interpreter: str
    options: list[str]
    errexit: bool
    pipefail: bool


def shell_options(shell: str, flags: str) -> ShellOptions:
    """The options of SHELL when it runs with FLAGS."""
    words = shlex.split(shell) + shlex.split(flags)
    interpreter = words[0]
    options: list[str] = []
    for word in words[1:]:
        if word in ("{0}", "-c"):
            continue
        if re.fullmatch(r"-[A-Za-z]+", word):
            word = word.replace("c", "")
            if word == "-":
                continue
        options.append(word)
    errexit = "errexit" in options or any(
        re.fullmatch(r"-[A-Za-z]*e[A-Za-z]*", o) for o in options
    )
    pipefail = "pipefail" in options and program_name(interpreter) in (
        "bash",
        "zsh",
        "ksh",
    )
    return ShellOptions(interpreter, options, errexit, pipefail)


def _cannot_fail(node) -> bool:
    if node.type != "command":
        return False
    words = command_words(node)
    return bool(words) and program_name(words[0]) in NO_FAILURE


class LostStatus(NamedTuple):
    """A command whose failure the shell does not report: its row in the
    command, whether a `;` or a `|` loses it, and its text."""

    row: int
    kind: str
    text: str


def dropped_statuses(command: str, errexit: bool, pipefail: bool) -> list[LostStatus]:
    """Each command whose failure COMMAND does not report.

    Without errexit, `a; b` reports only `b`. Without pipefail, `a | b` reports
    only `b`. A statement that the next one checks through `$?` is reported."""
    root = parse(command)
    found: list[LostStatus] = []
    if not errexit:
        statements = [c for c in root.named_children if c.type != "comment"]
        for statement, after in zip(statements, statements[1:]):
            if "$?" in node_text(after) or _cannot_fail(statement):
                continue
            found.append(
                LostStatus(statement.start_point[0], "sequence", node_text(statement))
            )
    if not pipefail:
        for pipeline in iter_nodes(root, "pipeline"):
            stages = [c for c in pipeline.named_children if c.type != "comment"]
            for stage in stages[:-1]:
                if not _cannot_fail(stage):
                    found.append(
                        LostStatus(stage.start_point[0], "pipe", node_text(stage))
                    )
    return found


# --------------------------------------------------------------------------- #
# Make $(shell …) fail
# --------------------------------------------------------------------------- #
def _close(text: str, start: int, opener: str) -> int:
    """The index of the delimiter that closes the one at START, or -1.

    GNU make counts only the delimiter type that opened the reference, so
    `$(shell echo })` closes at the `)`."""
    closer = ")" if opener == "(" else "}"
    depth = 0
    for i in range(start, len(text)):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i
    return -1


def shell_calls(text: str) -> list[ShellCall]:
    """Each `$(shell …)` and `${shell …}` call in the makefile TEXT.

    GNU make syntax has no Python parser, so this reads only what the fault
    injection needs: where a call starts and where its delimiter closes. A call
    on a line that starts with `#` is in a comment. A call that never closes is
    a make syntax error, and make reports it."""
    calls: list[ShellCall] = []
    for match in re.finditer(r"\$(?P<open>[({])shell\s", text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        if text[line_start : match.start()].lstrip().startswith("#"):
            continue
        end = _close(text, match.start() + 1, match.group("open"))
        if end == -1:
            continue
        line = text.count("\n", 0, match.start()) + 1
        calls.append(ShellCall(line, text[match.end() : end]))
    return calls


def _strip_make_refs(body: str) -> str:
    """BODY with each make reference made a placeholder, and `$$` made `$`."""
    out: list[str] = []
    i = 0
    while i < len(body):
        if body[i] != "$" or i + 1 >= len(body):
            out.append(body[i])
            i += 1
            continue
        nxt = body[i + 1]
        if nxt == "$":
            out.append("$")
            i += 2
        elif nxt in "({":
            end = _close(body, i + 1, nxt)
            out.append(MAKE_REF_PLACEHOLDER)
            i = len(body) if end == -1 else end + 1
        else:
            out.append(MAKE_REF_PLACEHOLDER)
            i += 2
    return "".join(out)


def fault_target(body: str) -> tuple[str, tuple[str, ...]] | None:
    """(program, literal arguments) of the first command in a `$(shell …)` BODY.

    The stub fails a call only when all these literal arguments are in its argv,
    so `git rev-parse --short HEAD` fails and `git rev-parse --show-toplevel`
    does not. None when the program name is itself an expansion."""
    for node in iter_nodes(parse(_strip_make_refs(body)), "command"):
        words = command_words(node)
        if not words:
            continue
        if MAKE_REF_PLACEHOLDER in words[0] or "$" in words[0]:
            return None
        literals = tuple(
            unquote(w)
            for w in words[1:]
            if MAKE_REF_PLACEHOLDER not in w and "$" not in w
        )
        return program_name(words[0]), literals
    return None


def write_stub(
    stub_dir: Path, program: str, literals: tuple[str, ...], marker: Path
) -> bool:
    """A PROGRAM in STUB_DIR that exits 1 when its argv holds every one of
    LITERALS, and runs the real PROGRAM otherwise. False when PROGRAM is a shell
    builtin or is not on PATH, because no stub on PATH can replace it."""
    real = shutil.which(program)
    if program in SHELL_BUILTINS or real is None:
        return False
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / program
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        f"if all(w in sys.argv[1:] for w in {list(literals)!r}):\n"
        f"    open({str(marker)!r}, 'a').close()\n"
        "    sys.exit(1)\n"
        f"os.execv({real!r}, [{real!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return True


def word_count(trace: Trace) -> int:
    """How many non-empty words the commands of TRACE hand to programs. A word
    that a failed `$(shell …)` empties out (`"x"` → `""`) stops counting."""
    total = 0
    for command in trace.commands:
        for node in iter_nodes(
            parse(command.text), "word", "string_content", "raw_string", "number"
        ):
            if unquote(node_text(node)).strip():
                total += 1
    return total


# --------------------------------------------------------------------------- #
# Run the content lints over what CI runs
# --------------------------------------------------------------------------- #
class SourceLine(NamedTuple):
    """Where a line of a synthetic file came from. ADDED marks a line that
    trace-ci wrote itself, such as the shebang."""

    path: str
    line: int
    added: bool


class BodyLine(NamedTuple):
    """A line of CI command text and its source line."""

    text: str
    path: str
    line: int


@dataclass(frozen=True)
class Synthetic:
    """A temporary file of CI commands, and the source line of each of its lines."""

    path: str
    lines: tuple[str, ...]
    sources: tuple["SourceLine", ...]


def _shell_file(
    path: str,
    shell: str,
    flags: str,
    body: list[BodyLine],
    header: tuple[str, int],
) -> Synthetic:
    interpreter, options, _, _ = shell_options(shell, flags)
    lines = [f"#!{interpreter}"]
    sources = [SourceLine(*header, True)]
    if options:
        lines.append("set " + " ".join(options))
        sources.append(SourceLine(*header, True))
    for text, source, line in body:
        lines.append(text)
        sources.append(SourceLine(source, line, False))
    return Synthetic(path, tuple(lines), tuple(sources))


LINT_LINE = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+):\s?(?P<msg>.*)$")
ANNOTATION_LINE = re.compile(
    r"^::(?:error|warning|notice) file=(?P<path>[^,:]+),line=(?P<line>\d+)[^:]*::(?P<msg>.*)$"
)


def run_lints(synthetics: list[Synthetic], checks: list[Check]) -> list[TraceFinding]:
    """The findings of CHECKS over SYNTHETICS, at the source lines."""
    if not synthetics:
        return []
    by_path = {s.path: s for s in synthetics}
    with tempfile.TemporaryDirectory(prefix="trace-ci-") as tmp:
        work = Path(tmp)
        for s in synthetics:
            target = work / s.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(s.lines) + "\n", encoding="utf-8")
        # Several lints read `git ls-files`, so the files are tracked in a repo.
        subprocess.run(["git", "init", "-q", str(work)], check=True)
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        selectors = [a for c in checks for a in ("--select", f"check:{c.module}")]
        env = dict(os.environ)
        package_parent = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = os.pathsep.join(
            p for p in (package_parent, env.get("PYTHONPATH")) if p
        )
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ci_truth_serum.run_selection",
                *selectors,
                *by_path,
            ],
            cwd=work,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        output = proc.stdout + proc.stderr
        prefix = f"{work}{os.sep}"
    findings: list[TraceFinding] = []
    for raw in output.splitlines():
        match = LINT_LINE.match(raw) or ANNOTATION_LINE.match(raw)
        path = match.group("path").removeprefix(prefix) if match else None
        if path in by_path:
            synthetic = by_path[path]
            row = min(int(match.group("line")), len(synthetic.sources)) - 1
            source, line, added = synthetic.sources[max(row, 0)]
            message = match.group("msg")
            if added:
                message = f"{message} (on the shell options that trace-ci added for this step)"
            findings.append(TraceFinding(source, line, "lint", message))
        elif any(p in raw for p in by_path):
            # A synthetic file is named, but not in a form this parser knows.
            # Keep the line instead of dropping a finding.
            path = next(p for p in by_path if p in raw)
            source, line, _ = next(
                (s for s in by_path[path].sources if not s.added),
                by_path[path].sources[0],
            )
            # The lint names the temporary file. Its line numbers count in the
            # command as CI expands it, not in the source file.
            message = re.sub(rf"^\S*{re.escape(path)}:\s*", "", raw).replace(
                "this file", "the command that CI expands here"
            )
            findings.append(TraceFinding(source, line, "lint", message))
    if proc.returncode != 0 and not findings:
        raise TraceError(
            "the content lints failed, but trace-ci found no finding in their "
            f"output, so it cannot report a result:\n{output}"
        )
    return findings


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _annotated(repo: Path, finding: TraceFinding) -> bool:
    path = repo / finding.path
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    near = lines[max(finding.line - 2, 0) : finding.line]
    return any(ANNOTATION.search(line) for line in near)


def _slug(text: str) -> str:
    """TEXT made safe for one part of a file name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


def _rel(repo: Path, path: Path) -> str:
    return os.path.relpath(path, repo)


def _content_checks(selects: list[str], ignores: list[str]) -> list[Check]:
    checks = [c for c in resolve_all(selects, ignores) if c.kind in CONTENT_KINDS]
    if not checks:
        raise TraceError(
            "the selection matches no lint that reads shell or Dockerfiles, so "
            "trace-ci would lint nothing; widen --select"
        )
    return checks


def trace(
    repo: Path,
    selects: list[str],
    ignores: list[str],
    goal: str,
    inject: bool,
    notes: list[str],
) -> list[TraceFinding]:
    """The findings for the repository at REPO. Notes for stderr go into NOTES,
    which keeps them when the trace stops with an error."""
    make = gnu_make()
    checks = _content_checks(selects, ignores)
    files = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    surfaces = discover(files)
    other = sorted(set(surfaces) - FOLLOWED - {"make", "dockerfile"})
    if other:
        listed = "; ".join(f"{k} ({', '.join(surfaces[k][:3])})" for k in other)
        notes.append(f"trace-ci does not read these CI systems yet: {listed}")
    if not FOLLOWED & set(surfaces):
        raise TraceError(
            "no CI file that trace-ci follows (CircleCI or GitHub Actions); "
            "it has nothing to trace"
        )

    findings: list[TraceFinding] = []
    synthetics: list[Synthetic] = []
    steps: list[CiStep] = []
    images: list[Image] = []
    for source in surfaces.get("circleci", []):
        found_steps, found_images = circleci_steps(
            (repo / source).read_text(encoding="utf-8"), source
        )
        steps += found_steps
        images += found_images
    for source in surfaces.get("github-actions", []):
        steps += workflow_steps((repo / source).read_text(encoding="utf-8"), source)

    if images:
        synthetics.append(
            Synthetic(
                "ci-images/Dockerfile",
                tuple(f"FROM {i.image}" for i in images),
                tuple(SourceLine(i.source, i.line, False) for i in images),
            )
        )

    calls: list[MakeCall] = []
    for n, step in enumerate(steps):
        if step.lint:
            body = [
                BodyLine(t, step.source, step.lines[min(i, len(step.lines) - 1)])
                for i, t in enumerate(step.command.splitlines())
            ]
            synthetics.append(
                _shell_file(
                    f"ci-steps/{n}-{_slug(step.job)}.sh",
                    step.shell,
                    "",
                    body,
                    (step.source, step.lines[0]),
                )
            )
            options = shell_options(step.shell, "")
            for row, kind, text in dropped_statuses(
                step.command, options.errexit, options.pipefail
            ):
                findings.append(
                    _drop_finding(
                        step.source,
                        step.lines[min(row, len(step.lines) - 1)],
                        kind,
                        text,
                        step.shell,
                    )
                )
        found, untraced = make_calls(step, repo)
        calls += found
        for line in untraced:
            findings.append(
                TraceFinding(
                    step.source,
                    line,
                    "untraced",
                    "this `make` call has an expansion or follows a `cd` that trace-ci cannot resolve, so its commands are not checked",
                )
            )

    baselines: dict[MakeCall, Trace] = {}
    reached_dirs: set[Path] = set()
    for call in calls:
        result = make_trace(make, call.cwd, call.args)
        baselines[call] = result
        reached_dirs.add(call.cwd)
        reached_dirs.update(c.cwd for c in result.commands)
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or [""])[-1]
            findings.append(
                TraceFinding(
                    call.step.source,
                    call.line,
                    "untraced",
                    f"`make {' '.join(call.args)}` fails as a dry run (exit {result.returncode}: {last}), so trace-ci cannot see what it runs",
                )
            )

    shells: dict[Path, MakeShell] = {}
    seen: set[Traced] = set()
    for call, result in baselines.items():
        for command in result.commands:
            if command in seen:
                continue
            seen.add(command)
            if command.cwd not in shells:
                shells[command.cwd] = make_shell(make, command.cwd)
            shell = shells[command.cwd]
            text = command.makefile.read_text(encoding="utf-8", errors="replace")
            recipe = recipe_lines(text, command.first_line)
            entry = (
                recipe[command.index]
                if command.index < len(recipe)
                else RecipeLine(command.first_line, 1, "")
            )
            path = _rel(repo, command.makefile)
            rows = command.text.splitlines()
            body = [
                BodyLine(t, path, entry.start + min(i, entry.count - 1))
                for i, t in enumerate(rows)
            ]
            synthetics.append(
                _shell_file(
                    f"make/{len(synthetics)}-{_slug(path)}-L{entry.start}.sh",
                    shell.shell,
                    shell.flags,
                    body,
                    (path, entry.start),
                )
            )
            if "-" in entry.prefix:
                findings.append(
                    TraceFinding(
                        path,
                        entry.start,
                        "ignored-error",
                        "this recipe line starts with `-`, so make ignores its exit status and CI passes when it fails; remove the `-`, or handle the failure in the command",
                    )
                )
            if shell.oneshell:
                continue
            options = shell_options(shell.shell, shell.flags)
            for row, kind, part in dropped_statuses(
                command.text, options.errexit, options.pipefail
            ):
                findings.append(
                    _drop_finding(
                        path,
                        entry.start + min(row, entry.count - 1),
                        kind,
                        part,
                        f"{shell.shell} {shell.flags}",
                    )
                )

    findings += run_lints(synthetics, checks)
    if inject:
        findings += _inject_faults(
            repo, make, surfaces.get("make", []), baselines, notes
        )
    findings += _coverage(repo, make, surfaces.get("make", []), reached_dirs, goal)

    unique = sorted(set(findings), key=lambda f: (f.path, f.line, f.kind, f.message))
    kept = [f for f in unique if not _annotated(repo, f)]
    return kept


def _drop_finding(
    path: str, line: int, kind: str, text: str, shell: str
) -> TraceFinding:
    snippet = " ".join(text.split())[:80]
    if kind == "pipe":
        why = f"this pipe stage can fail and CI does not see it, because `{shell}` has no pipefail: `{snippet}`"
    else:
        why = f"this command can fail and CI does not see it, because `{shell}` has no errexit and the next command sets the status: `{snippet}`"
    return TraceFinding(path, line, f"lost-status-{kind}", why)


def _inject_faults(
    repo: Path,
    make: str,
    makefiles: list[str],
    baselines: dict[MakeCall, Trace],
    notes: list[str],
) -> list[TraceFinding]:
    findings: list[TraceFinding] = []
    skipped = unexercised = 0
    with tempfile.TemporaryDirectory(prefix="trace-ci-stubs-") as tmp:
        for n, (makefile, call_site) in enumerate(
            (mf, c)
            for mf in makefiles
            for c in shell_calls(
                (repo / mf).read_text(encoding="utf-8", errors="replace")
            )
        ):
            target = fault_target(call_site.body)
            stub_dir, marker = Path(tmp) / str(n), Path(tmp) / f"{n}.fired"
            if target is None or not write_stub(stub_dir, *target, marker):
                skipped += 1
                continue
            effects: list[str] = []
            fired = False
            for call, base in baselines.items():
                if base.returncode != 0:
                    continue
                marker.unlink(missing_ok=True)
                faulted = make_trace(make, call.cwd, call.args, stub_dir)
                if not marker.exists():
                    continue
                fired = True
                if faulted.returncode != 0:
                    continue
                lost = sum(
                    (
                        Counter((c.makefile, c.first_line) for c in base.commands)
                        - Counter((c.makefile, c.first_line) for c in faulted.commands)
                    ).values()
                )
                fewer = word_count(base) - word_count(faulted)
                if lost > 0 or fewer > 0:
                    lessened = " and ".join(
                        part
                        for part in (
                            f"{lost} fewer commands" if lost > 0 else "",
                            f"{fewer} fewer arguments" if fewer > 0 else "",
                        )
                        if part
                    )
                    effects.append(
                        f"`make {' '.join(call.args)}` ({call.step.source}:{call.line}) runs {lessened}"
                    )
            unexercised += not fired
            if effects:
                expression = " ".join(call_site.body.split())
                if len(expression) > 60:
                    expression = expression[:59] + "…"
                findings.append(
                    TraceFinding(
                        makefile,
                        call_site.line,
                        "vacuous-on-failure",
                        f"when `$(shell {expression})` fails, make uses its empty output and still exits 0: "
                        + "; ".join(effects[:3])
                        + (
                            f"; and {len(effects) - 3} more CI calls"
                            if len(effects) > 3
                            else ""
                        )
                        + ". Stop on the failure: test `$(.SHELLSTATUS)` after the call, or `$(error …)` when the value is empty",
                    )
                )
    if skipped:
        notes.append(
            f"{skipped} `$(shell …)` calls run a builtin, a program that is not "
            "on PATH, or a program that an expansion names, so trace-ci could not "
            "make them fail"
        )
    if unexercised:
        notes.append(
            f"{unexercised} `$(shell …)` calls did not run in any traced CI call, "
            "or ran their program by an absolute path, so no fault reached them"
        )
    return findings


def _coverage(
    repo: Path, make: str, makefiles: list[str], reached: set[Path], goal: str
) -> list[TraceFinding]:
    findings: list[TraceFinding] = []
    if not goal:
        return findings
    for makefile in makefiles:
        if not identify.tags_from_filename(makefile) >= {
            "makefile"
        } or makefile.endswith(".mk"):
            continue
        directory = (repo / makefile).parent.resolve()
        if directory in reached:
            continue
        probe = subprocess.run(
            [make, "--dry-run", "-C", str(directory), goal],
            capture_output=True,
            text=True,
            env=_make_env(),
            check=False,
        )
        if probe.returncode == 0:
            findings.append(
                TraceFinding(
                    makefile,
                    1,
                    "uncovered",
                    f"this makefile has a `{goal}` goal, but no CI step runs make in this directory, so its checks never run in CI",
                )
            )
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="trace-ci", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--repo", default=".", help="the repository to trace (default: .)"
    )
    parser.add_argument(
        "--select",
        action="append",
        default=[],
        help="the content lints to run, as in run_selection (default: tier:1)",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="lints to remove from the selection",
    )
    parser.add_argument(
        "--goal",
        default="check",
        help="the make goal that the coverage check looks for; '' turns the check off",
    )
    parser.add_argument(
        "--no-fault-injection",
        action="store_true",
        help="do not make each $(shell …) call fail",
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    notes: list[str] = []
    try:
        findings = trace(
            repo,
            args.select or ["tier:1"],
            args.ignore,
            args.goal,
            not args.no_fault_injection,
            notes,
        )
    except (TraceError, SelectorError) as exc:
        for note in notes:
            print(f"trace-ci: note: {note}", file=sys.stderr)
        print(f"trace-ci: error: {exc}", file=sys.stderr)
        return 2
    for note in notes:
        print(f"trace-ci: note: {note}", file=sys.stderr)
    if args.format == "json":
        print(json.dumps([asdict(f) for f in findings], indent=2))
    else:
        for f in findings:
            print(f"{f.path}:{f.line}: [{f.kind}] {f.message}")
        print(f"trace-ci: {len(findings)} finding(s)", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Trace the commands that CI really runs, and find where CI can pass over a failure.

A CircleCI job can run `make check`, and the real work is in the `Makefile`
recipes that it reaches. No lint of this pack reads those recipes as CI. This
tool starts from the CI files and follows what they run. The README section
"Apply: trace the commands CI runs with trace-ci" states the full contract::

    trace-ci [--repo DIR] [--select SELECTOR] [--goal GOAL] [--format json]

1. It runs `make --dry-run --trace` for each `make` that a CircleCI or GitHub
   Actions step calls. The content lints read each printed command, and each
   finding points back to its source line.
2. It makes each `$(shell …)` call fail, and reports a call when make still
   exits 0 but runs fewer commands or arguments.
3. It reports a status that the recipe shell loses in `a; b`, in `a | b`, or
   in a loop, and a recipe line that starts with `-`.
4. It reports a directory whose `Makefile` has a `check` goal that no CI step
   reaches.

Trust the tree as you trust `make`: a dry run still runs each `$(shell …)`
call and each `+` or `$(MAKE)` recipe line. A `# trace-ci-ok: <reason>`
comment on a finding's line, or on the line above it, accepts the finding.
Exit status: 0 for no finding, 1 for findings, 2 when the tool cannot trace.
"""

import argparse
import fnmatch
import json
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import NamedTuple, Protocol

import yaml
from identify import identify

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    condition_parts,
    iter_nodes,
    node_text,
    parse,
    program_name,
    unquote,
)
from _cts_comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
    shell_comments,
    yaml_comments,
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

# CircleCI runs a step with this shell when the step and its job name none.
CIRCLECI_DEFAULT_SHELL = "/bin/bash -eo pipefail"
WINDOWS_SHELL = "powershell.exe"
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

# A rule from make's built-in rules is named `<builtin>`, with no line.
TRACE_RULE = re.compile(
    r"^(?P<file>.+?)(?::(?P<line>\d+))?: (?:target '.*' does not exist|update target '.*' due to: .*)$"
)
MAKE_MESSAGE = re.compile(r"^\S*make(?:\[\d+\])?: ")
ENTER = re.compile(
    r"^\S*make(?:\[(?P<level>\d+)\])?: Entering directory '(?P<dir>.*)'$"
)
LEAVE = re.compile(r"^\S*make(?:\[\d+\])?: Leaving directory '(?P<dir>.*)'$")
MAKE_REF_PLACEHOLDER = "__cts_make_ref__"
# A GitHub Actions `${{ … }}` or a CircleCI `<< … >>` template expression. A
# CircleCI name always holds a dot (`parameters.x`), so `cat <<EOF >> f` is no match.
CI_EXPRESSION = re.compile(r"\$\{\{.*?\}\}|<<\s*\w+(?:\.[\w-]+)+\s*>>")
CI_PLACEHOLDER = "${CTS_CI_EXPRESSION}"
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
    # Relative to the checkout. None when trace-ci cannot find it there.
    working_directory: str | None
    lint: bool
    # The environment variables that CI sets to a literal value for the step.
    env: tuple[tuple[str, str], ...] = ()


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
    # The step's environment, and each `VAR=value` written before `make`.
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Traced:
    """One command that a dry run of make prints.

    RUN numbers the rule runs of one trace, so the commands of one run can be
    counted. LEVEL is 0 for the make that CI calls and N for a `make[N]`.
    OPTIONS and GOALS are the arguments of the make that printed the command."""

    makefile: Path
    first_line: int
    index: int
    text: str
    cwd: Path
    run: int = 0
    level: int = 0
    options: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()


class Follow(NamedTuple):
    """The rule that a make ran after rule run RUN: its MAKEFILE, and LINE, its
    first recipe line."""

    run: int
    makefile: Path
    line: int


@dataclass(frozen=True)
class Trace:
    """The result of one dry run. HIDDEN and FOLLOWS are as `parse_trace`
    returns them."""

    returncode: int
    commands: tuple[Traced, ...]
    stderr: str
    hidden: str = ""
    follows: tuple[Follow, ...] = ()


@dataclass(frozen=True)
class RecipeLine:
    """A logical recipe line: its first line, its line count, its `@+-` prefix,
    and whether a dry run still runs it (a `+` line, or a `$(MAKE)` line)."""

    start: int
    count: int
    prefix: str
    runs: bool = False


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
def _pairs(node: yaml.MappingNode) -> list[tuple[str, yaml.Node]]:
    """The keys and values of NODE, with each `<<: *anchor` merged in.

    `yaml.compose` leaves merge keys as they are written. PyYAML's own
    `flatten_mapping` merges them, and it puts the merged pairs first, so the
    LAST pair of a key is the value that a YAML loader gives it."""
    yaml.SafeLoader("").flatten_mapping(node)
    return [(k.value, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)]


def _child(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    return dict(_pairs(node)).get(key)


def _items(node: yaml.Node | None) -> list[tuple[str, yaml.Node]]:
    if not isinstance(node, yaml.MappingNode):
        return []
    return list(dict(_pairs(node)).items())


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


# The variables that each CI system sets in every job.
CIRCLECI_ENV = {"CI": "true", "CIRCLECI": "true"}
GITHUB_ENV = {"CI": "true", "GITHUB_ACTIONS": "true"}
# The directory that GitHub runs a job in. Only paths relative to the checkout
# leave the readers, so any absolute name serves.
GITHUB_WORKSPACE = "/github/workspace"
# GitHub runs a step with this shell when the step and its defaults name none.
# It runs the `bash` and `sh` keywords with these options.
GITHUB_DEFAULT_SHELL = "bash -e {0}"
GITHUB_WINDOWS_SHELL = "pwsh"
GITHUB_SHELLS = {
    "bash": "bash --noprofile --norc -eo pipefail {0}",
    "sh": "sh -e {0}",
}


def _env(node: yaml.Node | None) -> dict[str, str]:
    """The variables of an `env:` or `environment:` mapping that have a literal
    value. A value with an expansion or a CI template expression is known only
    in CI, so the variable is left out."""
    return {
        k: v.value
        for k, v in _items(node)
        if isinstance(v, yaml.ScalarNode)
        and "$" not in v.value
        and not CI_EXPRESSION.search(v.value)
    }


@dataclass(frozen=True)
class _Job:
    """What a CI job gives each of its steps: the SHELL and DIRECTORY of a step
    that names none, the CHECKOUT directory that holds the code, and the ENV.
    DIRECTORY and CHECKOUT are absolute, or None when trace-ci cannot know
    them. LINT is whether the content lints read the steps of the job."""

    source: str
    name: str
    shell: str
    directory: str | None
    checkout: str | None
    env: dict[str, str]
    lint: bool


class _Run(NamedTuple):
    """A `run:` step, with the defaults of its job applied. DIRECTORY is
    absolute, or None when trace-ci cannot know it."""

    command: yaml.Node | None
    line: int
    shell: str
    directory: str | None
    env: yaml.Node | None


class _Call(NamedTuple):
    """A step that runs the step list STEPS of the file SOURCE: a CircleCI
    command or a local GitHub composite action, which NAME names."""

    name: str
    source: str
    steps: yaml.Node | None


class _ReadStep(Protocol):
    """Turns one step of a CI file into the runs and calls that it holds."""

    def __call__(self, entry: yaml.Node) -> list[_Run | _Call]: ...


def _job_steps(
    job: _Job,
    source: str,
    entries: yaml.Node | None,
    read: _ReadStep,
    calling: tuple[str, ...] = (),
) -> list[CiStep]:
    """The steps of JOB in the step list ENTRIES of SOURCE. A call runs where a
    job calls it, with the job's shell, directory and environment, so its
    steps are read at each call. CALLING names the calls that ENTRIES is
    inside, so a call to itself stops the trace instead of looping."""
    steps: list[CiStep] = []
    for entry in _seq(entries):
        for item in read(entry):
            if isinstance(item, _Run):
                steps.append(_ci_step(job, source, item))
                continue
            if item.name in calling:
                raise TraceError(
                    f"{source}: {item.name!r} calls itself, "
                    f"through {' -> '.join((*calling, item.name))}"
                )
            steps += _job_steps(
                job, item.source, item.steps, read, (*calling, item.name)
            )
    return steps


def _ci_step(job: _Job, source: str, run: _Run) -> CiStep:
    """The step RUN of JOB, which the file SOURCE holds."""
    if not isinstance(run.command, yaml.ScalarNode):
        raise TraceError(
            f"{source}:{run.line}: this `run:` step has no "
            "command string, so trace-ci cannot read what it runs"
        )
    directory = (
        None
        if run.directory is None or job.checkout is None
        else _inside(posixpath.relpath(run.directory, job.checkout))
    )
    return CiStep(
        source,
        job.name,
        run.shell,
        run.command.value,
        scalar_lines(run.command),
        directory,
        lint=job.lint and posix_shell(run.shell),
        env=tuple((job.env | _env(run.env)).items()),
    )


def _image(source: str, job: str, node: yaml.Node | None) -> list[Image]:
    """The image that NODE names for JOB, or none when NODE is not a literal
    image name."""
    if (
        not isinstance(node, yaml.ScalarNode)
        or "$" in node.value
        or CI_EXPRESSION.search(node.value)
    ):
        return []
    return [Image(source, job, node.value, node.start_mark.line + 1)]


def _ci_path(path: str, base: str | None) -> str | None:
    """PATH as an absolute path, where `~` stays the first part (`/~/project`).
    A relative PATH is read from BASE. None when PATH has an expansion, or is
    relative and BASE is None."""
    if "$" in path:
        return None
    if path == "~" or path.startswith("~/"):
        return posixpath.normpath("/" + path)
    if path.startswith("/"):
        return posixpath.normpath(path)
    return None if base is None else posixpath.normpath(posixpath.join(base, path))


def _inside(path: str) -> str | None:
    """PATH, relative to the checkout, normalised. None when it leaves the
    checkout, is absolute, or has an expansion. The checkout itself is ""."""
    path = posixpath.normpath(path)
    if "$" in path or path.startswith(("/", "~", "..")):
        return None
    return "" if path == "." else path


def circleci_steps(
    text: str, source: str, repo: Path, notes: list[str]
) -> tuple[list[CiStep], list[Image]]:
    """The `run:` steps and the job images of a CircleCI configuration.

    A reusable command under `commands:` is a call (see `_job_steps`). The
    steps of a `when` or an `unless` step are read as steps that run. A job
    takes its images, shell, directory and environment from the `executors:`
    entry that it names, and its own keys win. An executor from an orb is not
    in the file, so trace-ci cannot read it, and it says so in NOTES. A Windows
    job runs PowerShell unless it names a shell. REPO is not read: every
    reader takes it."""
    del repo
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    commands = dict(_items(_child(root, "commands")))
    executors = dict(_items(_child(root, "executors")))
    windows_orbs = {
        alias
        for alias, orb in _items(_child(root, "orbs"))
        if (_scalar(orb) or "").startswith("circleci/windows@")
    }
    steps: list[CiStep] = []
    images: list[Image] = []
    for name, body in _items(_child(root, "jobs")):
        executor_name = _scalar(_child(body, "executor")) or _scalar(
            _child(_child(body, "executor"), "name")
        )
        executor = executors.get(executor_name)
        if executor_name is not None and executor is None:
            notes.append(
                f"{source}: job {name!r} runs on the executor {executor_name!r}, "
                "which is not in this file (an orb executor), so trace-ci "
                "cannot read its images, and it reads its steps with the "
                "default shell of the job"
            )
        directory = _ci_path(
            _scalar(_job_field(body, executor, "working_directory")) or "~/project",
            None,
        )
        windows = _windows_job(body, executor, executor_name, windows_orbs)
        job = _Job(
            source,
            name,
            _scalar(_job_field(body, executor, "shell"))
            or (WINDOWS_SHELL if windows else CIRCLECI_DEFAULT_SHELL),
            directory,
            _circleci_checkout(body, directory),
            CIRCLECI_ENV
            | _env(_child(executor, "environment"))
            | _env(_child(body, "environment")),
            lint=True,
        )
        for entry in _seq(_job_field(body, executor, "docker")):
            images += _image(source, name, _child(entry, "image"))
        steps += _job_steps(
            job, source, _child(body, "steps"), partial(_circleci_step, job, commands)
        )
    return steps, images


def _windows_job(
    job: yaml.Node,
    executor: yaml.Node | None,
    executor_name: str | None,
    windows_orbs: set[str],
) -> bool:
    """Whether the CircleCI JOB runs on Windows: a Windows resource class or
    machine image of the job or its EXECUTOR, or an executor of an orb that
    WINDOWS_ORBS names (the aliases of `circleci/windows`)."""
    resource_class = _scalar(_job_field(job, executor, "resource_class")) or ""
    image = _scalar(_child(_job_field(job, executor, "machine"), "image")) or ""
    orb, slash, _ = (executor_name or "").partition("/")
    return (
        resource_class.startswith("windows.")
        or image.startswith("windows-")
        or (bool(slash) and orb in windows_orbs)
    )


def _job_field(
    job: yaml.Node, executor: yaml.Node | None, key: str
) -> yaml.Node | None:
    """The KEY of a CircleCI JOB, or of its EXECUTOR when the job has none."""
    own = _child(job, key)
    return own if own is not None else _child(executor, key)


def _circleci_step(
    job: _Job, commands: dict[str, yaml.Node], entry: yaml.Node
) -> list[_Run | _Call]:
    """The runs and calls in the CircleCI step ENTRY of JOB."""
    pairs = (
        [(entry.value, None)] if isinstance(entry, yaml.ScalarNode) else _items(entry)
    )
    found: list[_Run | _Call] = []
    for key, value in pairs:
        if key == "run":
            found.append(_circleci_run(job, value, entry.start_mark.line + 1))
        elif key in ("when", "unless"):
            for inner in _seq(_child(value, "steps")):
                found += _circleci_step(job, commands, inner)
        elif key in commands:
            found.append(_Call(key, job.source, _child(commands[key], "steps")))
    return found


def _circleci_run(job: _Job, run: yaml.Node, line: int) -> _Run:
    """The `run:` step whose value is RUN, on LINE of JOB. A relative
    `working_directory` is read from the job's directory."""
    if not isinstance(run, yaml.MappingNode):
        return _Run(run, line, job.shell, job.directory, None)
    wd = _scalar(_child(run, "working_directory"))
    return _Run(
        _child(run, "command"),
        line,
        _scalar(_child(run, "shell")) or job.shell,
        job.directory if wd is None else _ci_path(wd, job.directory),
        _child(run, "environment"),
    )


def _circleci_checkout(job: yaml.Node, job_dir: str | None) -> str | None:
    """Where JOB's `checkout` step puts the code. Its `path` is read from the
    job's directory, and the job's directory is the default. A job without a
    `checkout` step finds the code in `~/project`, the default checkout path."""
    for step in _seq(_child(job, "steps")):
        if _scalar(step) == "checkout":
            return job_dir
        if isinstance(step, yaml.MappingNode) and _child(step, "checkout") is not None:
            path = _scalar(_child(_child(step, "checkout"), "path"))
            return job_dir if path is None else _ci_path(path, job_dir)
    return "/~/project"


def workflow_steps(
    text: str, source: str, repo: Path, notes: list[str]
) -> tuple[list[CiStep], list[Image]]:
    """The `run:` steps and the job images of a GitHub Actions workflow.

    A step takes its shell and directory from the `defaults.run` of its job,
    and then of the workflow. A relative directory is read from the
    workspace. A job on a Windows runner runs `pwsh` unless it names a shell.
    A local composite action (`uses: ./dir`) in REPO is a call (see
    `_job_steps`). A remote action is code from another repository, so
    trace-ci does not read it, and it says so in NOTES for a local action
    that is not composite. The lints of this pack read the steps of a
    workflow already, so the content lints do not read them again."""
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    top_run = _child(_child(root, "defaults"), "run")
    top_env = GITHUB_ENV | _env(_child(root, "env"))
    steps: list[CiStep] = []
    images: list[Image] = []
    for name, body in _items(_child(root, "jobs")):
        job_run = _child(_child(body, "defaults"), "run")
        shell, wd = (
            _scalar(_child(job_run, key)) or _scalar(_child(top_run, key))
            for key in ("shell", "working-directory")
        )
        job = _Job(
            source,
            name,
            _github_shell(shell, _github_windows(_child(body, "runs-on"))),
            _ci_path(wd or ".", GITHUB_WORKSPACE),
            _github_checkout(_child(body, "steps")),
            top_env | _env(_child(body, "env")),
            lint=False,
        )
        services = [v for _, v in _items(_child(body, "services"))]
        for node in (_child(body, "container"), *services):
            image = node if isinstance(node, yaml.ScalarNode) else _child(node, "image")
            images += _image(source, name, image)
        steps += _job_steps(
            job,
            source,
            _child(body, "steps"),
            partial(_github_step, repo, job, notes),
        )
    return steps, images


def _github_shell(name: str | None, windows: bool = False) -> str:
    """The command that runs a step whose `shell:` is NAME, or that names none.
    A name that is not a keyword is a command already."""
    if name is None:
        return GITHUB_WINDOWS_SHELL if windows else GITHUB_DEFAULT_SHELL
    return GITHUB_SHELLS.get(name, name)


def _github_windows(runs_on: yaml.Node | None) -> bool:
    """Whether a job whose `runs-on:` is RUNS_ON runs on a Windows runner."""
    labels = [runs_on] if isinstance(runs_on, yaml.ScalarNode) else _seq(runs_on)
    return any((_scalar(label) or "").lower().startswith("windows") for label in labels)


def _github_checkout(steps: yaml.Node | None) -> str | None:
    """Where the first `actions/checkout` step of this repository puts the code:
    its `path`, read from the workspace. A job without one has the workspace."""
    for step in _seq(steps):
        action = (_scalar(_child(step, "uses")) or "").partition("@")[0]
        given = _child(step, "with")
        if action == "actions/checkout" and _child(given, "repository") is None:
            return _ci_path(_scalar(_child(given, "path")) or ".", GITHUB_WORKSPACE)
    return GITHUB_WORKSPACE


def _github_step(
    repo: Path, job: _Job, notes: list[str], entry: yaml.Node
) -> list[_Run | _Call]:
    """The run or the call in the GitHub step ENTRY of JOB."""
    line = entry.start_mark.line + 1
    run = _child(entry, "run")
    if run is not None:
        shell = _scalar(_child(entry, "shell"))
        wd = _scalar(_child(entry, "working-directory"))
        return [
            _Run(
                run,
                line,
                job.shell if shell is None else _github_shell(shell),
                job.directory if wd is None else _ci_path(wd, GITHUB_WORKSPACE),
                _child(entry, "env"),
            )
        ]
    uses = _scalar(_child(entry, "uses")) or ""
    if not uses.startswith("./"):
        return []
    directory = _inside(uses)
    found = [
        posixpath.join(directory, name)
        for name in ("action.yml", "action.yaml")
        if directory is not None and (repo / directory / name).is_file()
    ]
    if not found:
        raise TraceError(
            f"{job.source}:{line}: the local action {uses!r} has no action.yml "
            "in the repository, so trace-ci cannot read its steps"
        )
    action = yaml.compose(
        (repo / found[0]).read_text(encoding="utf-8"), Loader=yaml.SafeLoader
    )
    runs = _child(action, "runs")
    using = _scalar(_child(runs, "using"))
    if using != "composite":
        notes.append(
            f"{job.source}:{line}: the local action {uses!r} runs with "
            f"`using: {using}`, so trace-ci does not read what it runs"
        )
        return []
    return [_Call(found[0], found[0], _child(runs, "steps"))]


# The CI systems whose steps this tool reads, and the reader of each.
READERS = {"circleci": circleci_steps, "github-actions": workflow_steps}
FOLLOWED = frozenset(READERS)


def ci_shell_text(command: str) -> str:
    """COMMAND with each CI template expression (`${{ … }}`, `<< … >>`) made a
    shell expansion. The CI system fills them in before the shell runs, so the
    shell grammar must not read `<<` as a heredoc. The length of each line can
    change, but no line is added or removed."""
    return CI_EXPRESSION.sub(lambda _: CI_PLACEHOLDER, command)


# The nodes whose commands run in a subshell. A `cd` in one does not reach the
# commands after it.
SUBSHELL_SCOPES = frozenset(
    {"subshell", "command_substitution", "process_substitution", "pipeline"}
)
# The nodes whose commands run only on some paths, or only when called. A `cd`
# in one moves the directory for a later command in the same branch, and for
# no command in another branch. After the node, the directory is unknown.
GUARDS = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "case_item",
        "for_statement",
        "c_style_for_statement",
        "while_statement",
        "function_definition",
    }
)
BRANCH_CLAUSES = frozenset({"elif_clause", "else_clause", "case_item"})
DIR_COMMANDS = frozenset({"cd", "pushd", "popd"})
SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash"})
MAKE_PROGRAMS = frozenset({"make", "gmake"})


class Wrapper(NamedTuple):
    """A program that runs the command after its own arguments, and its options:
    - VALUES take a value, and FLAGS take none;
    - CHDIR change the directory, and each takes a value unless it is in FLAGS;
    - COMMAND take the command as their value (`env -S 'make check'`).
    FIRST are the words that come before the command, in order: a fixed
    subcommand, or None for any one word (the duration of `timeout`)."""

    values: frozenset[str] = frozenset()
    chdir: frozenset[str] = frozenset()
    first: tuple[str | None, ...] = ()
    flags: frozenset[str] = frozenset()
    command: frozenset[str] = frozenset()


# The options of each wrapper, from its manual. An option that no set lists
# can take the next word as its value, so `_launch` cannot tell whether that
# word is the command.
WRAPPERS = {
    "timeout": Wrapper(
        frozenset({"-s", "--signal", "-k", "--kill-after"}),
        first=(None,),
        flags=frozenset({"--foreground", "--preserve-status", "-v", "--verbose"}),
    ),
    "env": Wrapper(
        frozenset({"-u", "--unset"}),
        frozenset({"-C", "--chdir"}),
        flags=frozenset(
            {"-", "-i", "--ignore-environment", "-0", "--null", "-v", "--debug"}
        ),
        command=frozenset({"-S", "--split-string"}),
    ),
    "sudo": Wrapper(
        frozenset(
            {"-u", "--user", "-g", "--group", "-h", "--host", "-p", "--prompt"}
            | {"-C", "--close-from", "-r", "--role", "-t", "--type"}
            | {"-T", "--command-timeout", "-U", "--other-user"}
        ),
        # A login shell starts in the home directory of the target user.
        frozenset({"-D", "--chdir", "-i", "--login"}),
        flags=frozenset(
            {"-i", "--login", "-A", "--askpass", "-b", "--background"}
            | {"-E", "--preserve-env", "-H", "--set-home", "-K", "-k"}
            | {"--reset-timestamp", "-n", "--non-interactive", "-P"}
            | {"--preserve-groups", "-S", "--stdin", "-s", "--shell"}
        ),
    ),
    "nice": Wrapper(frozenset({"-n", "--adjustment"})),
    "time": Wrapper(
        frozenset({"-f", "--format", "-o", "--output"}),
        flags=frozenset(
            {"-p", "--portability", "-v", "--verbose", "-a", "--append"}
            | {"-q", "--quiet"}
        ),
    ),
    "nohup": Wrapper(),
    "exec": Wrapper(frozenset({"-a"}), flags=frozenset({"-c", "-l"})),
    "stdbuf": Wrapper(frozenset({"-i", "--input", "-o", "--output", "-e", "--error"})),
    "ionice": Wrapper(
        frozenset(
            {"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid"}
            | {"-u", "--uid"}
        ),
        flags=frozenset({"-t", "--ignore"}),
    ),
    "xvfb-run": Wrapper(
        frozenset(
            {"-n", "--server-num", "-e", "--error-file", "-f", "--auth-file"}
            | {"-p", "--xauth-protocol", "-s", "--server-args", "-w", "--wait"}
        ),
        flags=frozenset({"-a", "--auto-servernum", "-l", "--listen-tcp"}),
    ),
    "uv": Wrapper(
        frozenset(
            {"--with", "--with-requirements", "--with-editable", "--python", "-p"}
            | {"--extra", "--no-extra", "--group", "--no-group", "--only-group"}
            | {"--package", "--project", "--env-file", "--cache-dir"}
            | {"--index", "--default-index", "--index-url", "-i", "--config-file"}
            | {"--extra-index-url", "--find-links", "-f", "--index-strategy"}
            | {"--keyring-provider", "--allow-insecure-host", "--resolution"}
            | {"--prerelease", "--fork-strategy", "--exclude-newer"}
            | {"--exclude-newer-package", "--config-setting", "-C"}
            | {"--config-settings-package", "--no-build-isolation-package"}
            | {"--upgrade-package", "-P", "--reinstall-package"}
            | {"--refresh-package", "--no-build-package", "--no-binary-package"}
            | {"--link-mode", "--python-platform", "--python-preference"}
            | {"--color"}
        ),
        frozenset({"--directory"}),
        ("run",),
        frozenset(
            {"--frozen", "--locked", "--no-sync", "--isolated", "--all-extras"}
            | {"--no-dev", "--dev", "--all-groups", "--no-default-groups"}
            | {"--no-project", "--active", "--offline", "-q", "--quiet", "-v"}
            | {"--verbose", "--no-cache", "-n", "--refresh", "-U", "--upgrade"}
            | {"--reinstall", "--no-env-file", "--no-progress", "--native-tls"}
        ),
    ),
    "poetry": Wrapper(
        frozenset({"-P", "--project"}),
        frozenset({"-C", "--directory"}),
        ("run",),
        frozenset(
            {"-q", "--quiet", "-v", "--verbose", "-n", "--no-interaction"}
            | {"--no-ansi"}
        ),
    ),
    "pipenv": Wrapper(first=("run",)),
    "pdm": Wrapper(
        frozenset({"-p", "--project"}),
        first=("run",),
        flags=frozenset({"-v", "--verbose", "-q", "--quiet"}),
    ),
    "hatch": Wrapper(
        frozenset({"-e", "--env"}),
        first=("run",),
        flags=frozenset({"-v", "--verbose", "-q", "--quiet"}),
    ),
    "bundle": Wrapper(frozenset({"--gemfile"}), first=("exec",)),
}
# Programs that run a `make` where trace-ci cannot follow it: a shell on a
# script, a container, another host, or a development shell.
ELSEWHERE = SHELLS | {
    "docker",
    "podman",
    "ssh",
    "kubectl",
    "vagrant",
    "nix",
    "nix-shell",
    "devbox",
}
ELSEWHERE_TEXT = (
    "in a shell script, a command string, a container, another host or a "
    "development shell, in a directory that a wrapper option moves to, or "
    "after a wrapper option that trace-ci does not know"
)


class Move(NamedTuple):
    """A `cd`, `pushd` or `popd`. SCOPE is the subshell that it moves, or None
    for the step's own shell. CONDITIONAL is the node below SCOPE that runs it
    only on some paths, or None, and BRANCH is the id of its branch there."""

    scope: object
    conditional: object
    branch: int | None
    name: str
    words: list[str]


def _move_of(node, name: str, words: list[str]) -> Move:
    background = node.next_sibling is not None and node.next_sibling.type == "&"
    if background or node.parent.type == "pipeline":
        return Move(node, None, None, name, words)
    conditional = branch = None
    parent = node.parent
    while parent is not None and parent.type not in SUBSHELL_SCOPES:
        if conditional is None and parent.type in GUARDS:
            branch = parent.id
            conditional = parent.parent if parent.type in BRANCH_CLAUSES else parent
        parent = parent.parent
    return Move(parent, conditional, branch, name, words)


def _branch(conditional, node) -> int:
    """The id of the branch of CONDITIONAL that NODE is in. The `then` branch of
    an `if`, and the body of a loop or a function, is CONDITIONAL itself."""
    for child in conditional.named_children:
        if child.type in BRANCH_CLAUSES and _contains(child, node):
            return child.id
    return conditional.id


def _contains(scope, node) -> bool:
    return scope is None or (
        scope.start_byte <= node.start_byte and node.end_byte <= scope.end_byte
    )


def _apply(stack: list[Path | None], move: Move, node) -> None:
    """Apply MOVE to STACK, whose last entry is the directory of NODE. A target
    that this tool cannot follow, or a move that runs only on some paths to NODE,
    makes the directory None. A move in another branch than NODE's does not
    run on NODE's path."""
    if move.conditional is not None:
        if not _contains(move.conditional, node):
            stack[-1] = None
            return
        if _branch(move.conditional, node) != move.branch:
            return
    if move.name == "popd":
        if len(stack) > 1:
            stack.pop()
        else:
            stack[-1] = None
        return
    args = [unquote(w) for w in move.words[1:]]
    plain = (
        len(args) == 1
        and not args[0].startswith(("~", "-"))
        and "$" not in move.words[1]
    )
    here = stack[-1]
    target = (here / args[0]).resolve() if plain and here is not None else None
    if move.name == "pushd":
        stack.append(target)
    else:
        stack[-1] = target


def _fold_directory(cwd: Path, directories: Sequence[str]) -> Path | None:
    """CWD moved by each `-C DIR` of a make call, in order. The trace then
    starts in the directory whose makefile make reads. None when a DIR is not
    plain."""
    here: Path | None = cwd
    for target in directories:
        if here is not None:
            here = None if not target or "$" in target else (here / target).resolve()
    return here


def _prefix_env(node) -> tuple[tuple[str, str], ...]:
    """The `VAR=value` words written before the command NODE's name."""
    env: list[tuple[str, str]] = []
    for child in node.named_children:
        if child.type != "variable_assignment":
            continue
        value = child.child_by_field_name("value")
        env.append(
            (
                node_text(child.child_by_field_name("name")),
                "" if value is None else unquote(node_text(value)),
            )
        )
    return tuple(env)


UNTRACED = "untraced"


def _launch(words: list[str]) -> tuple[list[str], list[str]] | str | None:
    """How the command WORDS runs make. The result is one of these:
    - the words from `make` on, and the `VAR=value` words of the wrappers
      before it, for a make that runs by itself or behind WRAPPERS programs;
    - UNTRACED, for a make that runs where trace-ci cannot follow it (see
      ELSEWHERE_TEXT);
    - None, when WORDS runs no make.

    An option that the WRAPPERS table does not list is read as a flag. That
    option can take the next word as its value, so when the words after that
    word launch a make, the make is UNTRACED and not lost. A `--` ends the
    options."""
    name = program_name(words[0])
    if name in MAKE_PROGRAMS:
        return words, []
    if name in ELSEWHERE:
        return UNTRACED if _mentions_make(words[1:]) else None
    if name not in WRAPPERS:
        return None
    spec = WRAPPERS[name]
    first = list(spec.first)
    env: list[str] = []
    moved = unlisted = ended = False
    i = 1
    while i < len(words):
        text = unquote(words[i])
        options = [] if ended else _wrapper_options(text)
        # Whether the word before is an unlisted option that can take this word.
        after_unlisted, unlisted = unlisted, False
        if text == "--" and not ended:
            ended = True
        elif options:
            for option, value in options:
                if option in spec.command:
                    rest = words[i + 1 :] if value is None else [value, *words[i + 1 :]]
                    return UNTRACED if _mentions_make(rest) else None
                moved |= option in spec.chdir
                if option in spec.values | spec.chdir - spec.flags:
                    i += 0 if value is not None else 1
                    break
                unlisted = value is None and option not in spec.flags
        elif re.fullmatch(r"\w+=.*", text):
            env.append(words[i])
        elif first:
            expected = first.pop(0)
            if expected is not None and text != expected:
                return None
        else:
            found = _launch(words[i:])
            if found is None and after_unlisted and words[i + 1 :]:
                found = None if _launch(words[i + 1 :]) is None else UNTRACED
            if found is None:
                return None
            if moved or found == UNTRACED:
                return UNTRACED
            return found[0], env + found[1]
        i += 1
    return None


def _wrapper_options(text: str) -> list[tuple[str, str | None]]:
    """The options in the word TEXT, each with the value that TEXT holds for
    it, or None. `-iu` holds `-i` and `-u`, and `--user=ci` holds `--user` with
    the value `ci`. A short option that takes a value takes the rest of its
    group, so the value of each letter is the letters after it, and the last
    letter has None. A `-` alone is an option too (`env -` empties the
    environment)."""
    if text == "-":
        return [("-", None)]
    if text.startswith("--") and len(text) > 2:
        name, eq, value = text.partition("=")
        return [(name, value if eq else None)]
    if text.startswith("-") and len(text) > 1:
        letters = text[1:]
        return [(f"-{c}", letters[n + 1 :] or None) for n, c in enumerate(letters)]
    return []


def _mentions_make(words: list[str]) -> bool:
    """Whether a word of WORDS is `make`, or a shell script that runs make."""
    return any(
        _launch(command) is not None
        for word in words
        for command in map(command_words, iter_nodes(parse(unquote(word)), "command"))
        if command
    )


def make_calls(step: CiStep, repo: Path) -> tuple[list[MakeCall], list[int]]:
    """The `make` calls in STEP, and the lines of the calls that cannot be traced.

    A `cd` or `pushd` with one plain argument moves the directory for the
    commands after it in the same shell, and `popd` moves it back. A move that
    runs only on some paths (in a branch, a loop, a function or the background)
    makes the directory unknown after it. A `make` is returned as a line that
    cannot be traced when its directory is unknown or outside the checkout,
    when it has an expansion or a CI template expression, or when `_launch`
    finds that trace-ci cannot follow it."""
    start = (
        None
        if step.working_directory is None
        else (repo / step.working_directory).resolve()
    )
    moves: list[Move] = []
    calls: list[MakeCall] = []
    untraced: list[int] = []
    for node in iter_nodes(parse(ci_shell_text(step.command)), "command"):
        words = command_words(node)
        if not words:
            continue
        name = program_name(words[0])
        line = step.lines[min(node.start_point[0], len(step.lines) - 1)]
        if name in DIR_COMMANDS:
            moves.append(_move_of(node, name, words))
            continue
        found = _launch(words)
        if found is None:
            continue
        if found == UNTRACED:
            untraced.append(line)
            continue
        make_words, env_words = found
        stack: list[Path | None] = [start]
        for move in moves:
            if _contains(move.scope, node):
                _apply(stack, move, node)
        args = parse_make_args([unquote(w) for w in make_words[1:]])
        cwd = (
            None if stack[-1] is None else _fold_directory(stack[-1], args.directories)
        )
        if (
            cwd is None
            or not cwd.is_relative_to(repo.resolve())
            or any(True for _ in iter_nodes(node, *EXPANSIONS))
            or CI_PLACEHOLDER in node_text(node)
        ):
            untraced.append(line)
            continue
        env = step.env + _prefix_env(node)
        env += tuple(tuple(unquote(w).split("=", 1)) for w in env_words)
        calls.append(MakeCall(step, cwd, args.args, line, env))
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


def _make_env(
    path_prefix: Path | None = None, ci_env: Sequence[tuple[str, str]] = ()
) -> dict[str, str]:
    """The environment for a dry run: the CI variables CI_ENV, no make flags from
    a parent make, and C messages, which the trace parser reads."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEFILES", "GNUMAKEFLAGS")
    }
    env.update(ci_env)
    env["LC_ALL"] = "C"
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env.get('PATH', '')}"
    return env


# `--eval` reaches every sub-make through MAKEFLAGS, so each make prints this
# line with its level as it starts, whether or not it prints its directory.
LEVEL_MARK = "__cts_make_level__="
# Why `parse_trace` cannot tell which make printed a command.
HIDDEN_DIRECTORY = "a sub-make with `--no-print-directory`, so trace-ci cannot tell which makefile each command comes from; remove `--no-print-directory` to trace it"
HIDDEN_SYNC = "a sub-make that syncs its output (`-O`) over more than one job, and a `$(MAKE)` or `+` line comes out between its jobs, so trace-ci cannot tell which make each command comes from; `-O` in a `MAKEFLAGS` reaches every sub-make, so remove it there to trace this call"


def make_trace(
    make: str,
    cwd: Path,
    args: Sequence[str],
    path_prefix: Path | None = None,
    ci_env: Sequence[tuple[str, str]] = (),
) -> Trace:
    parsed = parse_make_args(args)
    proc = subprocess.run(
        [
            make,
            "--dry-run",
            "--trace",
            # `-s` hides the directory lines of a sub-make, and `-w` shows them.
            "--print-directory",
            "-C",
            str(cwd),
            "--eval",
            f"$(info {LEVEL_MARK}$(MAKELEVEL))",
            # Parallel sub-makes mix their directory lines. `.NOTPARALLEL`
            # reaches each sub-make, as the line above does, and it runs
            # each make one job at a time, even a `$(MAKE) -j4`.
            "--eval",
            ".NOTPARALLEL:",
            *parsed.args,
            # With one job, make does not sync output (`-O`) in job chunks.
            # The last `-j` wins, so this one goes after the call's own.
            "--jobs=1",
        ],
        capture_output=True,
        text=True,
        env=_make_env(path_prefix, ci_env),
        check=False,
    )
    commands, hidden, follows = parse_trace(
        proc.stdout, cwd, parsed.options, parsed.goals
    )
    return Trace(proc.returncode, tuple(commands), proc.stderr, hidden, follows)


def parse_trace(
    output: str,
    cwd: Path,
    options: tuple[str, ...] = (),
    goals: tuple[str, ...] = (),
) -> "ParsedTrace":
    """The commands in the stdout of `make --dry-run --trace`, HIDDEN, and
    FOLLOWS: for each rule run, the makefile and first recipe line of the rule
    that the same make ran next. HIDDEN says why trace-ci cannot tell which
    make printed a command, or is "".

    GNU make names the FIRST RECIPE LINE of each rule that it runs, not the rule
    header. A command that ends in `\\` goes on to the next line. A sub-make
    prints its rules between `Entering directory` and `Leaving directory`, and
    the parent's rule goes on after it, so each sub-make keeps its own frame.
    The parent's last command is the one that started the sub-make, so it gives
    the sub-make's arguments. Each make prints its level mark once, as it
    starts, so the mark tells a new sub-make from the same make again.

    Output sync (`-O`) puts the mark of a make that syncs from its start in a
    pair of directory lines of its own. With one job, the rest of its output
    is in one more pair. With more jobs, the output of each job is in its own
    pair, and a `$(MAKE)` or `+` line is outside every pair, so a line between
    two of its pairs can be its own or its parent's, and that hides it. A
    sub-make with `--no-print-directory` prints its level mark but no
    `Entering directory` line, and that hides it too."""
    frames = [_Frame(cwd, 0, options, goals)]
    found: list[Traced] = []
    follows: list[Follow] = []
    runs = 0
    hidden = ""
    pending: list[str] = []
    # The frame that each make level and directory left last, the makes whose
    # mark is in a pair of its own, the make whose mark is the line before,
    # and such a sub-make whose pair ended on the line before.
    left: dict[tuple[int, Path], _Frame] = {}
    synced: list[_Frame] = []
    marked: _Frame | None = None
    after_pair: _Frame | None = None

    def emit(text: str) -> None:
        top = frames[-1]
        found.append(
            Traced(
                *top.where,
                top.index,
                text,
                top.cwd,
                top.run,
                top.level,
                top.options,
                top.goals,
            )
        )
        top.index += 1
        top.last, top.started = text, 0

    for raw in output.splitlines():
        was_marked, marked = marked, None
        was_after_pair, after_pair = after_pair, None
        if pending:
            pending.append(raw)
            if not raw.endswith("\\"):
                emit("\n".join(pending))
                pending = []
            continue
        if m := ENTER.match(raw):
            parent, level = frames[-1], int(m.group("level") or 0)
            directory = Path(m.group("dir"))
            if level == parent.level and directory == parent.cwd:
                frames.append(parent)
            else:
                # The make that left this directory last, entering again; a
                # level mark on the next line names a new make instead.
                frame = left.get(
                    (level, directory),
                    _Frame(directory, level, parent.options, parent.goals),
                )
                frame.pairs += 1
                frames.append(frame)
            continue
        if raw.startswith(LEVEL_MARK):
            top, level = frames[-1], int(raw[len(LEVEL_MARK) :])
            parent = frames[-2] if len(frames) > 1 else top
            if level == top.level and parent.level < level:
                # A new make, so the frame it entered as was not entered.
                top.pairs -= 1
                frames[-1] = _Frame(
                    top.cwd,
                    level,
                    *_sub_make_args(parent.last, parent.started, parent.options),
                )
                parent.started += 1
                marked = frames[-1]
            if level != top.level:
                hidden = hidden or HIDDEN_DIRECTORY
            continue
        if LEAVE.match(raw):
            if len(frames) > 1:
                top = frames.pop()
                left[top.level, top.cwd] = top
                if top is was_marked:
                    synced.append(top)
                if top in synced and top.level > 0:
                    after_pair = top
            continue
        if MAKE_MESSAGE.match(raw):
            continue
        if m := TRACE_RULE.match(raw):
            top = frames[-1]
            path = Path(m.group("file"))
            # The commands of a built-in rule are in no file of the repository.
            where = (
                None
                if m.group("line") is None
                else (
                    (path if path.is_absolute() else top.cwd / path).resolve(),
                    int(m.group("line")),
                )
            )
            if top.where is not None and where is not None:
                follows.append(Follow(top.run, *where))
            top.where = where
            top.index, top.run, runs = 0, runs, runs + 1
            continue
        if was_after_pair is not None:
            was_after_pair.bare_after = True
        if frames[-1].where is None:
            continue
        if raw.endswith("\\"):
            pending = [raw]
        else:
            emit(raw)
    if pending:
        # The output ended inside a continued command. Keep what it printed.
        emit("\n".join(pending))
    if any(f.pairs > 1 and f.bare_after for f in synced):
        hidden = hidden or HIDDEN_SYNC
    return ParsedTrace(found, hidden, tuple(follows))


class ParsedTrace(NamedTuple):
    """What `parse_trace` reads from the output of a dry run."""

    commands: list[Traced]
    hidden: str
    follows: tuple[Follow, ...]


@dataclass(eq=False)
class _Frame:
    """The state of one make level while `parse_trace` reads its output."""

    cwd: Path
    level: int
    options: tuple[str, ...]
    goals: tuple[str, ...]
    where: tuple[Path, int] | None = None
    index: int = 0
    run: int = 0
    # The last command, and how many sub-makes it started so far.
    last: str = ""
    started: int = 0
    # How many pairs of directory lines it entered, and, for a make that
    # syncs its output, whether a line followed one of its pairs.
    pairs: int = 0
    bare_after: bool = False


def _sub_make_args(
    text: str, n: int, inherited: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The options and goals of the Nth sub-make that the command TEXT starts,
    from 0. A loop starts one make word more than once, so N past the last
    make word reads the last. Make passes its command-line variables down, so
    the sub-make also gets the `VAR=value` words of INHERITED. Its `-C` is left
    out: the frame has its directory."""
    variables = tuple(o for o in inherited if "=" in o and not o.startswith("-"))
    makes = [
        words
        for words in map(command_words, iter_nodes(parse(text), "command"))
        if words and program_name(words[0]) in MAKE_PROGRAMS
    ]
    if not makes:
        return variables, ()
    words = makes[min(n, len(makes) - 1)]
    args = parse_make_args([unquote(w) for w in words[1:]])
    return variables + args.options, args.goals


# A conditional directive can sit between the lines of one recipe.
CONDITIONAL = re.compile(r" *(?:ifdef|ifndef|ifeq|ifneq|else|endif)\b")


def recipe_lines(
    text: str, first_line: int, recipe_prefix: str = "\t"
) -> list[RecipeLine]:
    """The logical recipe lines of the rule whose first recipe line is FIRST_LINE.
    A recipe line starts with RECIPE_PREFIX. Each branch of a conditional
    counts, so a branch that make skips makes the count larger than what make
    prints."""
    lines = text.splitlines()
    out: list[RecipeLine] = []
    i = first_line - 1
    while i < len(lines):
        line = lines[i]
        if line.startswith(recipe_prefix):
            start = i
            while lines[i].endswith("\\") and i + 1 < len(lines):
                i += 1
            prefix = (
                re.match(r"[@+\-\s]*", line[len(recipe_prefix) :])
                .group(0)
                .replace(" ", "")
                .replace("\t", "")
            )
            source = "\n".join(lines[start : i + 1])
            # Make runs nothing for an empty line, and prints nothing for it.
            if not re.sub(r"[@+\-\s]", "", source[len(recipe_prefix) :]):
                i += 1
                continue
            runs = "+" in prefix or "$(MAKE)" in source or "${MAKE}" in source
            out.append(RecipeLine(start + 1, i - start + 1, prefix, runs))
        elif line.strip() and not line.startswith("#") and not CONDITIONAL.match(line):
            break
        i += 1
    return out


@dataclass(frozen=True)
class MakeShell:
    """The shell that a make runs its recipe lines with, and the character
    that starts a recipe line (`.RECIPEPREFIX`, a tab by default)."""

    shell: str
    flags: str
    oneshell: bool
    prefix: str = "\t"


PROBE_GOAL = ".cts-trace-probe"
PROBE_SHELL, PROBE_FLAGS, PROBE_PREFIX = (
    "__cts_shell__=",
    "__cts_shellflags__=",
    "__cts_recipeprefix__=",
)
# The options of GNU make 4.3, from the `switches` table in its src/main.c.
# Each name maps to its long name and how it takes a value:
# - "value": the rest of the word, or else the next word;
# - "optional": only the rest of the word (`-Oline`, `--debug=b`);
# - "number": the rest of the word, or else the next word when it is a number;
# - "" (None for a short option): no value.
MAKE_SHORT_OPTIONS = {
    **dict.fromkeys("bBdehikLmnpqrRsStvw"),
    "C": ("directory", "value"),
    "E": ("eval", "value"),
    "f": ("file", "value"),
    "I": ("include-dir", "value"),
    "j": ("jobs", "number"),
    "l": ("load-average", "number"),
    "o": ("old-file", "value"),
    "O": ("output-sync", "optional"),
    "W": ("what-if", "value"),
}
MAKE_LONG_OPTIONS = {
    "always-make": "",
    "assume-new": "value",
    "assume-old": "value",
    "check-symlink-times": "",
    "debug": "optional",
    "directory": "value",
    "dry-run": "",
    "environment-overrides": "",
    "eval": "value",
    "file": "value",
    "help": "",
    "ignore-errors": "",
    "include-dir": "value",
    "jobs": "number",
    "jobserver-auth": "value",
    "jobserver-fds": "value",
    "just-print": "",
    "keep-going": "",
    "load-average": "number",
    "makefile": "value",
    "max-load": "number",
    "new-file": "value",
    "no-builtin-rules": "",
    "no-builtin-variables": "",
    "no-keep-going": "",
    "no-print-directory": "",
    "no-silent": "",
    "old-file": "value",
    "output-sync": "optional",
    "print-data-base": "",
    "print-directory": "",
    "question": "",
    "quiet": "",
    "recon": "",
    "silent": "",
    "stop": "",
    "touch": "",
    "trace": "",
    "version": "",
    "warn-undefined-variables": "",
    "what-if": "value",
}
MAKE_FILE_OPTIONS = frozenset({"file", "makefile"})


class MakeArgs(NamedTuple):
    """The arguments of one make call. DIRECTORIES are its `-C` values, in
    order, and FILES its `-f` values. OPTIONS are its other options, one word
    each, and its `VAR=value` words. GOALS are the rest."""

    directories: tuple[str, ...]
    files: tuple[str, ...]
    options: tuple[str, ...]
    goals: tuple[str, ...]

    @property
    def args(self) -> tuple[str, ...]:
        """Every argument but the `-C` values, as words that make reads back."""
        dashed = any(g.startswith("-") for g in self.goals)
        return (*self.options, *(("--",) if dashed else ()), *self.goals)


def parse_make_args(args: Sequence[str]) -> MakeArgs:
    """ARGS read as GNU make reads them: grouped short options (`-sC dir`),
    a value in the same word or the next, a unique prefix of a long option, and
    options after the goals. An option that make does not know is kept as an
    option word, so make itself rejects it when trace-ci runs it."""
    directories: list[str] = []
    files: list[str] = []
    options: list[str] = []
    goals: list[str] = []
    words = list(args)
    i = 0

    def take(name: str, how: str, attached: str | None) -> None:
        nonlocal i
        value = attached
        if value is None and i < len(words):
            following = words[i]
            if how == "value" or (
                how == "number" and re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", following)
            ):
                value, i = following, i + 1
        if name == "directory":
            directories.append(value or "")
        elif name in MAKE_FILE_OPTIONS:
            files.append(value or "")
            options.append(f"--file={value or ''}")
        else:
            options.append(f"--{name}" if value is None else f"--{name}={value}")

    while i < len(words):
        word = words[i]
        i += 1
        if word == "--":
            for rest in words[i:]:
                (options if "=" in rest else goals).append(rest)
            break
        if word.startswith("--"):
            name, eq, attached = word[2:].partition("=")
            known = (
                [name]
                if name in MAKE_LONG_OPTIONS
                else [n for n in MAKE_LONG_OPTIONS if n.startswith(name)]
            )
            if len(known) != 1:
                options.append(word)
                continue
            if MAKE_LONG_OPTIONS[known[0]]:
                take(known[0], MAKE_LONG_OPTIONS[known[0]], attached if eq else None)
            else:
                options.append(f"--{known[0]}")
            continue
        if word.startswith("-") and len(word) > 1:
            for n, letter in enumerate(word[1:], 2):
                spec = MAKE_SHORT_OPTIONS.get(letter)
                if not spec:
                    options.append(f"-{letter}")
                    continue
                name, how = spec
                rest = word[n:]
                take(name, how, rest or None)
                break
            continue
        (options if "=" in word else goals).append(word)
    return MakeArgs(tuple(directories), tuple(files), tuple(options), tuple(goals))


def make_shell(
    make: str,
    cwd: Path,
    options: Sequence[str] = (),
    goals: Sequence[str] = (),
    ci_env: Sequence[tuple[str, str]] = (),
) -> MakeShell:
    """SHELL, .SHELLFLAGS and .ONESHELL as make in CWD, with OPTIONS and the
    environment CI_ENV, sets them for GOALS.

    The rule that `--eval` adds prints the values when make expands its recipe,
    which is after every makefile is read. So the values are the ones that a
    recipe really gets, with each `=` reference expanded. MAKECMDGOALS is set to
    GOALS, because a makefile can test it, and the probe is not the real goal.
    `.ONESHELL` is a special target, and the database that `-p` prints names it."""
    proc = subprocess.run(
        [
            make,
            "-p",
            "--dry-run",
            "-C",
            str(cwd),
            *options,
            f"MAKECMDGOALS={' '.join(goals)}",
            "--eval",
            f"{PROBE_GOAL}: ; @:$(info {PROBE_SHELL}$(SHELL))"
            f"$(info {PROBE_FLAGS}$(.SHELLFLAGS))"
            f"$(info {PROBE_PREFIX}$(.RECIPEPREFIX))",
            PROBE_GOAL,
        ],
        capture_output=True,
        text=True,
        env=_make_env(ci_env=ci_env),
        check=False,
    )
    values: dict[str, str] = {}
    oneshell = False
    for line in proc.stdout.splitlines():
        for name in (PROBE_SHELL, PROBE_FLAGS, PROBE_PREFIX):
            if line.startswith(name):
                values[name] = line[len(name) :]
        oneshell = oneshell or line == ".ONESHELL:"
    if proc.returncode != 0 or PROBE_SHELL not in values:
        raise TraceError(
            f"make in {cwd} did not print its SHELL value:\n{proc.stderr.strip()}"
        )
    return MakeShell(
        values[PROBE_SHELL],
        values.get(PROBE_FLAGS) or "-c",
        oneshell,
        values.get(PROBE_PREFIX) or "\t",
    )


class ShellOptions(NamedTuple):
    """How a shell runs: its interpreter, the options for `set`, and whether a
    failed command stops it (errexit) or fails a pipeline (pipefail)."""

    interpreter: str
    options: list[str]
    errexit: bool
    pipefail: bool


def _shell_words(shell: str) -> list[str]:
    """The words of SHELL from its interpreter on. `env` and its options and
    assignments are skipped, so `/usr/bin/env bash` reads as bash."""
    words = shlex.split(shell)
    while len(words) > 1 and program_name(words[0]) == "env":
        words = words[1:]
        while len(words) > 1 and (words[0].startswith("-") or "=" in words[0]):
            words = words[1:]
    return words


def posix_shell(shell: str) -> bool:
    """Whether SHELL is a POSIX shell, which the bash grammar can read.
    PowerShell and `cmd.exe` are not."""
    return program_name(_shell_words(shell)[0]).removesuffix(".exe") in SHELLS


def shell_options(shell: str, flags: str) -> ShellOptions:
    """The options of the POSIX SHELL when it runs with FLAGS."""
    words = _shell_words(shell) + shlex.split(flags)
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
    pipefail = "pipefail" in options and has_pipefail(interpreter)
    return ShellOptions(interpreter, options, errexit, pipefail)


def has_pipefail(interpreter: str) -> bool:
    """Whether INTERPRETER honours `pipefail`. dash, the usual `/bin/sh`, does not."""
    return program_name(interpreter) in ("bash", "zsh", "ksh")


def _cannot_fail(node) -> bool:
    """A statement that sets the shell state, or that runs a command in NO_FAILURE.

    `X=$(cmd)` takes the status of `cmd`, so it can fail. `export X=$(cmd)` takes
    the status of `export`, which is the separate defect that SC2155 names."""
    if node.type == "variable_assignment":
        return not any(True for _ in iter_nodes(node, "command_substitution"))
    if node.type == "declaration_command":
        return True
    if node.type != "command":
        return False
    words = command_words(node)
    if not words:
        # Only assignments and redirections: `X=1` as a whole statement.
        return True
    return program_name(words[0]) in NO_FAILURE | {"set"}


def _can_lose(node) -> bool:
    """Whether NODE, as a statement, can end with a failed status that a later
    statement hides. A compound statement ends with the status of the last
    statement of the branch or the pass that ran. A `||` list handles the
    failure of its left side: `a || rc=1`, `a || exit 1`. A failed condition
    only picks a branch, so it is not a lost status."""
    kind = node.type
    if kind == "list":
        return node.children[-2].type != "||" and any(
            _can_lose(c) for c in node.named_children
        )
    if kind == "redirected_statement":
        return _can_lose(node.named_children[0])
    if kind == "pipeline":
        return _can_lose(node.named_children[-1])
    if kind in ("if_statement", "case_statement", "compound_statement", "subshell"):
        branches = [node, *node.named_children] if kind == "if_statement" else [node]
        if kind == "case_statement":
            branches = [c for c in node.named_children if c.type == "case_item"]
        return any(
            _can_lose(body[-1])
            for b in branches
            if b.type in BODIES and (body := _body(b))
        )
    if kind in LOOPS:
        body = next((c for c in node.named_children if c.type == "do_group"), None)
        statements = _body(body) if body is not None else []
        return bool(statements) and _can_lose(statements[-1])
    if kind == "command":
        words = command_words(node)
        if words and program_name(words[0]) in ("exit", "return"):
            return False
    if kind in ("negated_command", "function_definition"):
        return False
    return not _cannot_fail(node)


# The nodes whose statements run one after another. A loop's body is its
# `do_group`. A function's body is a `compound_statement`.
BODIES = frozenset(
    {
        "program",
        "do_group",
        "compound_statement",
        "subshell",
        "if_statement",
        "elif_clause",
        "else_clause",
        "case_item",
    }
)
LOOPS = frozenset({"for_statement", "c_style_for_statement", "while_statement"})


def _body(node) -> list:
    """The statements of NODE, one of BODIES, in order. The condition of an
    `if` or an `elif`, its other clauses, and the patterns of a `case` item
    are left out."""
    children = node.children
    if node.type == "case_item":
        close = next((i for i, c in enumerate(children) if c.type == ")"), -1)
        children = children[close + 1 :]
    skip = {c.id for c in condition_parts(node)}
    return [
        c
        for c in children
        if c.is_named
        and c.id not in skip
        and c.type not in ("comment", "elif_clause", "else_clause")
    ]


def _set_changes(statement) -> dict[str, bool]:
    """The `errexit` and `pipefail` changes that a top-level `set` STATEMENT
    makes: `-e` and `-o NAME` turn an option on, `+e` and `+o NAME` turn it off."""
    if statement.type != "command":
        return {}
    words = [unquote(w) for w in command_words(statement)]
    if not words or words[0] != "set":
        return {}
    changes: dict[str, bool] = {}
    for word, after in zip(words[1:], [*words[2:], ""]):
        if re.fullmatch(r"[-+][A-Za-z]*e[A-Za-z]*", word):
            changes["errexit"] = word[0] == "-"
        if re.fullmatch(r"[-+][A-Za-z]*o", word) and after in ("errexit", "pipefail"):
            changes[after] = word[0] == "-"
    return changes


# A `$?` read inside one of these nodes acts on the status of the statement before.
STATUS_READERS = frozenset(
    {"test_command", "binary_expression", "case_statement", "variable_assignment"}
)


def _reads_status(statement) -> bool:
    """Whether STATEMENT acts on `$?`: tests it, compares it, branches on it,
    saves it, or exits with it. `echo "done $?"` only prints it, and so does a
    `$?` in the body of an `if`, so neither counts."""
    for expansion in iter_nodes(statement, "simple_expansion"):
        if node_text(expansion) != "$?":
            continue
        child, node = expansion, expansion.parent
        while node is not None:
            if node.type in STATUS_READERS:
                return True
            if node.type == "command":
                words = command_words(node)
                if words and program_name(words[0]) in ("test", "[", "exit", "return"):
                    return True
            if any(part.id == child.id for part in condition_parts(node)):
                return True
            if node.id == statement.id:
                break
            child, node = node, node.parent
    return False


class LostStatus(NamedTuple):
    """A command whose failure the shell does not report: its row in the
    command, whether a `;`, a `|` or a loop loses it, and its text."""

    row: int
    kind: str
    text: str


def dropped_statuses(
    command: str, errexit: bool, pipefail: bool, interpreter: str = "bash"
) -> list[LostStatus]:
    """Each command whose failure COMMAND does not report.

    Without errexit, `a; b` reports only `b`, in any body: the top level, a
    loop, a branch, a group or a function. A loop reports only its last pass,
    so the last statement of its body is reported too. Without pipefail,
    `a | b` reports only `b`. A statement whose status the next one acts on
    through `$?`, or whose failure a `||` handles, is not reported. A top-level
    `set` changes an option for the code after it: `-e` and `-o NAME` turn it
    on, and `+e` and `+o NAME` turn it off. INTERPRETER says whether the
    shell honours `pipefail`."""
    root = parse(command)
    top = _body(root)
    # The state of each option as each top-level statement starts.
    state = {"errexit": errexit, "pipefail": pipefail}
    states: list[dict[str, bool]] = []
    for statement in top:
        states.append(dict(state))
        for option, on in _set_changes(statement).items():
            if option == "errexit" or has_pipefail(interpreter):
                state[option] = on

    def option(node, name: str) -> bool:
        """NAME's state for NODE: the state of the top-level statement it is in."""
        return next(
            states[i][name]
            for i, s in enumerate(top)
            if s.start_byte <= node.start_byte < s.end_byte
        )

    found: list[LostStatus] = []
    for body in [root, *iter_nodes(root, *(BODIES - {"program"}))]:
        statements = _body(body)
        for statement, after in zip(statements, statements[1:]):
            if option(statement, "errexit") or _reads_status(after):
                continue
            if _can_lose(statement):
                found.append(
                    LostStatus(
                        statement.start_point[0], "sequence", node_text(statement)
                    )
                )
    for loop in iter_nodes(root, *LOOPS):
        body = next((c for c in loop.named_children if c.type == "do_group"), None)
        statements = _body(body) if body is not None else []
        if statements and not option(loop, "errexit") and _can_lose(statements[-1]):
            last = statements[-1]
            found.append(LostStatus(last.start_point[0], "loop", node_text(last)))
    for pipeline in iter_nodes(root, "pipeline"):
        if option(pipeline, "pipefail"):
            continue
        stages = [c for c in pipeline.named_children if c.type != "comment"]
        for stage in stages[:-1]:
            if not _cannot_fail(stage):
                found.append(LostStatus(stage.start_point[0], "pipe", node_text(stage)))
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
    """(program, literal arguments) of the first command in a `$(shell …)` BODY
    that a stub on PATH can replace, so `cd $(DIR) && git ls-files` targets git.

    The stub fails a call only when all these literal arguments are in its argv,
    so `git rev-parse --short HEAD` fails and `git rev-parse --show-toplevel`
    does not. None when the program name is itself an expansion."""
    for node in iter_nodes(parse(_strip_make_refs(body)), "command"):
        words = command_words(node)
        if not words:
            continue
        if MAKE_REF_PLACEHOLDER in words[0] or "$" in words[0]:
            return None
        if program_name(words[0]) in SHELL_BUILTINS:
            continue
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
    # run_selection exits 1 both for a finding and for a lint that crashed, so a
    # finding in one lint would hide the crash of another.
    if "Traceback (most recent call last):" in output:
        raise TraceError(
            f"a content lint crashed on the commands that CI runs:\n{output}"
        )
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
    """Whether a comment on the finding's line, or on the line above it,
    accepts the finding."""
    path = repo / finding.path
    if not path.is_file():
        return False
    comments = _comments(
        finding.path, path.read_text(encoding="utf-8", errors="replace")
    )
    return any(
        ANNOTATION.search(comments.get(n, "")) for n in (finding.line - 1, finding.line)
    )


def _comments(path: str, text: str) -> dict[int, str]:
    """1-based line -> the comment text on that line of the file at PATH. A
    marker in a string is data that a command handles, so only a comment can
    accept a finding.

    In a YAML file a comment is a YAML comment or a comment in a `run:` script.
    A CI file that trace-ci reports on parsed when it was read, so
    `yaml_comments` never takes its text fallback here.
    In a makefile a recipe line goes to the shell, so the bash grammar finds
    its comment. Make has no parser here, but its rule for any other line is
    plain: an unescaped `#` starts a comment, even inside a `$(…)`."""
    if path.endswith((".yml", ".yaml")):
        return yaml_comments(text)
    if "makefile" not in identify.tags_from_filename(path):
        return comment_lines(text, path)
    comments: dict[int, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if line.startswith("\t"):
            comments[n] = shell_comments(line[1:]).get(1, "")
        elif m := re.search(r"(?<!\\)#.*", line):
            comments[n] = m.group(0)
    return comments


def _slug(text: str) -> str:
    """TEXT made safe for one part of a file name."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text)


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
    untracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--others", "--directory"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    if untracked:
        notes.append(
            f"{len(untracked)} paths are not tracked by git "
            f"({', '.join(untracked[:3])}). make skips a rule whose target file "
            "is up to date, and CI starts from a clean checkout, so a rule that "
            "CI runs can be missing from this trace; trace a clean checkout to "
            "see every rule"
        )
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
    for system, read in READERS.items():
        for source in surfaces.get(system, []):
            found_steps, found_images = read(
                (repo / source).read_text(encoding="utf-8"), source, repo, notes
            )
            steps += found_steps
            images += found_images

    if images:
        synthetics.append(
            Synthetic(
                "ci-images/Dockerfile",
                tuple(f"FROM {i.image}" for i in images),
                tuple(SourceLine(i.source, i.line, False) for i in images),
            )
        )

    calls: list[MakeCall] = []
    other_shells = sorted(
        {s.shell for s in steps if s.shell and not posix_shell(s.shell)}
    )
    if other_shells:
        notes.append(
            "the content lints read only POSIX shell, so they do not read the "
            f"steps that run in {', '.join(other_shells)}; trace-ci still "
            "follows the make calls of those steps"
        )
    for n, step in enumerate(steps):
        if step.lint:
            findings += _step_findings(
                step, f"ci-steps/{n}-{_slug(step.job)}.sh", synthetics
            )
        found, untraced = make_calls(step, repo)
        calls += found
        for line in untraced:
            findings.append(
                TraceFinding(
                    step.source,
                    line,
                    "untraced",
                    f"this `make` call has an expansion or a CI template expression, runs in a directory outside the checkout, follows a `cd` that trace-ci cannot resolve, or runs {ELSEWHERE_TEXT}, so its commands are not checked",
                )
            )

    baselines: dict[MakeCall, Trace] = {}
    reached_dirs: set[Path] = set()
    for call in calls:
        result = make_trace(make, call.cwd, call.args, ci_env=call.env)
        reached_dirs.update(_makefile_dirs(call))
        if result.hidden:
            findings.append(
                TraceFinding(
                    call.step.source,
                    call.line,
                    "untraced",
                    f"`make {' '.join(call.args)}` runs {result.hidden}; its commands are not checked",
                )
            )
            continue
        baselines[call] = result
        reached_dirs.update(c.makefile.parent for c in result.commands)
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

    findings += _recipe_findings(repo, make, baselines, synthetics, notes)
    findings += run_lints(synthetics, checks)
    if inject:
        findings += _inject_faults(
            repo, make, surfaces.get("make", []), baselines, notes
        )
    kept = _kept(repo, findings)
    untraced = sum(f.kind == "untraced" for f in kept)
    return _kept(
        repo,
        kept
        + _coverage(repo, make, surfaces.get("make", []), reached_dirs, goal, untraced),
    )


def _kept(repo: Path, findings: list[TraceFinding]) -> list[TraceFinding]:
    """FINDINGS once each, in order, without the ones that a marker accepts."""
    unique = sorted(set(findings), key=lambda f: (f.path, f.line, f.kind, f.message))
    return [f for f in unique if not _annotated(repo, f)]


def _makefile_dirs(call: MakeCall) -> list[Path]:
    """The directories of the makefiles that CALL reads: each `-f FILE`, or
    the call's directory when it names none."""
    files = parse_make_args(call.args).files
    return [(call.cwd / f).resolve().parent for f in files if f] or [call.cwd]


def _step_findings(
    step: CiStep, path: str, synthetics: list[Synthetic]
) -> list[TraceFinding]:
    """Write STEP out for the content lints, and return the statuses it loses."""
    command = ci_shell_text(step.command)
    body = [
        BodyLine(t, step.source, step.lines[min(i, len(step.lines) - 1)])
        for i, t in enumerate(command.splitlines())
    ]
    synthetics.append(
        _shell_file(path, step.shell, "", body, (step.source, step.lines[0]))
    )
    options = shell_options(step.shell, "")
    return [
        _drop_finding(
            step.source,
            step.lines[min(row, len(step.lines) - 1)],
            kind,
            text,
            step.shell,
        )
        for row, kind, text in dropped_statuses(
            command, options.errexit, options.pipefail, options.interpreter
        )
    ]


def _recipe_findings(
    repo: Path,
    make: str,
    baselines: dict[MakeCall, Trace],
    synthetics: list[Synthetic],
    notes: list[str],
) -> list[TraceFinding]:
    """Write each traced recipe command out for the content lints, and return
    the statuses that the recipe shell loses and the failures that make ignores.

    A command maps to a recipe line by its place in the rule. When a rule run
    printed more or fewer commands than the recipe has lines (a line that
    expands to nothing, or a `define` that expands to several), the places do
    not match, so its commands are reported at the first recipe line. A dry run
    runs a `+` or `$(MAKE)` line, so its output mixes with the printed
    commands: a rule with one is not checked when the counts do not match.
    Make prints the `$(info …)` text of a recipe before it names the rule, so
    that text mixes with the commands of the rule before it, and that rule is
    not checked. trace-ci finds `$(info` only in the recipe text, so an `info`
    that a variable or a `$(call …)` holds is read as a command of the rule
    before it. Under `.ONESHELL` one shell runs the whole recipe, so the rule
    is checked as one script."""
    findings: list[TraceFinding] = []
    shells: dict[tuple, MakeShell] = {}
    rules: dict[tuple[Path, int], str] = {}
    seen: set[tuple] = set()
    for call, result in baselines.items():
        runs: dict[int, list[Traced]] = {}
        for command in result.commands:
            runs.setdefault(command.run, []).append(command)
        follows = {f.run: f for f in result.follows}
        for run, group in runs.items():
            first = group[0]
            key = (first.cwd, first.options, first.goals, call.env)
            if key not in shells:
                shells[key] = make_shell(make, *key)
            shell = shells[key]
            if (tuple(group), shell) in seen:
                continue
            seen.add((tuple(group), shell))
            path = os.path.relpath(first.makefile, repo)
            source = first.makefile.read_text(encoding="utf-8", errors="replace")
            recipe = recipe_lines(source, first.first_line, shell.prefix)
            rules[first.makefile, first.first_line] = shell.prefix
            if run in follows and _prints_info(
                follows[run].makefile, follows[run].line, shell.prefix
            ):
                after = follows[run]
                findings.append(
                    TraceFinding(
                        path,
                        first.first_line,
                        "untraced",
                        f"make expands the recipe at {os.path.relpath(after.makefile, repo)}:{after.line} before it names that rule, so the `$(info …)` text of that recipe comes out among this rule's commands; trace-ci cannot tell them apart and does not check this rule; use `@echo` in that recipe instead",
                    )
                )
                continue
            entries = recipe
            if len(group) != len(recipe):
                if any(e.runs for e in recipe):
                    findings.append(
                        TraceFinding(
                            path,
                            first.first_line,
                            "untraced",
                            "this rule has a `+` or `$(MAKE)` line, which a dry run runs, and the output of that line mixes with the commands that make prints, so trace-ci cannot tell them apart and does not check this rule",
                        )
                    )
                    continue
                notes.append(
                    f"{path}:{first.first_line}: make printed {len(group)} "
                    f"commands for a recipe of {len(recipe)} lines, so trace-ci "
                    "reports this rule's findings at its first recipe line"
                )
                entries = [RecipeLine(first.first_line, 1, "")] * len(group)
            findings += [
                TraceFinding(path, entry.start, "untraced", why)
                for command, entry in zip(group, entries)
                if not entry.runs and (why := _unfollowed_make(command.text))
            ]
            units = [
                [
                    BodyLine(t, path, entry.start + min(i, entry.count - 1))
                    for i, t in enumerate(command.text.splitlines())
                ]
                for command, entry in zip(group, entries)
            ]
            if shell.oneshell:
                units = [[line for unit in units for line in unit]]
            for unit in units:
                findings += _unit_findings(path, shell, unit, synthetics)
    for (makefile, first_line), recipe_prefix in sorted(rules.items()):
        text = makefile.read_text(encoding="utf-8", errors="replace")
        path = os.path.relpath(makefile, repo)
        for entry in recipe_lines(text, first_line, recipe_prefix):
            if "-" in entry.prefix:
                findings.append(
                    TraceFinding(
                        path,
                        entry.start,
                        "ignored-error",
                        "this recipe line starts with `-`, so make ignores its exit status and CI passes when it fails; remove the `-`, or handle the failure in the command",
                    )
                )
    return findings


def _prints_info(makefile: Path, first_line: int, recipe_prefix: str) -> bool:
    """Whether the recipe at FIRST_LINE of MAKEFILE calls make's `info`, which
    prints to the same output as the commands of a dry run. A `$$` is a `$` for
    the shell, so `$$(info x)` is no call."""
    text = makefile.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return any(
        re.search(
            r"(?<!\$)(?:\$\$)*\$[({]info\s",
            "\n".join(lines[entry.start - 1 : entry.start - 1 + entry.count]),
        )
        for entry in recipe_lines(text, first_line, recipe_prefix)
    )


def _unfollowed_make(text: str) -> str:
    """Why trace-ci does not follow a make that the printed recipe command TEXT
    runs, or "" when TEXT runs no make. A dry run runs only a line that is
    written with `+` or `$(MAKE)`, so it printed this one and did not run it."""
    found = [
        _launch(words)
        for words in map(command_words, iter_nodes(parse(text), "command"))
        if words
    ]
    if any(
        isinstance(f, tuple)
        and not {"--version", "--help", "-v", "-h"}
        & set(parse_make_args([unquote(w) for w in f[0][1:]]).options)
        for f in found
    ):
        return "this recipe line runs make, but the line does not say `$(MAKE)`, so a dry run prints it and does not run it, and trace-ci does not follow that make; write `$(MAKE)`, which a dry run runs, and which passes the make flags on"
    if UNTRACED in found:
        return f"this recipe line runs make {ELSEWHERE_TEXT}, so trace-ci does not follow that make and does not check its commands"
    return ""


def _unit_findings(
    path: str, shell: MakeShell, body: list[BodyLine], synthetics: list[Synthetic]
) -> list[TraceFinding]:
    """Write BODY, the script that one recipe shell runs, out for the content
    lints, and return the statuses that the shell loses in it."""
    start = body[0].line
    synthetics.append(
        _shell_file(
            f"make/{len(synthetics)}-{_slug(path)}-L{start}.sh",
            shell.shell,
            shell.flags,
            body,
            (path, start),
        )
    )
    options = shell_options(shell.shell, shell.flags)
    return [
        _drop_finding(
            path,
            body[min(row, len(body) - 1)].line,
            kind,
            part,
            f"{shell.shell} {shell.flags}",
        )
        for row, kind, part in dropped_statuses(
            "\n".join(line.text for line in body),
            options.errexit,
            options.pipefail,
            options.interpreter,
        )
    ]


def _drop_finding(
    path: str, line: int, kind: str, text: str, shell: str
) -> TraceFinding:
    snippet = " ".join(text.split())[:80]
    if kind == "pipe":
        why = f"this pipe stage can fail and CI does not see it, because `{shell}` has no pipefail: `{snippet}`"
    elif kind == "loop":
        why = f"this command can fail in a pass of the loop and CI does not see it, because `{shell}` has no errexit and the loop reports only its last pass: `{snippet}`"
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
    """Make each `$(shell …)` call fail, and report a call whose failure leaves
    a CI call green with fewer commands or arguments.

    A stub fails every call of its program whose argv holds its literals, so
    two calls of one command fail together. They are made to fail once, and
    the finding at the first call names the others, because the effect can
    come from any of them."""
    findings: list[TraceFinding] = []
    skipped = unexercised = 0
    sites = [
        (mf, c, fault_target(c.body))
        for mf in makefiles
        for c in shell_calls((repo / mf).read_text(encoding="utf-8", errors="replace"))
    ]
    done: set[tuple[str, tuple[str, ...]]] = set()
    with tempfile.TemporaryDirectory(prefix="trace-ci-stubs-") as tmp:
        for n, (makefile, call_site, target) in enumerate(sites):
            if target in done:
                continue
            stub_dir, marker = Path(tmp) / str(n), Path(tmp) / f"{n}.fired"
            if target is None or not write_stub(stub_dir, *target, marker):
                skipped += 1
                continue
            done.add(target)
            also = [
                f"{mf}:{c.line}"
                for mf, c, t in sites
                if c is not call_site
                and t is not None
                and t[0] == target[0]
                and set(target[1]) <= set(t[1])
            ]
            effects: list[str] = []
            fired = False
            for call, base in baselines.items():
                if base.returncode != 0:
                    continue
                marker.unlink(missing_ok=True)
                faulted = make_trace(make, call.cwd, call.args, stub_dir, call.env)
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
                        + (
                            f". trace-ci made the same command fail at {', '.join(also)} too, so the effect can come from there"
                            if also
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
    repo: Path,
    make: str,
    makefiles: list[str],
    reached: set[Path],
    goal: str,
    untraced: int,
) -> list[TraceFinding]:
    """An `uncovered` finding for each makefile with GOAL in a directory that
    no traced make reached. UNTRACED counts the `untraced` findings that the
    user sees: one of them can run make there, so the finding then says so."""
    findings: list[TraceFinding] = []
    why = (
        f"this makefile has a `{goal}` goal, but no CI step runs make in this directory, so its checks never run in CI"
        if not untraced
        else f"this makefile has a `{goal}` goal, but no make that trace-ci traced runs in this directory, and {untraced} `untraced` {'finding shows' if untraced == 1 else 'findings show'} make work that trace-ci could not follow; if none of it runs here, its checks never run in CI"
    )
    if not goal:
        return findings
    for makefile in makefiles:
        # An included `.mk` fragment has no goal of its own to run.
        if makefile.endswith(".mk"):
            continue
        directory = (repo / makefile).parent.resolve()
        if directory in reached:
            continue
        if _has_goal(make, directory, goal):
            findings.append(TraceFinding(makefile, 1, "uncovered", why))
    return findings


def _has_goal(make: str, directory: Path, goal: str) -> bool:
    """Whether the makefile in DIRECTORY has a rule for GOAL that a dry run can
    reach. A file named GOAL lets make exit 0 with no rule, so the database
    that `-p` prints must name GOAL as a target."""
    probe = subprocess.run(
        [make, "-p", "--dry-run", "-C", str(directory), goal],
        capture_output=True,
        text=True,
        env=_make_env(),
        check=False,
    )
    lines = probe.stdout.splitlines()
    return probe.returncode == 0 and any(
        re.match(rf"{re.escape(goal)}::?(?:\s|$)", line)
        and (i == 0 or lines[i - 1] != "# Not a target:")
        for i, line in enumerate(lines)
    )


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

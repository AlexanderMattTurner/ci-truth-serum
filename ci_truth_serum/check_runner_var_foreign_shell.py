#!/usr/bin/env python3
"""Ban a runner variable in a step whose shell GitHub does not set up.

A GitHub Actions step talks to the runner through four FILES, not through
memory. The runner makes each file, puts its path in an environment variable,
and reads the file back after the step ends:

  * `GITHUB_OUTPUT` — the step's outputs, which `steps.<id>.outputs.<name>` reads
  * `GITHUB_ENV` — environment variables for the LATER steps of the same job
  * `GITHUB_PATH` — directories the runner prepends to `$PATH` for later steps
  * `GITHUB_STEP_SUMMARY` — the Markdown the run's summary page shows

GitHub sets up six shells: `bash`, `sh`, `pwsh`, `powershell`, `python`, and
`cmd`. It starts each one on the runner itself, so the four paths above name
files that shell can open. Any other `shell:` value is a program the author
chose, and this pack calls it a foreign shell. A foreign shell often runs the
script somewhere else: `shell: docker run --rm -v … {0}` runs it in a
container, and a remote-execution wrapper runs it on another machine. The
environment variable still arrives, because a child process inherits it, but
the PATH it holds belongs to the runner's filesystem. In the container that
path is absent or points at a different file.

The failure is silent, which is why this is a Tier 1 honesty check. Take a step
that appends `result=ok` to `$GITHUB_OUTPUT` inside a container. The append
succeeds against a file in the container, the step exits 0, and the check run
is green. The runner then reads its OWN empty file, so `steps.build.outputs.result`
is the empty string. A later `if: steps.build.outputs.result == 'ok'` is false,
that job skips, and GitHub counts a skipped required check as satisfied. The
whole pull request reports green with the guarded work never run, and no log
line anywhere names the container as the cause.

The remedies, in the order to prefer them:

1. Move the write into a step that uses a shell GitHub sets up. Let the foreign
   shell print the value, capture it, and write it from `bash`.
2. Pass the value out of the foreign environment yourself, for example through a
   bind-mounted file, then write the runner variable outside.
3. Annotate the step with `# allow-runner-var-foreign-shell: <reason>` when the
   foreign shell demonstrably runs on the runner's own filesystem, as
   `shell: node {0}` does.

WHERE THE OPT-OUT GOES. In a YAML comment, above the step or on a comment line
inside it — never in the `run:` script. The script is a YAML string value, and a
foreign language gives this check no grammar to tell that language's comments
from its data. Honouring the marker anywhere in the script text would let
`console.log("// allow-runner-var-foreign-shell: …")` turn the check off, which
is a fail-open. `yaml_comment_view` blanks everything PyYAML does not report as
a comment, so what suppresses a finding is what GitHub parses as a comment.

WHY THIS PARSES YAML BUT SCANS THE SCRIPT AS TEXT. The two questions have
different answers. "What is this step's shell?" is structural — a `shell:` key
in a comment is not a shell, and a step's own `shell:` beats the job's
`defaults.run.shell`, which beats the workflow's — so PyYAML answers it. "Does
this script name `GITHUB_OUTPUT`?" is a question about text in an UNKNOWN
language: the script under a foreign shell may be Perl, Ruby, a Dockerfile
`RUN` body, or anything else, so there is no grammar to pick. That is the
carve-out `.claude/rules/shell-lint-parsing.md` names, and the name is the same
literal string in every language.

Known blind spots, both false-negative: a `shell:` value that holds a `${{ }}`
expression is computed, so this cannot read it and skips the step; and a step
that reaches a runner variable through a script FILE it calls is not followed
into that file.

Globs every workflow and composite action; the passed file list is ignored.
"""

import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    annotated_near,
    container_block_end,
    default_run_shell,
    shell_program,
    step_span_ends,
    workflow_files as _workflow_files,
    yaml_comment_view,
    _job_blocks,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

ALLOW = "allow-runner-var-foreign-shell"

# The six shells GitHub itself sets up, by the keyword a `shell:` names. A custom
# template that re-invokes one of them with different flags (`bash -e {0}`) still
# runs THAT interpreter on the runner, so it is judged by its first word.
# `python3` is here for the template form; it is not a keyword GitHub accepts.
NATIVE_SHELLS = frozenset(
    {"bash", "sh", "pwsh", "powershell", "python", "python3", "cmd"}
)

# The four files the runner writes for a step and reads back after it.
RUNNER_VARS = ("GITHUB_OUTPUT", "GITHUB_ENV", "GITHUB_PATH", "GITHUB_STEP_SUMMARY")
# Matched without regard to case. A Windows runner looks an environment variable
# up case-insensitively, and so do the runtimes a foreign shell starts there, so
# `$ENV{github_output}` in Perl on Windows reads the same file as the capitals do.
_RUNNER_VAR_RE = re.compile(rf"\b(?:{'|'.join(RUNNER_VARS)})\b", re.IGNORECASE)


def is_foreign_shell(shell: str | None) -> bool:
    """True when GitHub does not set this shell up, so it may run off the runner.

    False in the three cases this check must not flag. An unset or empty shell is
    the job's default (`bash` on Linux and macOS, `pwsh` on Windows). A custom
    template is judged by its first word, because that word names the program the
    runner starts, so `bash -e {0}` is still bash. And a `${{ }}` expression in
    that first word hides the program, which this check reads as unknown rather
    than as foreign.
    """
    if shell is None:
        return False
    program = shell_program(shell)
    return program is not None and program not in NATIVE_SHELLS


def runner_vars(script: str) -> list[str]:
    """Every runner variable SCRIPT names, in first-appearance order, once each.

    Reported by its canonical capitals whatever case the script spells, so one
    variable named twice in two spellings is one entry.
    """
    seen: list[str] = []
    for match in _RUNNER_VAR_RE.finditer(script):
        name = match.group().upper()
        if name not in seen:
            seen.append(name)
    return seen


def _steps(container: object) -> list[dict]:
    """The mapping steps of a job or a composite action's `runs:` block."""
    steps = container.get("steps") if isinstance(container, dict) else None
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


class Container(NamedTuple):
    """One block that holds steps: a job, or a composite action's `runs:` block.

    NAME is the key `_job_blocks` uses, so a step's opt-out window can be bounded
    by its OWN job's block. A composite action's `runs:` block is not a job and
    has no such entry, so it takes the file as its bound. LABEL is what a finding
    calls the block, and MAPPING holds the steps.
    """

    name: str
    label: str
    mapping: object


def _containers(doc: dict) -> list[Container]:
    """Every block of DOC that holds steps, in source order."""
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        return [Container(str(n), f"job {n}", job) for n, job in jobs.items()]
    runs = doc.get("runs")
    return (
        [Container("runs", "composite action", runs)] if isinstance(runs, dict) else []
    )


def _message(location: str, shell: str, names: list[str]) -> str:
    """The finding this check reports for one step."""
    return (
        f"{location} runs under `{shell}`, which GitHub does not set up. Its script "
        f"reads {', '.join(f'`${name}`' for name in names)}. Each of those "
        "variables holds the path of a file on the RUNNER. The runner reads that "
        "file after the step ends. A shell GitHub does not set up can run the "
        "script in another filesystem, such as a container or a remote host. The "
        "path is absent there, so the write reaches no file the runner reads. The "
        "step still exits 0, and its result stays empty. Write the runner variable "
        f"from a step that uses a shell GitHub sets up, or annotate `# {ALLOW}: "
        "<reason>`."
    )


def analyze(doc: object, text: str) -> list[tuple[int, str]]:
    """Every violation in one parsed document, as (step's 1-based line, message).

    TEXT is the file's own source. The opt-out lookup reads it through
    `yaml_comment_view`, so only a real YAML comment can suppress a finding.
    """
    if not isinstance(doc, dict):
        return []
    lines = yaml_comment_view(text)
    last_line = max(len(lines), 1)
    blocks = _job_blocks(text)

    found: list[tuple[int, str]] = []
    for name, label, container in _containers(doc):
        steps = _steps(container)
        if not steps:
            continue
        # Bound each step's opt-out window by its OWN job's block. A window that
        # ran to the next job's first step would swallow that job's header, so an
        # opt-out written there would suppress this job's last step.
        ends = step_span_ends(steps, container_block_end(blocks, name, last_line))
        for step in steps:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            shell = step.get("shell")
            if shell is None:
                shell = default_run_shell(container, doc)
            if shell is not None and not isinstance(shell, str):
                continue
            if not is_foreign_shell(shell):
                continue
            names = runner_vars(script)
            if not names:
                continue
            step_line = step.get("__line__")
            if not isinstance(step_line, int):
                step_line = 1
            if annotated_near(lines, step_line, ALLOW, span_end=ends.get(step_line)):
                continue
            found.append((step_line, _message(label, shell.strip(), names)))
    return sorted(found)


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every violation in one workflow's TEXT.

    A file this cannot parse as YAML is itself reported rather than passed as
    clean: "no findings" on a document nobody read is the false green this pack
    exists to refuse.
    """
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                1,
                f"could not parse as YAML ({first_line}); cannot verify that no "
                "step in a foreign shell reads a runner variable — fix the syntax "
                "(or run actionlint) and re-check.",
            )
        ]
    return analyze(doc, text)


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every violation in PATH."""
    return violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} runner-variable-in-a-foreign-shell violation(s).")
        print(
            "A runner variable holds a path on the runner. A shell GitHub does not "
            "set up can run elsewhere, so the write reaches no file the runner "
            "reads, and the step reports success with an empty result."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Make every `run:` step in a `container:` job name the shell that runs it.

GitHub runs a step with no `shell:` as `bash -e {0}` on the runner. In a job with
`container:`, it runs the same step as `sh -e {0}` inside the image. On a Debian
or Ubuntu image that is dash, and on Alpine it is BusyBox ash. actionlint and
shellcheck still read the step as bash, so a bash array passes every lint and
then fails on the runner. `if [[ … ]]` is worse: dash reports `[[: not found`,
the `if` takes its `else` branch, and the step exits 0.

Declare the shell on the step, on the job's `defaults.run`, or on the
workflow's `defaults.run`. `shell: sh` changes nothing at runtime and gives the
other lints the correct dialect. `shell: bash` is also accepted.

Opt a job out with `# shell-default-ok: <reason>` on its key line or in the
comment block directly above it. The reason is required.

Globs every workflow; the passed file list is ignored. A composite action cannot
declare `container:`, and its `run:` steps must already name a shell.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    workflow_files as _workflow_files,
    yaml_script_view,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ALLOW = "shell-default-ok"

MESSAGE = (
    "this `run:` step is in a `container:` job and names no shell. GitHub runs it "
    "with `sh -e {0}` inside the image, but actionlint and shellcheck read it as "
    "bash. Bash syntax then passes every lint and fails or misbehaves on the "
    "runner. Declare the shell the job already uses: `defaults: {run: {shell: "
    f"sh}}}}` on the job changes no behaviour. Or annotate the job `# {ALLOW}: "
    "<reason>`."
)


def _value(node: object, key: str) -> yaml.Node | None:
    """The value node under KEY when NODE is a mapping, else None."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value
    return None


def _set(node: yaml.Node | None) -> bool:
    """True when NODE holds a value. A `key:` with nothing after it is YAML null."""
    if node is None:
        return False
    return not (isinstance(node, yaml.ScalarNode) and node.tag.endswith(":null"))


def _declares_shell(scope: yaml.Node | None) -> bool:
    """True when SCOPE sets `defaults.run.shell`. Jobs and workflows share the shape."""
    return _set(_value(_value(_value(scope, "defaults"), "run"), "shell"))


def _undeclared_run_lines(job: yaml.Node) -> list[int]:
    """The 1-based `run:` key lines of JOB's steps that name no shell of their own."""
    steps = _value(job, "steps")
    if not isinstance(steps, yaml.SequenceNode):
        return []
    lines: list[int] = []
    for step in steps.value:
        if not isinstance(step, yaml.MappingNode) or _set(_value(step, "shell")):
            continue
        for key_node, _ in step.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value == "run":
                lines.append(key_node.start_mark.line + 1)
    return lines


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for each undeclared `run:` step in a container job.

    A file PyYAML cannot parse is itself a finding. A clean result on a file
    nobody read would be a false pass.
    """
    try:
        root = compose(text)
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check its shells.")]
    jobs = _value(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode) or _declares_shell(root):
        return []
    lines = text.splitlines()
    comments = yaml_script_view(text)
    found: list[int] = []
    for key_node, job in jobs.value:
        if not _set(_value(job, "container")) or _declares_shell(job):
            continue
        key_line = key_node.start_mark.line + 1
        if annotated_near(lines, key_line, ALLOW, comments=comments):
            continue
        found.extend(_undeclared_run_lines(job))
    return [(line, MESSAGE) for line in sorted(found)]


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every violation in PATH."""
    return violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR)


def main() -> None:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} container-job step(s) name no shell.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Require the mute-failure-guard action at the start of every job that runs bash.

A CI step can fail and print no reason. A bash `run:` step that ends on a failed `[[ … ]]`, or a
helper whose failure an outer command drops (`f "$(g)"`), leaves only an exit code in the log,
and each such failure costs a full diagnosis round. The action at
`actions/mute-failure-guard` points `BASH_ENV` at a guard that records every failed command in
a ledger. Its post hook prints the ledger when the job fails, and it names a failed step whose
output carries no error text.

The guard reaches only the bash steps that run after it, and a job without it leaves no ledger.
So this lint asks one question of each job: does the guard action run before the first bash
`run:` step? It flags a job when:

  * the job has a bash `run:` step, and no guard step runs before that step; or
  * the guard step carries an `if:`, so the job can skip it.

A bash step is a `run:` whose effective shell is bash: the step's `shell:`, else the job's
`defaults.run.shell`, else the workflow's, else GitHub's default. That default is bash, except
on a Windows runner. A `pwsh`, `python`, `sh` or `wsl-bash` step does not read `BASH_ENV`, so
the guard cannot reach it, and a job made only of those steps is not flagged. A reusable-workflow
call (`jobs.<id>.uses`) is checked where its workflow is defined.

The guard is the step whose `uses:` names `AlexanderMattTurner/ci-truth-serum/actions/
mute-failure-guard@<ref>`. A consumer that vendors the action names its copy with `--action`.
`--only <glob>` limits the check to the workflow files whose names match; with no `--only`,
every workflow is checked.

Opt a job out with `# allow-unguarded-job: <reason>` in a comment on the job's key line or in
its body. A coverage job is the usual case, because kcov sets `BASH_ENV` itself. The reason is
required.
"""

import argparse
import fnmatch
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import _job_blocks, workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import yaml_script_view  # noqa: E402,I001  # pylint: disable=wrong-import-position

ALLOW = "allow-unguarded-job"
PUBLISHED_ACTION = "alexandermattturner/ci-truth-serum/actions/mute-failure-guard"

_MESSAGE = (
    "job '{name}' {problem}, so a failed bash step leaves no ledger and can fail with no "
    "reason in the log. Add `- uses: AlexanderMattTurner/ci-truth-serum/actions/"
    "mute-failure-guard@<sha>` as the job's first step, with no `if:`, or add "
    f"'# {ALLOW}: <reason>' to the job."
)


def _is_guard(uses: object, actions: tuple[str, ...]) -> bool:
    """True when USES names the published guard at any ref, or one of ACTIONS exactly."""
    if not isinstance(uses, str):
        return False
    name, _, ref = uses.partition("@")
    if name.lower() == PUBLISHED_ACTION and ref:
        return True
    return uses in actions


def _shell_of(value: object) -> str | None:
    """The `shell` a `defaults.run` mapping sets, or None."""
    run = value.get("run") if isinstance(value, dict) else None
    shell = run.get("shell") if isinstance(run, dict) else None
    return shell if isinstance(shell, str) else None


def _is_bash(shell: str) -> bool:
    """True when SHELL starts bash itself: `bash`, or a template such as `/bin/bash -e {0}`."""
    words = shell.split()
    return bool(words) and Path(words[0]).name == "bash"


def _first_problem(
    job: dict, workflow_shell: str | None, actions: tuple[str, ...]
) -> str | None:
    """Why JOB needs the guard and lacks it, or None when it is guarded or runs no bash."""
    default = _shell_of(job.get("defaults")) or workflow_shell
    if default is None and "windows" in str(job.get("runs-on", "")).lower():
        default = "pwsh"
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        if _is_guard(step.get("uses"), actions):
            if "if" in step:
                return "starts the mute-failure guard under an `if:` that can skip it"
            return None
        shell = step.get("shell") if isinstance(step.get("shell"), str) else default
        if "run" in step and _is_bash(shell or "bash"):
            return "runs a bash step before any mute-failure guard"
    return None


def check_file(
    path: Path, actions: tuple[str, ...] = ()
) -> list[tuple[int | None, str]]:
    """Return (line, message) for each job in PATH that runs bash without the guard first.

    A file that does not parse as YAML is itself a finding, so it is never passed as clean."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot check its jobs for the guard.",
            )
        ]
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    workflow_shell = _shell_of(doc.get("defaults"))
    blocks = _job_blocks(text)
    found: list[tuple[int | None, str]] = []
    for name, job in jobs.items():
        if not isinstance(job, dict) or "uses" in job:
            continue
        problem = _first_problem(job, workflow_shell, actions)
        if problem is None:
            continue
        block = blocks.get(str(name))
        if block and any(annotated(line, ALLOW) for line in yaml_script_view(block[1])):
            continue
        found.append(
            (block[0] if block else 1, _MESSAGE.format(name=name, problem=problem))
        )
    return found


def main(argv: list[str]) -> None:
    """Check the workflows in `.github/workflows`, or the ones `--only` names."""
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--only", action="append", default=[], metavar="GLOB")
    parser.add_argument("--action", action="append", default=[], metavar="USES")
    parser.add_argument(
        "paths", nargs="*", help="ignored: the check finds its own workflows"
    )
    args = parser.parse_args(argv)
    root = Path.cwd()
    total = 0
    for path in workflow_files(root / ".github" / "workflows"):
        if args.only and not any(
            fnmatch.fnmatch(path.name, glob) for glob in args.only
        ):
            continue
        rel = path.relative_to(root)
        for line, message in check_file(path, tuple(args.action)):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} job(s) run bash without the mute-failure guard.")
        raise SystemExit(1)


if __name__ == "__main__":
    main(sys.argv[1:])

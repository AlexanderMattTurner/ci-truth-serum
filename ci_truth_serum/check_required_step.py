#!/usr/bin/env python3
"""Refuse a job that runs a script before the step that the script declares it needs.

Some scripts work only after an earlier step in the same job. One example is an
install script that refuses to run unless a cache restore ran first and left a
record. Such a script refuses at run time, but only in a job that runs. A job
that runs only on a schedule or after a merge then fails on the default
branch, after review. This check reads every job before the merge.

A script declares the step it needs with a comment that names a `uses:` value:

    # requires-step: ./.github/actions/cache-pip-tools

The check follows each `run:` step into the repository scripts that
`_cts_step_reach` resolves, and into local composite actions. The walk reads
the step's working directory and environment. When a reached shell script
carries the comment, an earlier step in the same job must use exactly that
value. A trailing `/` does not count, and a remote action counts only when the
job pins the same ref that the comment names.

The comment is the opt-in, so the check has no opt-out marker. The check does
not read the comment in a Python file that a step hands an interpreter, and it
ignores `if:` conditions.

Globs every workflow; the passed file list is ignored.
"""

import functools
import sys
from pathlib import Path

import yaml
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_uncached_download as uncached  # noqa: I001  # pylint: disable=wrong-import-position
from _cts_bash_ast import PathologicalInputError, iter_nodes, node_text  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # pylint: disable=wrong-import-position
    LineLoader,
    annotation_re,
    workflow_files,
)
from _cts_step_reach import reach  # pylint: disable=wrong-import-position

WORKFLOWS_DIR = uncached.REPO_ROOT / ".github" / "workflows"
MARKER = "requires-step"

MESSAGE = (
    "job '{job}' runs {script}, which declares `# {marker}: {needed}`, and no "
    "earlier step in the job uses `{needed}`. A job that runs only on a schedule "
    "or after a merge then fails on the default branch. Add `- uses: {needed}` "
    "to the job before this step."
)


@functools.cache
def _declared(text: str, root: Node) -> frozenset[str]:
    """The `uses:` values that the shell script TEXT, parsed as ROOT, declares
    it needs. Most scripts never spell the marker, so a text test skips their
    comment walk."""
    if MARKER not in text:
        return frozenset()
    matcher = annotation_re(MARKER)
    needed = set()
    for comment in iter_nodes(root, "comment"):
        match = matcher.search(node_text(comment))
        if match:
            needed.add(match.group("reason").strip().rstrip("/"))
    return frozenset(needed)


def _needs(script: str, run: uncached.StepRun) -> dict[str, str]:
    """Each `uses:` value that a file the `run:` body SCRIPT reaches declares,
    mapped to the first file that declares it. The body itself declares
    nothing: the marker belongs to the script that needs the step."""
    root = uncached.REPO_ROOT
    reached = reach(script, root, run.action_dir, run.directory, run.environment)
    found: dict[str, str] = {}
    for namespace in reached.namespaces:
        for unit in namespace.units:
            if unit.path is None:
                continue
            for needed in _declared(unit.text, unit.root):
                found.setdefault(needed, unit.path.relative_to(root).as_posix())
    return found


def violations(text: str, path: Path) -> list[tuple[int, str]]:
    """(1-based line, message) for each step in the workflow TEXT at PATH that
    runs a script before the step that the script declares.

    A workflow, or a composite action that it uses, that PyYAML cannot parse is
    a finding. So is a script that the bash grammar cannot parse safely. A clean
    result on a file nobody read would be a false pass."""
    try:
        return _violations(text, path)
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        reason = (
            f"could not parse this workflow or an action it uses as YAML "
            f"({first}); cannot check its jobs."
        )
        return [(1, reason)]
    except PathologicalInputError as err:
        return [(1, str(err))]


def _violations(text: str, path: Path) -> list[tuple[int, str]]:
    doc = yaml.load(text, Loader=LineLoader)
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    found: list[tuple[int, str]] = []
    for name, job in jobs.items():
        default_directory = (
            uncached._default_directory(doc, job) if isinstance(job, dict) else ""  # pylint: disable=protected-access
        )
        used: set[str] = set()
        for flat in uncached.job_steps(doc, job, 1, path):
            script = flat.step.get("run")
            if isinstance(script, str):
                run = uncached.step_run(flat, default_directory)
                for needed, holder in sorted(_needs(script, run).items()):
                    if needed not in used:
                        message = MESSAGE.format(
                            job=name, script=holder, marker=MARKER, needed=needed
                        )
                        found.append((flat.line, message))
            uses = flat.step.get("uses")
            if isinstance(uses, str):
                used.add(uses.rstrip("/"))
    return found


def main() -> None:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(uncached.REPO_ROOT)
        for line, message in violations(path.read_text(encoding="utf-8"), path):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} step(s) run a script before the step it needs.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

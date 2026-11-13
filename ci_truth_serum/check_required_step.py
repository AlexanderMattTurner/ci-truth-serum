#!/usr/bin/env python3
"""Refuse a job that runs a script before the step that the script declares it needs.

Some scripts work only after an earlier step in the same job, such as an
installer that refuses to run until a cache restore leaves a record. A job that
runs only on a schedule or after a merge finds that gap on the default branch.

A script declares the step it needs with a comment that names a `uses:` value:

    # requires-step: ./.github/actions/cache-pip-tools

The check follows each `run:` step, in its working directory, into the shell
scripts that `_cts_step_reach` resolves, and into local composite actions. An
earlier step in the same job must use the declared value. A trailing `/` on
either value is ignored. The comment is the opt-in, so there is no opt-out
marker. The check ignores `if:` conditions and Python files.

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
    "earlier step in the job uses `{needed}`. Add `- uses: {needed}` to the job "
    "before this step."
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
        used: set[str] = set()
        for flat in uncached.job_steps(doc, job, 1, path):
            script = flat.step.get("run")
            if isinstance(script, str):
                run = uncached.step_run(flat, doc, job)
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

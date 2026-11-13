#!/usr/bin/env python3
"""Refuse a job that runs a script before the step that script declares it needs.

Some scripts work only after an earlier step in the same job. One example is an
install script that refuses to run unless a cache restore ran first and left a
record. The script refuses at run time, but only in a job that runs. A job that
runs only on a schedule or after a merge then fails on the default branch,
after review. This check reads every job before the merge.

A script declares the step it needs with a comment that names a `uses:` value:

    # requires-step: ./.github/actions/cache-pip-tools

The check follows each `run:` step into every repository script it runs or
sources (`_cts_step_reach`), and into local composite actions. When a reached
shell script carries the comment, an earlier step in the same job must use
exactly that value. A remote action therefore counts only when the job pins
the same ref that the comment names.

The comment is the opt-in, so the check has no opt-out marker: a job that
skips the declared step fails at run time anyway. The check does not read the
comment in a Python file that a step hands an interpreter.

Globs every workflow; the passed file list is ignored.
"""

import functools
import sys
from pathlib import Path
from typing import NamedTuple

import yaml
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import PathologicalInputError, iter_nodes, node_text  # noqa: I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # pylint: disable=wrong-import-position
    LineLoader,
    annotation_re,
    workflow_files,
)
from _cts_step_reach import reach  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
MARKER = "requires-step"

MESSAGE = (
    "job '{job}' runs {script}, which declares `# {marker}: {needed}`, and no "
    "earlier step in the job uses `{needed}`. The script refuses at run time "
    "without that step, so a job that runs only on a schedule or after a merge "
    "fails on the default branch. Add `- uses: {needed}` to the job before this step."
)


def _steps(container: object) -> list[dict]:
    steps = container.get("steps") if isinstance(container, dict) else None
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _action_file(uses: str, root: Path) -> Path | None:
    """The definition file of the local composite action that USES names."""
    directory = (root / uses.removeprefix("./").rstrip("/")).resolve()
    if not directory.is_relative_to(root.resolve()):
        return None
    for name in ("action.yaml", "action.yml"):
        if (directory / name).is_file():
            return directory / name
    return None


class JobStep(NamedTuple):
    """One step that a job runs: the step mapping, the workflow line to report
    it at, and the composite action it came from (empty in the workflow)."""

    step: dict
    line: int
    origin: str


def _flat(
    steps: list[dict], line: int | None, origin: str, seen: frozenset, root: Path
) -> list[JobStep]:
    """Every step that a job runs, in run order. A step from a composite action
    keeps the line of the workflow step that reached it. SEEN ends a cycle of
    actions."""
    flat: list[JobStep] = []
    for step in steps:
        at = line if line is not None else step.get("__line__", 1)
        flat.append(JobStep(step, at, origin))
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./"):
            continue
        target = _action_file(uses, root)
        if target is None or target in seen:
            continue
        doc = yaml.load(target.read_text(encoding="utf-8"), Loader=LineLoader)
        inner = _steps(doc.get("runs")) if isinstance(doc, dict) else []
        flat += _flat(inner, at, uses.rstrip("/"), seen | {target}, root)
    return flat


@functools.cache
def _declared(path: Path, text: str, root: Node) -> frozenset[str]:
    """The `uses:` values that the shell script at PATH declares it needs.
    TEXT and ROOT are the script and its parse tree. Most scripts never spell
    the marker, so a text test skips their comment walk."""
    if MARKER not in text:
        return frozenset()
    matcher = annotation_re(MARKER)
    needed = set()
    for comment in iter_nodes(root, "comment"):
        match = matcher.search(node_text(comment))
        if match:
            needed.add(match.group("reason").strip())
    return frozenset(needed)


def _needs(step: dict, origin: str, root: Path) -> dict[str, str]:
    """Each `uses:` value that a script STEP reaches declares, mapped to the
    first script that declares it."""
    script = step.get("run")
    if not isinstance(script, str):
        return {}
    found: dict[str, str] = {}
    for namespace in reach(script, root, origin.removeprefix("./") or None).namespaces:
        for unit in namespace.units:
            if unit.path is None:
                continue
            for needed in _declared(unit.path, unit.text, unit.root):
                found.setdefault(needed, unit.path.relative_to(root).as_posix())
    return found


def violations(text: str, root: Path) -> list[tuple[int, str]]:
    """(1-based line, message) for each step in the workflow TEXT that runs a
    script before the step that the script declares. ROOT is the repository
    that the steps run in.

    A workflow or composite action that PyYAML cannot parse is a finding, and
    so is a script that the bash grammar cannot parse safely. A clean result on
    a file nobody read would be a false pass."""
    try:
        return _violations(text, root)
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check its jobs.")]
    except PathologicalInputError as err:
        return [(1, str(err))]


def _violations(text: str, root: Path) -> list[tuple[int, str]]:
    doc = yaml.load(text, Loader=LineLoader)
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return []
    found: list[tuple[int, str]] = []
    for name, job in jobs.items():
        used: set[str] = set()
        for step, line, origin in _flat(_steps(job), None, "", frozenset(), root):
            for needed, script in sorted(_needs(step, origin, root).items()):
                if needed in used:
                    continue
                message = MESSAGE.format(
                    job=name, script=script, marker=MARKER, needed=needed
                )
                found.append((line, message))
            uses = step.get("uses")
            if isinstance(uses, str):
                used.add(uses.rstrip("/"))
    return found


def main() -> None:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        for line, message in violations(path.read_text(encoding="utf-8"), REPO_ROOT):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} step(s) run a script before the step it needs.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

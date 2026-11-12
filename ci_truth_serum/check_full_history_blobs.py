#!/usr/bin/env python3
"""Refuse a full-history checkout that downloads every file version it never reads.

An `actions/checkout` step with `fetch-depth: 0` gives the job every commit. A
job needs that for a merge base, a walk over the commits, or a tag. With no
`filter:` input, the step also downloads every version of every file, which is
most of the bytes in a clone. A slow git cache can make that transfer outlast
the job's deadline. `filter: blob:none` keeps the commits and the trees. Git
then fetches the content of a file only when a step reads it.

Any `filter:` input passes. `--require-filter=<value>` demands one exact value.
A private repository refuses the anonymous fetch that a blobless clone uses to
get a missing file, so a repository that has private copies can demand an
expression that gives an empty filter there.

Opt a job out with `# full-blobs-ok: <reason>` on its key line or in the comment
block directly above it. The reason is required. These jobs need the opt-out:

- a job that reads file CONTENT across history: `git range-diff`, a
  `--numstat` walk, or `git archive` of another commit;
- a job that copies history out of the checkout by push or fetch. A plain
  repository cannot fetch from a blobless clone: `upload-pack` runs with lazy
  fetching off and stops at the first missing file.

Globs every workflow; the passed file list is ignored.
"""

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_checkout_uses,
    source_lines,
    workflow_files as _workflow_files,
    yaml_marker_comments,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ALLOW = "full-blobs-ok"

MESSAGE = (
    "this full-history checkout downloads every version of every file, and the "
    "job reads almost none of them. Through a slow git cache, that transfer can "
    "outlast the job's deadline. Add {remedy} beside this `fetch-depth: 0`. A job "
    "that reads file content across history (`git range-diff`, a `--numstat` "
    "walk, `git archive` of another commit), or that pushes or fetches history "
    f"out of the checkout, needs all of it: annotate that job `# {ALLOW}: <reason>`."
)


def _value(node: object, key: str) -> yaml.Node | None:
    """The value node under KEY when NODE is a mapping, else None."""
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value
    return None


def _scalar(node: yaml.Node | None) -> str | None:
    """NODE's text when it is a scalar, else None."""
    return node.value if isinstance(node, yaml.ScalarNode) else None


def _unfiltered_full_checkouts(job: yaml.Node, required: str | None) -> list[int]:
    """The 1-based `fetch-depth:` lines of JOB's full-history checkouts whose
    filter is absent, or differs from REQUIRED when REQUIRED is set."""
    steps = _value(job, "steps")
    if not isinstance(steps, yaml.SequenceNode):
        return []
    lines: list[int] = []
    for step in steps.value:
        if not is_checkout_uses(_scalar(_value(step, "uses"))):
            continue
        inputs = _value(step, "with")
        depth = _value(inputs, "fetch-depth")
        if _scalar(depth) != "0":
            continue
        found = _value(inputs, "filter")
        if found is None or (required is not None and _scalar(found) != required):
            lines.append(depth.start_mark.line + 1)
    return lines


def violations(text: str, required: str | None = None) -> list[tuple[int, str]]:
    """(1-based line, message) for each refused full-history checkout in TEXT.

    A file PyYAML cannot parse is itself a finding. A clean result on a file
    nobody read would be a false pass.
    """
    try:
        root = compose(text)
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check checkouts.")]
    jobs = _value(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return []
    lines = source_lines(text)
    comments = yaml_marker_comments(text)
    found: list[int] = []
    for key_node, job in jobs.value:
        if annotated_near(
            lines, key_node.start_mark.line + 1, ALLOW, comments=comments
        ):
            continue
        found.extend(_unfiltered_full_checkouts(job, required))
    remedy = "`filter: blob:none`" if required is None else f"`filter: {required}`"
    return [(line, MESSAGE.format(remedy=remedy)) for line in found]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-filter",
        metavar="VALUE",
        help="the exact `filter:` value every full-history checkout must carry "
        "(default: any `filter:` passes)",
    )
    args = parser.parse_args(argv)
    total = 0
    for path in _workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text(encoding="utf-8")
        for line, message in violations(text, args.require_filter):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} full-history checkout(s) have no blob filter.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

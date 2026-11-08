#!/usr/bin/env python3
"""Refuse a GitHub-hosted runner label that GitHub moves to a new OS release.

`ubuntu-latest`, `macos-latest` and `windows-latest` are aliases. GitHub points
each one at a new OS image about every two years, and no commit records the
move. In 2026 `ubuntu-latest` moved from Ubuntu 24.04 to 26.04, and one merge
queue stayed red until each workflow named `ubuntu-24.04`. Name the release, so
a commit moves it and the diff shows why CI changed.

The check reads every workflow and composite action as YAML, and flags each
scalar VALUE whose decoded text names an alias. That finds the label in every
place that picks a runner: `runs-on:` in any form, a matrix entry, a `${{ }}`
fallback, an input default, or an `env:` value. It skips mapping keys, YAML
comments, and the `description:` text of the action or of an input, output or
secret. The larger-runner forms (`ubuntu-latest-4-cores`) move with their
alias, so they count too. A vendor label that holds an alias inside a longer
name (`acme-ubuntu-latest`) does not.

A finding lands on the line where its scalar starts. Opt out with
`# allow-moving-runner-alias: <reason>` on that line or in the comment block
above it. The reason is required.

Globs every workflow and composite action; the passed file list is ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import PathologicalInputError  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    source_lines,
    workflow_files as _workflow_files,
    yaml_marker_comments,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOW = "allow-moving-runner-alias"

# A GitHub-hosted alias, with any larger-runner suffix. GitHub matches runner
# labels without regard to case.
ALIAS = re.compile(
    r"(?<![\w.-])(?:ubuntu|macos|windows)-latest(?:-[a-z0-9]+)*(?![\w.-])",
    re.IGNORECASE,
)

# The maps whose entries each carry a `description:` that GitHub shows as text.
_DESCRIBED = frozenset({"inputs", "outputs", "secrets"})


def _is_prose(path: tuple[str, ...], key: str) -> bool:
    """True when KEY under PATH (the keys from the root) holds display text:
    the action's `description`, `inputs.<id>.description`, or
    `on.<event>.inputs.<id>.description`. A key of that name anywhere else,
    such as a matrix axis, is data."""
    if key != "description":
        return False
    if len(path) == 2:
        return path[0] in _DESCRIBED
    return not path or (len(path) == 4 and path[0] == "on" and path[2] in _DESCRIBED)


def _value_scalars(root: yaml.Node) -> list[yaml.ScalarNode]:
    """Every scalar VALUE in ROOT that is not display text, once each."""
    found: list[yaml.ScalarNode] = []
    stack: list[tuple[yaml.Node, tuple[str, ...]]] = [(root, ())]
    seen: set[int] = set()
    while stack:
        node, path = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            found.append(node)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend((item, path) for item in node.value)
        elif isinstance(node, yaml.MappingNode):
            for key_node, value in node.value:
                key = key_node.value if isinstance(key_node, yaml.ScalarNode) else ""
                if not _is_prose(path, key):
                    stack.append((value, (*path, key)))
    return found


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for each scalar that names a moving alias.

    A file PyYAML cannot parse is itself a finding: a clean result on a file
    nobody read would be a false pass.
    """
    try:
        root = compose(text)
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check its runners.")]
    hits = {
        scalar.start_mark.line + 1: match.group()
        for scalar in (_value_scalars(root) if root is not None else [])
        if (match := ALIAS.search(scalar.value))
    }
    if not hits:
        return []
    lines = source_lines(text)
    comments = yaml_marker_comments(text)
    return [
        (line, _message(alias))
        for line, alias in sorted(hits.items())
        if not annotated_near(lines, line, ALLOW, comments=comments)
    ]


def _message(alias: str) -> str:
    return (
        f"`{alias}` is an alias that GitHub moves to a new OS release with no "
        "commit in this repository, so CI can change under an unchanged diff. "
        "Name the release, such as `ubuntu-24.04`, `macos-15` or "
        f"`windows-2025`. Or annotate the line `# {ALLOW}: <reason>`."
    )


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every violation in PATH."""
    return violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> None:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            # Reading the opt-outs parses each bash `run:`. A script the
            # grammar refuses is a file this check did not finish reading.
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} finding(s). Name the OS release of each runner.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

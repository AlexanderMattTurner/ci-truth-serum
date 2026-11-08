#!/usr/bin/env python3
"""Refuse a GitHub-hosted runner label that GitHub moves to a new OS release.

`ubuntu-latest`, `macos-latest` and `windows-latest` are aliases. GitHub points
each one at a new OS image about every two years, and no commit records the
move. In 2026 `ubuntu-latest` moved from Ubuntu 24.04 to 26.04, and one merge
queue stayed red until each workflow named `ubuntu-24.04`. Name the release, so
a commit moves it and the diff shows why CI changed.

The check reads every workflow and composite action as YAML. It flags each
scalar VALUE that names an alias, so it finds every place that picks a runner:

  * `runs-on:` as a string, a list, or a `labels:` mapping;
  * a `strategy.matrix` entry;
  * a `${{ }}` expression, such as a fallback `vars.RUNNER || 'ubuntu-latest'`;
  * an input default, or an `env:` value that a later job reads.

The check skips three kinds of text, because none of them picks a runner:

  * a YAML comment, which is not a scalar;
  * the value of a `description:` key, which GitHub shows and never runs;
  * a shell comment inside a `run:` value that GitHub hands to bash. The bash
    grammar decides where that comment starts, so `echo "a # b"` stays code.

The larger-runner forms (`ubuntu-latest-4-cores`, `macos-latest-xlarge`) move
with their alias, so the check flags them too. A third-party label that holds
an alias inside a longer name (`acme-ubuntu-latest`) belongs to its vendor, and
the check leaves it alone.

Opt out with `# allow-moving-runner-alias: <reason>` on the finding's line or
in the comment block directly above it. The reason is required.

Globs every workflow and composite action; the passed file list is ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import strip_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    source_lines,
    workflow_files as _workflow_files,
    yaml_marker_comments,
    yaml_run_scalars,
    yaml_run_script,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOW = "allow-moving-runner-alias"

# A GitHub-hosted alias, with any larger-runner suffix (`-4-cores`, `-xlarge`).
# GitHub matches runner labels without regard to case. The lookarounds keep a
# vendor label that only CONTAINS an alias (`acme-ubuntu-latest`) and a dotted
# word (`ubuntu-latest.txt`) out of the match.
ALIAS = re.compile(
    r"(?<![\w.-])(?:ubuntu|macos|windows)-latest(?:-[a-z0-9]+)*(?![\w.-])",
    re.IGNORECASE,
)

# A key whose value GitHub shows as text and never runs.
_PROSE_KEYS = frozenset({"description"})


def _message(alias: str) -> str:
    return (
        f"`{alias}` is an alias that GitHub moves to a new OS release with no "
        "commit in this repository, so CI can change under an unchanged diff. "
        "Name the release, such as `ubuntu-24.04`, `macos-15` or "
        f"`windows-2025`. Or annotate the line `# {ALLOW}: <reason>`."
    )


def _value_scalars(root: yaml.Node) -> list[yaml.ScalarNode]:
    """Every scalar in ROOT that is a VALUE, once each.

    A mapping key names a field and never a runner, and a `description:` value
    is prose. A node that a YAML alias reaches twice is the same source text,
    so the walk visits it once.
    """
    found: list[yaml.ScalarNode] = []
    stack: list[yaml.Node] = [root]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, yaml.ScalarNode):
            found.append(node)
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            stack.extend(
                value
                for key, value in node.value
                if not (isinstance(key, yaml.ScalarNode) and key.value in _PROSE_KEYS)
            )
    return found


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for each moving alias in a workflow or action TEXT.

    The check matches the SOURCE text of each scalar, not its decoded value, so
    a finding inside a multi-line value lands on its own line. A file PyYAML
    cannot parse is itself a finding: a clean result on a file nobody read
    would be a false pass.
    """
    try:
        root = compose(text)
        bash_runs = {s.start: s for s in yaml_run_scalars(text) if s.is_bash}
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check its runners.")]
    if root is None:
        return []
    lines = source_lines(text)
    comments = yaml_marker_comments(text)
    found: dict[int, str] = {}
    for scalar in _value_scalars(root):
        start = scalar.start_mark.index
        run = bash_runs.get(start)
        source = (
            strip_comments(yaml_run_script(text, run))
            if run is not None
            else text[start : scalar.end_mark.index]
        )
        for match in ALIAS.finditer(source):
            line = text.count("\n", 0, start + match.start()) + 1
            if line in found or annotated_near(lines, line, ALLOW, comments=comments):
                continue
            found[line] = _message(match.group())
    return sorted(found.items())


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every violation in PATH."""
    return violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> None:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} moving runner alias(es) found.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()

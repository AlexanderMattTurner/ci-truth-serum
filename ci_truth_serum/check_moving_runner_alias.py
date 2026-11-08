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
  * the `description:` text of the action, or of an input, output or secret,
    which GitHub shows and never runs;
  * a shell comment inside a `run:` value that GitHub hands to bash. The bash
    grammar decides where that comment starts, so `echo "a # b"` stays code.

A `run:` in another shell (`pwsh`, `python`) is read whole, because the bash
grammar cannot find its comments. Its findings land on the `run:` line, where
a YAML comment can carry the opt-out.

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
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import PathologicalInputError, strip_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    RunScalar,
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

# A GitHub-hosted alias, with any larger-runner suffix (`-4-cores`, `-xlarge`),
# including one an expression computes (`-${{ inputs.size }}`). GitHub matches
# runner labels without regard to case. The lookarounds keep a vendor label
# that only CONTAINS an alias (`acme-ubuntu-latest`) and a dotted word
# (`ubuntu-latest.txt`) out of the match.
ALIAS = re.compile(
    r"(?<![\w.-])(?:ubuntu|macos|windows)-latest(?:-[a-z0-9]+)*(?![\w.]|-(?!\$))",
    re.IGNORECASE,
)

# The maps whose entries each carry a `description:` that GitHub shows as text:
# the inputs, outputs and secrets of a workflow trigger or of an action.
_DESCRIBED = frozenset({"inputs", "outputs", "secrets"})


def _message(alias: str) -> str:
    return (
        f"`{alias}` is an alias that GitHub moves to a new OS release with no "
        "commit in this repository, so CI can change under an unchanged diff. "
        "Name the release, such as `ubuntu-24.04`, `macos-15` or "
        f"`windows-2025`. Or annotate the line `# {ALLOW}: <reason>`."
    )


def _is_prose(path: tuple[str, ...], key: str) -> bool:
    """True when KEY holds a `description:` text that GitHub shows and never runs.

    PATH holds the keys from the root down to the mapping that holds KEY. Three
    places qualify, each at a fixed depth:

      * the action's own description, at the root;
      * an action input or output: `inputs.<id>.description`;
      * a trigger's input, output or secret: `on.<event>.inputs.<id>.description`.

    A key of that name anywhere else is data. A job whose ID is `inputs` and a
    reusable workflow's `with: {description: …}` both still get read.
    """
    if key != "description":
        return False
    if not path:
        return True
    if len(path) == 2:
        return path[0] in _DESCRIBED
    return len(path) == 4 and path[0] == "on" and path[2] in _DESCRIBED


def _value_scalars(root: yaml.Node) -> list[yaml.ScalarNode]:
    """Every scalar in ROOT that is a VALUE and not prose, once each.

    A mapping key names a field and never a runner. A node that a YAML alias
    reaches twice is the same source text, so the walk visits it once.
    """
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


def _scalar_hits(
    text: str, scalar: yaml.ScalarNode, run: RunScalar | None
) -> list[tuple[int, str]]:
    """(1-based line, alias) for each alias that SCALAR hands to GitHub.

    The written form keeps every offset, so a match in it lands on its own line.
    The decoded form is what GitHub receives, so an escape such as
    `"ubuntu\\u002dlatest"` cannot hide an alias: each alias that only that form
    holds lands on the scalar's first line. RUN is the step SCALAR is the
    script of, if any. A bash script loses its comments first.
    """
    start = scalar.start_mark.index
    first_line = scalar.start_mark.line + 1
    written = text[start : scalar.end_mark.index]
    decoded = scalar.value
    # A shortlist only: for a script, the grammar below decides what is code.
    if not (ALIAS.search(written) or ALIAS.search(decoded)):
        return []
    if run is not None and not run.is_bash:
        return [(first_line, m.group()) for m in ALIAS.finditer(decoded)]
    if run is not None:
        written = strip_comments(yaml_run_script(text, run))
        decoded = strip_comments(decoded)
    hits = [
        (text.count("\n", 0, start + m.start()) + 1, m.group())
        for m in ALIAS.finditer(written)
    ]
    spelled = Counter(alias.lower() for _line, alias in hits)
    hidden = Counter(m.group().lower() for m in ALIAS.finditer(decoded)) - spelled
    return hits + [(first_line, alias) for alias in hidden.elements()]


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for each moving alias in a workflow or action TEXT.

    A file PyYAML cannot parse is itself a finding: a clean result on a file
    nobody read is a false pass.
    """
    try:
        root = compose(text)
        runs: dict[int, RunScalar] = {}
        for run in yaml_run_scalars(text):
            # A step that a YAML alias shares between jobs is read as bash only
            # when every job runs it under bash.
            if run.start not in runs or not run.is_bash:
                runs[run.start] = run
    except yaml.YAMLError as err:
        first = str(err).partition("\n")[0]
        return [(1, f"could not parse as YAML ({first}); cannot check its runners.")]
    if root is None:
        return []
    hits = [
        hit
        for scalar in _value_scalars(root)
        for hit in _scalar_hits(text, scalar, runs.get(scalar.start_mark.index))
    ]
    if not hits:
        return []
    lines = source_lines(text)
    comments = yaml_marker_comments(text, [s for s in runs.values() if s.is_bash])
    found: dict[int, str] = {}
    for line, alias in hits:
        if line in found or annotated_near(lines, line, ALLOW, comments=comments):
            continue
        found[line] = _message(alias)
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
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            # A `run:` the bash grammar refuses is a file this check did not
            # read. Report it, and read the files after it.
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

#!/usr/bin/env python3
"""Keep a SHA-pinned GitHub Action pinned to ONE SHA repo-wide.

That an action is pinned to a SHA at all is a job for a per-reference audit
(zizmor's `unpinned-uses`, or this pack's own scan of the same lines). This
check covers what a per-reference audit cannot: two references to the SAME
action carrying DIFFERENT SHAs. Both refs are validly pinned, so nothing about
either reference alone is wrong — divergence is a property of the whole set. It
means a version bump updated some call sites and missed others, which also
makes at least one inline `# vX.Y` comment a lie about which release its SHA
names.

A `./`-relative action has no upstream release to diverge from and is skipped;
an unpinned (tag/branch) ref is a different lint's finding and is skipped here
too, or the same ref would be reported twice.

`uses:` is read from a real YAML parse (a composed node tree), not a line
regex: a `uses:` line inside a `run: |` block scalar is DATA, not a reference,
and a quoted value (`uses: "actions/checkout@<sha>"`) is still one reference.
Composing (rather than fully constructing) the document walks only mapping and
sequence nodes, so a multi-line string's contents are a scalar LEAF the walk
never descends into — a `uses:` string typed inside one is never visited.

Opt out with a same-line `# divergent-pin-ok: <reason>` — the reason is
REQUIRED, matching the sibling `check_exit_suppression` /
`check_substitution_exit_swallow` contract. Opting out ONE occurrence does not
exempt the action: divergence is still computed from every occurrence (opted
out or not), so an unannotated stale pin next to an annotated one is still
reported. Annotating every occurrence of a genuinely deliberate split is what
silences the group.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _fastyaml import compose  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_pin_comment_truth import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ACTIONS_DIR,
    REPO_ROOT,
    Violation,
    WORKFLOWS_DIR,
)

OPT_OUT = "divergent-pin-ok"

# A resolved `uses:` scalar naming a SHA-pinned Action: `owner/repo@<40-hex>`,
# possibly with a `/sub/path` segment. A `./`-relative action (no `/…@` split
# an upstream release could diverge from) and a tag/branch ref (no 40-hex SHA)
# both simply fail this match, which is the skip — no separate branch needed.
_SHA_PIN = re.compile(r"^(?P<ref>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40})$")


def _iter_uses_nodes(node):
    """Yield every YAML scalar VALUE node bound to a `uses:` key anywhere under
    NODE — a composed (not constructed) tree, so a `run: |` block scalar is a
    leaf this never opens, and a quoted scalar's resolved value is already
    unquoted."""
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            if (
                isinstance(key_node, yaml.ScalarNode)
                and key_node.value == "uses"
                and isinstance(value_node, yaml.ScalarNode)
            ):
                yield value_node
            else:
                yield from _iter_uses_nodes(value_node)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            yield from _iter_uses_nodes(child)


class ActionPin(NamedTuple):
    """One SHA-pinned `uses:` reference: where it sits, what it names, and
    whether it opted out of this check."""

    line: int
    action: str
    sha: str
    opted_out: bool


class ActionPinRecord(NamedTuple):
    """An `ActionPin` with its source file attached, so divergence can be
    compared across every file at once."""

    path: str
    line: int
    action: str
    sha: str
    opted_out: bool


def pin_records(text: str) -> list[ActionPin]:
    """(1-based line, action, sha, opted_out) for every SHA-pinned `uses:`
    reference composed from TEXT."""
    raw = text.splitlines()
    records: list[ActionPin] = []
    for value_node in _iter_uses_nodes(compose(text)):
        m = _SHA_PIN.match(value_node.value)
        if not m:
            continue
        lineno = value_node.start_mark.line + 1
        line = raw[lineno - 1] if 0 < lineno <= len(raw) else ""
        records.append(
            ActionPin(lineno, m.group("ref"), m.group("sha"), annotated(line, OPT_OUT))
        )
    return records


def check_files(texts: list[tuple[str, str]]) -> list[Violation]:
    """(path, line, message) for every divergent pin across TEXTS ((path,
    content) pairs) — divergence is a property of the whole tree, so every
    reference (opted out or not) must be compared against every other one.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line 1) rather than silently passed as clean — matching the sibling
    workflow lints (`check_always_reporter` &c.) — and does not stop the
    remaining files from being checked against each other."""
    all_records: list[ActionPinRecord] = []
    parse_errors: list[Violation] = []
    for path, text in texts:
        try:
            records = pin_records(text)
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            parse_errors.append(
                Violation(
                    path,
                    1,
                    f"could not parse as YAML ({first_line}); cannot check for "
                    "divergent action pins — fix the syntax (or run actionlint) "
                    "and re-check.",
                )
            )
            continue
        all_records += [
            ActionPinRecord(path, line, action, sha, opted)
            for line, action, sha, opted in records
        ]

    shas_by_action: dict[str, set[str]] = defaultdict(set)
    sites_by_action_sha: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(
        list
    )
    for path, line, action, sha, _opted in all_records:
        shas_by_action[action].add(sha)
        sites_by_action_sha[(action, sha)].append((path, line))

    found: list[Violation] = []
    for path, line, action, sha, opted in all_records:
        if opted:
            continue
        shas = shas_by_action[action]
        if len(shas) > 1:
            elsewhere = sorted(
                f"{other_path}:{other_line} ({other_sha})"
                for other_sha in shas - {sha}
                for other_path, other_line in sites_by_action_sha[(action, other_sha)]
            )
            found.append(
                Violation(
                    path,
                    line,
                    f"divergent pin: `{action}` is pinned to `{sha}` here, and "
                    f"differently at {elsewhere} — converge every call site on "
                    f"one SHA (or annotate `# {OPT_OUT}: <reason>` on every "
                    "occurrence if the split is deliberate).",
                )
            )
    return parse_errors + found


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    texts = [
        (str(path.relative_to(REPO_ROOT)), path.read_text(encoding="utf-8"))
        for path in workflow_files()
    ]
    violations = check_files(texts)
    for path, line, message in violations:
        print(f"::error file={path},line={line}::{message}")
    if violations:
        print(f"\nERROR: {len(violations)} divergent action pin(s) found.")
        print(
            "Pin each action to ONE SHA repo-wide, and update every call site "
            "together — a divergent pin means a bump missed one of them."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

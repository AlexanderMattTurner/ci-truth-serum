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

Opt out with `# divergent-pin-ok` on the line.
"""

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_pin_comment_truth import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _USES_SHA,
    ACTIONS_DIR,
    REPO_ROOT,
    WORKFLOWS_DIR,
)

OPT_OUT = "divergent-pin-ok"


def pin_records(text: str) -> list[tuple[int, str, str, bool]]:
    """(1-based line, action, sha, opted_out) for every SHA-pinned `uses:` line
    in TEXT — the same match `check_pin_comment_truth` reads its lines with."""
    records: list[tuple[int, str, str, bool]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _USES_SHA.match(line)
        if not m:
            continue
        records.append(
            (
                lineno,
                m.group("ref"),
                m.group("sha"),
                annotated(m.group("rest"), OPT_OUT, require_reason=False),
            )
        )
    return records


def check_files(texts: list[tuple[str, str]]) -> list[tuple[str, int, str]]:
    """(path, line, message) for every divergent pin across TEXTS ((path,
    content) pairs) — divergence is a property of the whole tree, so every
    reference must be compared against every other one at once."""
    all_records: list[tuple[str, int, str, str, bool]] = []
    for path, text in texts:
        all_records += [
            (path, line, action, sha, opted)
            for line, action, sha, opted in pin_records(text)
        ]

    shas_by_action: dict[str, set[str]] = defaultdict(set)
    for _path, _line, action, sha, opted in all_records:
        if not opted:
            shas_by_action[action].add(sha)

    found: list[tuple[str, int, str]] = []
    for path, line, action, sha, opted in all_records:
        if opted:
            continue
        shas = shas_by_action[action]
        if len(shas) > 1:
            others = sorted(shas - {sha})
            found.append(
                (
                    path,
                    line,
                    f"divergent pin: `{action}` is pinned to `{sha}` here, and to "
                    f"{others} elsewhere — converge every call site on one SHA "
                    f"(or annotate `# {OPT_OUT}` if the split is deliberate).",
                )
            )
    return found


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

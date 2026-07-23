#!/usr/bin/env python3
"""Keep the human-readable `# vX.Y` comment on SHA-pinned `uses:` lines honest.

A SHA pin (`uses: actions/checkout@9c091bb…`) is unreadable without its version
comment — the comment IS the documentation reviewers trust. But nothing checks
it: a bump that edits twelve lines and misses the thirteenth leaves the same
SHA annotated `# v6` on one line and `# v7.0.0` on the others (a real
incident), and the next reader has no idea which is true.

Offline rules (no registry/network resolution — detection stays offline like
the rest of the pack; whether the comment matches the SHA's REAL tag is a
review-time question this lint makes answerable by making the comments
consistent):

  (a) a SHA-pinned `uses:` with no version comment at all is flagged;
  (b) the same `owner/repo@sha` carrying two different comment strings
      anywhere under `.github/` is flagged at every occurrence;
  (c) a comment not matching `# v<digits>[.<digits>[.<digits>]]` (trailing
      text after the version is fine) is flagged.

Opt out with `# pin-comment-ok` on the line (in addition to, or instead of, a
version comment). Globs every workflow like the other workflow lints; the
passed file list is ignored.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

OPT_OUT = "pin-comment-ok"

# A `uses:` line pinning to a 40-hex SHA, with whatever trails it. Anchored
# after optional block-sequence `- ` so a commented-out `# uses:` never matches.
_USES_SHA = re.compile(
    r"^\s*-?\s*uses:\s*(?P<ref>[\w.-]+/[\w./-]+)@(?P<sha>[0-9a-f]{40})\b(?P<rest>.*)$"
)
# The version comment: `# v1`, `# v1.2`, `# v1.2.3`, with optional trailing
# text (`# v6.0.2 # zizmor: ignore[...]` is fine).
_VERSION_COMMENT = re.compile(r"#\s*(?P<version>v\d+(?:\.\d+){0,2})(?:\s|$|#)")
_ANY_COMMENT = re.compile(r"#\s*(?P<text>.*)$")


def pin_records(text: str) -> list[tuple[int, str, str | None, bool]]:
    """(1-based line, `owner/repo@sha`, version-comment-or-None, opted_out) for
    every SHA-pinned `uses:` line in TEXT. The version comment is the bare
    `vX[.Y[.Z]]` token; None when the trailing text carries no wellformed one
    (including when there is no comment at all)."""
    records: list[tuple[int, str, str | None, bool]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        m = _USES_SHA.match(line)
        if not m:
            continue
        rest = m.group("rest")
        version = _VERSION_COMMENT.search(rest)
        records.append(
            (
                lineno,
                f"{m.group('ref')}@{m.group('sha')}",
                version.group("version") if version else None,
                annotated(rest, OPT_OUT, require_reason=False),
            )
        )
    return records


def check_files(
    texts: list[tuple[str, str]],
) -> list[tuple[str, int, str]]:
    """(path, line, message) for every pin-comment violation across TEXTS
    ((path, content) pairs) — the cross-file consistency rule needs the whole
    set at once."""
    all_records: list[tuple[str, int, str, str | None, bool]] = []
    for path, text in texts:
        all_records += [
            (path, line, pin, version, opted)
            for line, pin, version, opted in pin_records(text)
        ]

    # Which comment strings does each pin carry, over non-opted-out lines?
    comments_by_pin: dict[str, set[str]] = defaultdict(set)
    for _path, _line, pin, version, opted in all_records:
        if not opted and version is not None:
            comments_by_pin[pin].add(version)

    found: list[tuple[str, int, str]] = []
    for path, line, pin, version, opted in all_records:
        if opted:
            continue
        if version is None:
            found.append(
                (
                    path,
                    line,
                    f"SHA-pinned `{pin.split('@')[0]}` has no wellformed version "
                    "comment — the SHA is unreadable without one. Append "
                    "`# v<major>[.<minor>[.<patch>]]` (or `# pin-comment-ok`).",
                )
            )
            continue
        versions = comments_by_pin[pin]
        if len(versions) > 1:
            found.append(
                (
                    path,
                    line,
                    f"`{pin}` carries conflicting version comments across the repo: "
                    f"{sorted(versions)} — at most one can be true. Unify them "
                    "(same SHA = same version).",
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
        print(f"\nERROR: {len(violations)} pin-comment violation(s) found.")
        print(
            "The `# vX.Y` comment is the only human-readable part of a SHA pin; "
            "a missing or contradictory one leaves reviewers guessing."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

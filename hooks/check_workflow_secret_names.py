#!/usr/bin/env python3
"""Round-trip the secret/var names workflows reference against a checked-in
allowlist, so a typo'd or renamed secret can't silently degrade a workflow.

`secrets.WRONG_NAME` is not an error on GitHub's side — the expression just
evaluates empty, and whatever the secret fed (an API call, a changelog
drafter, a release token) silently degrades. Real incidents: a workflow read
`secrets.ANTHROPIC_API_KEY` while the configured secret was
`GH_ACTION_ANTHROPIC_API_KEY`, and changelog drafting silently fell back to a
plain commit list for a week; three renames of a release token each surfaced
only at runtime.

The contract: every `secrets.<NAME>` / `vars.<NAME>` referenced anywhere under
`.github/workflows/` and `.github/actions/` must appear in
`.github/workflow-secrets.txt` (one name per line, `#` comments allowed,
sorted; a trailing comment may note repo/org scope), and every listed name
must still be referenced — both directions, so the allowlist is a reviewed
mirror of reality, not a graveyard. `GITHUB_TOKEN` (and the `github.token`
context) is GitHub-provided, so it is always implicitly allowed and never
listed. On any mismatch the corrected file content is printed, copy-paste
ready. Globs every workflow like the other workflow lints; the passed file
list is ignored.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOWLIST = Path(".github") / "workflow-secrets.txt"

# GitHub provisions this one itself; referencing it needs no configuration, so
# it is implicitly allowed (as is the equivalent `github.token` context, which
# the extraction regex never matches anyway).
IMPLICIT = frozenset({"GITHUB_TOKEN"})

# A `secrets.NAME` / `vars.NAME` context reference. The leading boundary rejects
# a longer path (`foo.secrets.X` cannot occur in workflow expressions anyway);
# names follow GitHub's secret-name grammar (alnum + underscore, not leading
# digit).
_REF = re.compile(r"\b(?:secrets|vars)\.(?P<name>[A-Za-z_][A-Za-z0-9_]*)")


def referenced_names(text: str) -> set[str]:
    """Every secret/var name TEXT references, minus the implicit set."""
    return {m.group("name") for m in _REF.finditer(text)} - IMPLICIT


def parse_allowlist(text: str) -> set[str]:
    """Names listed in an allowlist file: one per line, `#` starts a comment
    (full-line or trailing), blank lines ignored."""
    names: set[str] = set()
    for raw in text.splitlines():
        entry = raw.split("#", 1)[0].strip()
        if entry:
            names.add(entry)
    return names


def corrected_content(referenced: set[str]) -> str:
    """The exact allowlist file content matching REFERENCED, sorted and
    copy-paste ready."""
    header = (
        "# Secret/var names referenced by .github/workflows and .github/actions.\n"
        "# Kept in sync by check-workflow-secret-names; GITHUB_TOKEN is implicit.\n"
    )
    return header + "".join(f"{name}\n" for name in sorted(referenced))


def check_repo(referenced: set[str], allowlist_text: str | None) -> list[str]:
    """Every round-trip violation, as printable messages. ALLOWLIST_TEXT is the
    allowlist file's content, or None when the file does not exist."""
    if allowlist_text is None:
        if not referenced:
            return []
        return [
            f"::error file={ALLOWLIST}::workflows reference secrets/vars but "
            f"{ALLOWLIST} does not exist. Create it with exactly this content:\n"
            f"{corrected_content(referenced)}"
        ]

    listed = parse_allowlist(allowlist_text)
    unlisted = sorted(referenced - listed)
    stale = sorted(listed - referenced)
    if not unlisted and not stale:
        return []
    detail = "; ".join(
        part
        for part in (
            f"referenced but not listed (typo or unreviewed addition): {unlisted}"
            if unlisted
            else "",
            f"listed but no longer referenced (stale): {stale}" if stale else "",
        )
        if part
    )
    return [
        f"::error file={ALLOWLIST}::secret/var allowlist is out of sync with the "
        f"workflows — {detail}. Replace the file content with:\n"
        f"{corrected_content(referenced)}"
    ]


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    referenced: set[str] = set()
    for path in workflow_files():
        referenced |= referenced_names(path.read_text(encoding="utf-8"))
    allowlist_path = REPO_ROOT / ALLOWLIST
    allowlist_text = (
        allowlist_path.read_text(encoding="utf-8") if allowlist_path.exists() else None
    )
    violations = check_repo(referenced, allowlist_text)
    for message in violations:
        print(message)
    if violations:
        print(f"\nERROR: {len(violations)} secret-name violation(s) found.")
        print(
            "A misspelled secret evaluates to empty and the workflow silently "
            "degrades; the allowlist makes every name a reviewed choice."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Ban the `${{ secrets.A || secrets.B }}` fallback idiom in token positions.

A token fallback reads as harmless plumbing, but it makes the workflow's push
identity a function of which secrets happen to exist: the day someone sets the
first secret, every push/tag/API call silently switches accounts. Real
incident: a cross-account PAT landed in the first slot, a tag push started
403'ing, and a retrying version-bump loop walked an npm package from 1.x to
5.x before anyone noticed.

Flagged: any `||` between two `secrets.*` references on a line whose YAML key
is a token position — a `token:`/`github-token:`/`github_token:` input, or an
env var named `GITHUB_TOKEN`/`GH_TOKEN`. A fallback elsewhere (say a notify
URL) is not an identity switch and is left alone.

Opt out with `# token-fallback-ok: <reason>` trailing the line (or on the line
immediately above) when the identity switch is the designed behaviour.

Globs every workflow like the other workflow lints; the passed file list is
ignored.
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

OPT_OUT = "token-fallback-ok"

# A token-position key: the `token`-family action inputs, or the two env names
# gh/git tooling reads. Anchored as a YAML `key:` so `my-token-helper:` or a
# value mentioning "token" never matches.
_TOKEN_KEY = re.compile(
    r"^\s*(?:token|github[-_]token|GITHUB_TOKEN|GH_TOKEN)\s*:", re.IGNORECASE
)
# `secrets.A || secrets.B` (any spacing) inside the line's value — the fallback
# idiom itself. Only secret-to-secret fallbacks are an identity switch; a
# fallback to a literal or to github.token is a different (visible) choice.
_FALLBACK = re.compile(r"\bsecrets\.[A-Za-z_][\w]*\s*\|\|\s*secrets\.[A-Za-z_][\w]*")

MESSAGE = (
    "token fallback `secrets.A || secrets.B`: the push identity silently "
    "switches accounts the day the first secret is set (a cross-account PAT "
    "here 403'd tag pushes and a version-bump loop walked npm from 1.x to 5.x). "
    "Pin ONE secret, or annotate `# token-fallback-ok: <reason>` if the "
    f"switch is designed. (opt-out: # {OPT_OUT}: <reason>)"
)


def violations(text: str) -> list[int]:
    """1-based line numbers carrying a secrets-to-secrets fallback in a token
    position, minus opted-out lines."""
    lines = text.splitlines()
    hits: list[int] = []
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            continue
        if not (_TOKEN_KEY.match(line) and _FALLBACK.search(line)):
            continue
        if OPT_OUT in line or (idx >= 1 and OPT_OUT in lines[idx - 1]):
            continue
        hits.append(idx + 1)
    return hits


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line in violations(path.read_text(encoding="utf-8")):
            print(f"::error file={rel},line={line}::{MESSAGE}")
            total += 1
    if total:
        print(f"\nERROR: {total} token-fallback violation(s) found.")
        print(
            "A `secrets.A || secrets.B` fallback in a token position changes "
            "which account pushes the moment the first secret appears."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

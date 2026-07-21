#!/usr/bin/env python3
"""Verify that two files which pin the same thing actually agree — the
config-driven replacement for "keep in lockstep" comments.

The motivating twin pin: a repo's `.pre-commit-config.yaml` names a
ci-truth-serum `rev:` while a workflow `pip install git+…@<sha>` pins the same
release — hand-bumped three times in one week, each time with a "keep these in
lockstep" note that enforced nothing. This hook IS the enforcement: each
configured pair extracts one value from each file and fails unless they are
equal.

Config-driven — the hook does nothing without arguments (and says so loudly):

    - id: check-lockstep-pins
      args:
        - --pair
        - .pre-commit-config.yaml
        - 'ci-truth-serum\\n\\s*rev:\\s*(\\S+)'
        - .github/workflows/ci.yaml
        - 'ci-truth-serum@(\\S+)'

Each regex (searched with re.DOTALL disabled, re.MULTILINE enabled) must carry
exactly ONE capture group and match its file EXACTLY once — zero or multiple
matches is a hard error, not a pass: a pattern that stops matching is a rotted
config, and silence would be the exact vice this pack exists to catch.
"""

import argparse
import re
import sys
from pathlib import Path


def check_pair(
    file1: str, text1: str, regex1: str, file2: str, text2: str, regex2: str
) -> list[str]:
    """Every lockstep violation for one configured pair, as printable messages.
    A config error (bad group count, not-exactly-one match) is a violation too
    — fail loud, never silently pass a rotted pattern."""

    def extract(file: str, text: str, pattern: str) -> tuple[str | None, str | None]:
        try:
            rx = re.compile(pattern, re.MULTILINE)
        except re.error as err:
            return None, f"--pair regex for {file} does not compile: {err}"
        if rx.groups != 1:
            return None, (
                f"--pair regex for {file} has {rx.groups} capture groups; exactly "
                "one is required (it selects the pinned value)."
            )
        matches = rx.findall(text)
        if len(matches) != 1:
            return None, (
                f"--pair regex for {file} matched {len(matches)} times; exactly one "
                "match is required — a pattern matching zero (rotted) or several "
                "(ambiguous) pins cannot prove lockstep."
            )
        return matches[0], None

    value1, err1 = extract(file1, text1, regex1)
    value2, err2 = extract(file2, text2, regex2)
    errors = [f"::error file={f}::{e}" for f, e in ((file1, err1), (file2, err2)) if e]
    if errors:
        return errors
    if value1 != value2:
        return [
            f"::error file={file2}::lockstep pin mismatch: {file1} pins `{value1}` "
            f"but {file2} pins `{value2}`. Bump both in the same commit — that is "
            "the whole contract this pair encodes."
        ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pair",
        nargs=4,
        action="append",
        metavar=("FILE1", "REGEX1", "FILE2", "REGEX2"),
        help="two files and one single-group regex each; the captures must match",
    )
    args = parser.parse_args(argv)
    if not args.pair:
        print(
            "::error::check-lockstep-pins has no --pair configured — it verifies "
            "nothing. Add at least one `--pair FILE1 REGEX1 FILE2 REGEX2` to the "
            "hook's args (see README), or remove the hook.",
            file=sys.stderr,
        )
        return 2

    violations: list[str] = []
    for file1, regex1, file2, regex2 in args.pair:
        texts: list[str] = []
        for file in (file1, file2):
            path = Path(file)
            if not path.exists():
                violations.append(
                    f"::error file={file}::--pair names a file that does not exist."
                )
                break
            texts.append(path.read_text(encoding="utf-8"))
        else:
            violations += check_pair(file1, texts[0], regex1, file2, texts[1], regex2)

    for message in violations:
        print(message)
    if violations:
        print(f"\nERROR: {len(violations)} lockstep-pin violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

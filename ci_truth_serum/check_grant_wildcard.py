#!/usr/bin/env python3
"""
Ban a Claude Code `permissions.allow` shell rule whose `*` extends a word.

In a `Bash(...)` rule, a `*` matches any text, and the text before it matches as
written. The space before a trailing `*` is part of the rule. So `Bash(ls *)`
matches `ls -la` and `ls` but not `lsof`, and `Bash(ls*)` matches `lsof` too.
`PowerShell(...)` rules use the same shape. Source:
https://code.claude.com/docs/en/permissions, section "Wildcard patterns".

An allow rule runs a command with no prompt. So `Bash(git diff*)` also approves
`git difftool`, which starts a program that git config names.

The rule: the character just before a `*` must not be a letter or a digit. A
wildcard that starts at a space, a `:` or a punctuation mark passes, such as
`Bash(git diff *)`, `Bash(npm run test:*)` and `Bash(python *.py)`. The remedy is
the space: `Bash(git diff *)` also matches the bare `git diff`.

Only `permissions.allow` is read: a wide `deny` or `ask` rule asks for more, not
less. File-tool rules belong to `check_unscoped_tool_grant`. There is no opt-out,
because a settings file is JSON and has no comments.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_claude_settings import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Finding,
    line_of,
    run,
)

# The tools whose rule argument is a command line with `*` wildcards.
SHELL_TOOLS = ("Bash", "PowerShell")

_SUMMARY = (
    "A `*` right after a letter or digit extends that word, so the rule approves "
    "every longer command that shares the prefix. Put a space before the `*`."
)


def command_pattern(rule: str) -> str | None:
    """The command pattern inside a shell-tool RULE, or None for any other rule."""
    for tool in SHELL_TOOLS:
        if rule.startswith(f"{tool}(") and rule.endswith(")"):
            return rule[len(tool) + 1 : -1]
    return None


def extends_a_word(pattern: str) -> bool:
    """True when a `*` in PATTERN comes right after a letter or a digit."""
    return any(
        char == "*" and index > 0 and pattern[index - 1].isalnum()
        for index, char in enumerate(pattern)
    )


def findings(doc: dict, text: str) -> list[Finding]:
    """Every `permissions.allow` shell rule in settings DOC whose `*` extends a word.

    DOC comes from `_cts_claude_settings.decode(TEXT)`, so each rule string carries
    the offset that gives its line. A missing or malformed `allow` list holds no
    rule, so it gives no finding.
    """
    permissions = doc.get("permissions")
    allow = permissions.get("allow") if isinstance(permissions, dict) else None
    if not isinstance(allow, list):
        return []
    out: list[Finding] = []
    for rule in allow:
        pattern = command_pattern(rule) if isinstance(rule, str) else None
        if pattern is None or not extends_a_word(pattern):
            continue
        message = (
            f"the allow rule `{rule}` has a `*` right after a letter or digit, so "
            "it also approves every longer command that shares the prefix "
            "(`Bash(git diff*)` approves `git difftool`). Put a space before the "
            "`*`: `Bash(git diff *)` matches `git diff` and `git diff HEAD`, and "
            "not `git difftool`."
        )
        out.append((line_of(text, rule.offset), message))
    return out


def main(argv: list[str]) -> None:
    run(argv, __doc__, findings, _SUMMARY)


if __name__ == "__main__":
    main(sys.argv[1:])

#!/usr/bin/env python3
"""
Require an explicit positive numeric `timeout` on every Claude Code PreToolUse hook entry.

A PreToolUse hook is the only hook that can block a tool call before it runs.
Claude Code cancels a hook that reaches its timeout and discards its output. The
hooks reference says a timed-out `command`, `http` or `mcp_tool` hook does not
block the tool call: the call continues through the normal permission flow.
Source: https://code.claude.com/docs/en/hooks, sections "Timeouts" and "Common
fields". So a gate that runs past its bound fails OPEN.

An entry with no `timeout` gets a default that depends on the hook type and the
event, and the author never chose it. This check makes the bound a value that
somebody wrote and a reviewer can see. It checks every entry under
`hooks.PreToolUse[].hooks[]`, whatever its `type`. It skips an `async: true`
entry, because Claude Code does not enforce a timeout on one.

There is no opt-out. A settings file is JSON, which has no comments, and every
entry can state a timeout.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_claude_settings import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Finding,
    line_of,
    run,
)

EVENT = "PreToolUse"

_SUMMARY = (
    "A PreToolUse hook that reaches its timeout does not block the tool call, so "
    "give each one an explicit timeout sized to its slowest honest run."
)


def _label(entry: dict) -> str:
    """The text that names ENTRY in a message: its command, prompt or URL."""
    for key in ("command", "prompt", "url", "tool"):
        if key in entry:
            return f"{key}={str(entry[key])[:60]!r}"
    return "no command"


def _has_timeout(entry: dict) -> bool:
    """True when ENTRY states a POSITIVE numeric `timeout`.

    A JSON boolean is not a number, and a non-positive bound is no bound.
    """
    timeout = entry.get("timeout")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        return False
    return timeout > 0


def findings(doc: dict, text: str) -> list[Finding]:
    """Every PreToolUse hook entry in settings DOC with no positive numeric `timeout`.

    DOC comes from `_cts_claude_settings.decode(TEXT)`, so each entry carries the
    offset that gives its line. A shape that is not a list or an object holds no
    hook entry, so it is skipped. Indices count those skipped elements too, so a
    locator names the entry as it appears in the file.
    """
    hooks = doc.get("hooks")
    groups = hooks.get(EVENT) if isinstance(hooks, dict) else None
    if not isinstance(groups, list):
        return []
    out: list[Finding] = []
    for i, group in enumerate(groups):
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            continue
        for j, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("async") is True:
                continue
            if _has_timeout(entry):
                continue
            message = (
                f"hooks.{EVENT}[{i}].hooks[{j}] ({_label(entry)}) has no positive "
                'numeric "timeout". A PreToolUse hook that reaches its timeout does not '
                "block the tool call, so the gate fails open at a bound nobody "
                'chose. Add "timeout": <seconds>, sized to the slowest honest run '
                "of the hook."
            )
            out.append((line_of(text, entry.offset), message))
    return out


def main(argv: list[str]) -> None:
    run(argv, __doc__, findings, _SUMMARY)


if __name__ == "__main__":
    main(sys.argv[1:])

"""Read Claude Code settings files and report findings on the line that holds them.

The stdlib JSON decoder discards positions, so a finding could name no line.
`decode` runs that same decoder's pure-Python scanner with two hooks. Each object
comes back as a `LocatedDict` and each string value as a `LocatedStr`. Both carry
the offset where the value starts. The values are the ones `json.loads` returns,
so a duplicate key resolves the same way it does for Claude Code.

`run` is the command-line driver both settings checks share. With no file
argument it reads the project settings files Claude Code loads from the current
directory, and it says so on stderr when none exists.
"""

import argparse
import json
import sys
from collections.abc import Callable
from json.decoder import JSONObject, scanstring
from json.scanner import py_make_scanner
from pathlib import Path
from typing import Any

# The project-scoped settings files, per https://code.claude.com/docs/en/hooks
# ("Hook locations"). User and managed settings live outside the repository.
DEFAULT_SETTINGS = (".claude/settings.json", ".claude/settings.local.json")


class LocatedDict(dict):
    """A decoded JSON object, plus the offset of its opening brace."""

    offset: int


class LocatedStr(str):
    """A decoded JSON string value, plus the offset of its opening quote."""

    offset: int


class _LocatingDecoder(json.JSONDecoder):
    """`json.JSONDecoder`, with the offset of each object and string value kept.

    The C scanner ignores a Python `parse_object`, so this one uses the stdlib's
    pure-Python scanner instead. Object keys go through `scanstring` directly and
    stay plain strings.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parse_object = self._parse_object
        self.parse_string = self._parse_string
        self.scan_once = py_make_scanner(self)

    @staticmethod
    def _parse_object(s_and_end: tuple[str, int], *args: Any) -> tuple[Any, int]:
        obj, end = JSONObject(s_and_end, *args)
        located = LocatedDict(obj)
        located.offset = s_and_end[1] - 1
        return located, end

    def _parse_string(self, text: str, end: int, strict: bool) -> tuple[Any, int]:
        value, after = scanstring(text, end, strict)
        located = LocatedStr(value)
        located.offset = end - 1
        return located, after


def decode(text: str) -> Any:
    """Decode TEXT as JSON; objects and string values carry their offsets.

    Malformed JSON raises `json.JSONDecodeError`, as `json.loads` does.
    """
    return _LocatingDecoder().decode(text)


def line_of(text: str, offset: int) -> int:
    """The 1-based line of TEXT that holds character OFFSET."""
    return text.count("\n", 0, offset) + 1


# One finding: the 1-based line, and the message for that line.
Finding = tuple[int, str]


def settings_paths(files: list[str]) -> list[Path]:
    """FILES as paths, or the default settings files that exist when FILES is empty.

    An empty result prints a notice: a clean exit over no file is otherwise the
    same as a real pass.
    """
    if files:
        return [Path(f) for f in files]
    found = [Path(f) for f in DEFAULT_SETTINGS if Path(f).is_file()]
    if not found:
        print(
            f"note: no Claude Code settings file ({', '.join(DEFAULT_SETTINGS)}) "
            "in this directory — this check scanned nothing.",
            file=sys.stderr,
        )
    return found


def run(
    argv: list[str],
    description: str,
    findings: Callable[[dict, str], list[Finding]],
    summary: str,
) -> None:
    """Run FINDINGS over each settings file in ARGV and exit 1 when any fires.

    FINDINGS gets the decoded document and its text. Each finding prints as a
    GitHub `::error` annotation, and SUMMARY prints once after them. A file whose
    top level is not a JSON object is not a settings file, so it raises.
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "files",
        nargs="*",
        help=f"settings files to check (default: {' and '.join(DEFAULT_SETTINGS)})",
    )
    args = parser.parse_args(argv)
    total = 0
    for path in settings_paths(args.files):
        text = path.read_text(encoding="utf-8")
        doc = decode(text)
        if not isinstance(doc, dict):
            raise ValueError(f"{path}: the top level is not a JSON object")
        for line, message in findings(doc, text):
            print(f"::error file={path},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} violation(s) found.\n{summary}")
        raise SystemExit(1)

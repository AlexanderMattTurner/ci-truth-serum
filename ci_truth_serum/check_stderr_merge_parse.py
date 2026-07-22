#!/usr/bin/env python3
"""Flag `2>&1`-merged output that is then PARSED — stderr noise becomes data.

Merging stderr into a captured stream is fine for diagnostics ("show me
everything the command said"). It is a bug the moment the merged stream is fed
to a parser: any warning the tool prints becomes part of "the value". Real
incident: an npm stderr warning merged via `2>&1` became "the version", and
every release aborted on the nonsense comparison that followed.

Flagged (precision over recall — plain diagnostic captures must never fire):

  (a) a command substitution that merges (`2>&1`) and pipes the merged stream
      into a parsing command INSIDE the substitution
      (`v=$(cmd 2>&1 | tail -1)`);
  (b) `var=$(cmd 2>&1)` where, within the next 10 lines, `$var` is piped into
      a parsing command or used in a `[[ … ]]` / `(( … ))` comparison.

Parsing commands: head, tail, grep, awk, cut, sed, jq, sort, wc. NOT flagged:
`var=$(cmd 2>&1)` followed only by echo/printf/logging — capture-for-
diagnostics is the dominant legitimate use. Opt out with a
`# stderr-merge-ok: <reason>` comment on the flagged line, the line above, or
(for rule b) the capturing assignment.

Invoked by pre-commit with the staged shell files as arguments; a
`.github/{workflows,actions}` YAML path among them has each inline `run:`
block scanned instead (reported at the step's line).
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    logical_lines,
)

OPT_OUT = "stderr-merge-ok"

_PARSERS = r"(?:head|tail|grep|awk|cut|sed|jq|sort|wc)"
_MERGE = "2>&1"
# `| parser` — a single pipe (not `||`) into a parsing command.
_PIPE_TO_PARSER = re.compile(rf"(?<!\|)\|(?!\|)\s*{_PARSERS}\b")
# `var=$(…` — a capture assignment whose right-hand side opens a substitution.
_ASSIGN_OPEN = re.compile(
    r"^\s*(?:local\s+|export\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?"
    r"(?P<var>\w+)=[\"']?\$\("
)
# Comparison operators inside `[[ … ]]` that treat the value as data to rank —
# `-z`/`-n` (emptiness) and `-f`-style file tests are diagnostics, not parsing.
_COMPARE_OP = re.compile(r"(?:-eq|-ne|-lt|-le|-gt|-ge|=~|==|!=|<|>)")
_TEST_BRACKET = re.compile(r"\[\[(?P<body>.*?)\]\]")
_ARITH = re.compile(r"\(\((?P<body>.*?)\)\)")

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

MESSAGE_INLINE = (
    "`2>&1` merges stderr into a stream that is then piped into a parser — any "
    'warning on stderr becomes part of "the value" (an npm warning merged this '
    'way became "the version" and aborted every release). Parse stdout only, '
    f"or annotate `# {OPT_OUT}: <reason>`."
)
MESSAGE_LATER = (
    "this line parses/compares a variable captured with `2>&1` — any warning on "
    "stderr became part of the value. Capture stdout only (keep stderr for "
    f"diagnostics), or annotate `# {OPT_OUT}: <reason>`."
)


def _substitution_spans(line: str) -> list[str]:
    """The content of every ``$(…)`` span in LINE (nested spans included in
    their parent's content, and reported on their own too)."""
    spans: list[str] = []
    starts: list[int] = []
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line.startswith("$(", i):
            starts.append(i + 2)
            i += 2
            continue
        if line[i] == ")" and starts:
            spans.append(line[starts.pop() : i])
        i += 1
    return spans


def _merged_then_parsed(span: str) -> bool:
    """True when SPAN merges stderr and later pipes the merged stream into a
    parsing command."""
    idx = span.find(_MERGE)
    return idx >= 0 and bool(_PIPE_TO_PARSER.search(span, idx + len(_MERGE)))


def _is_compared(logical: str, var: str) -> bool:
    """True when LOGICAL uses $VAR inside a `[[ … ]]` comparison or `(( … ))`
    arithmetic — treating the captured stream as an orderable value."""
    ref = re.compile(rf"\$\{{?{re.escape(var)}\b")
    for m in _TEST_BRACKET.finditer(logical):
        body = m.group("body")
        if ref.search(body) and _COMPARE_OP.search(body):
            return True
    return any(
        re.search(rf"\b{re.escape(var)}\b", m.group("body"))
        for m in _ARITH.finditer(logical)
    )


def _is_piped_to_parser(logical: str, var: str) -> bool:
    """True when LOGICAL pipes a command line containing $VAR into a parser."""
    ref = re.compile(rf"\$\{{?{re.escape(var)}\b")
    m = ref.search(logical)
    return bool(m and _PIPE_TO_PARSER.search(logical, m.end()))


def _opted_out(physical: list[str], start: int, logical: str) -> bool:
    """Opt-out marker on the logical line itself or the physical line above it."""
    return OPT_OUT in logical or (start >= 2 and OPT_OUT in physical[start - 2])


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every merged-then-parsed capture in TEXT."""
    physical = text.splitlines()
    found: list[tuple[int, str]] = []
    # (var, assignment start line, lines remaining) for rule (b) tracking.
    tracked: list[tuple[str, int]] = []
    for start, logical in logical_lines(text):
        stripped = logical.lstrip()
        if stripped.startswith("#"):
            continue
        opted = _opted_out(physical, start, logical)

        # Rule (a): merged and parsed inside one substitution.
        if any(_merged_then_parsed(span) for span in _substitution_spans(logical)):
            if not opted:
                found.append((start, MESSAGE_INLINE))
            continue

        # Rule (b) — uses of previously tracked merged captures.
        for var, assigned_at in tracked:
            if start - assigned_at > 10:
                continue
            if not (_is_piped_to_parser(logical, var) or _is_compared(logical, var)):
                continue
            if not opted and OPT_OUT not in physical[assigned_at - 1]:
                found.append((start, MESSAGE_LATER))
        tracked = [(v, at) for v, at in tracked if start - at <= 10]

        # Any reassignment supersedes the tracked capture — the old merged
        # value is gone, so later uses read the NEW value.
        reassigned = re.match(
            r"^\s*(?:local\s+|export\s+|readonly\s+|declare\s+(?:-\w+\s+)*)?(?P<var>\w+)=",
            logical,
        )
        if reassigned:
            tracked = [(v, at) for v, at in tracked if v != reassigned.group("var")]

        # Rule (b) — start tracking a merged capture with no inline parse.
        assign = _ASSIGN_OPEN.match(logical)
        if assign and any(_MERGE in span for span in _substitution_spans(logical)):
            tracked.append((assign.group("var"), start))
    return found


def _run_scripts(path: Path) -> list[tuple[int, str]]:
    """(step line, script) for every inline `run:` block in a workflow or
    composite-action file. An unparseable file yields no scripts — YAML syntax
    is actionlint's job, and the shell files this lint owns are its argv."""
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    scripts: list[tuple[int, str]] = []
    containers = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        containers += [j for j in jobs.values() if isinstance(j, dict)]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        containers.append(runs)
    for container in containers:
        steps = container.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                scripts.append((step.get("__line__", 1), step["run"]))
    return scripts


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = 0
    for arg in argv:
        path = Path(arg)
        try:
            if _WORKFLOW_PATH.search(arg.replace("\\", "/")):
                hits = [
                    (step_line, message)
                    for step_line, script in _run_scripts(path)
                    for _line, message in violations(script)
                ]
            elif arg.endswith((".yaml", ".yml")):
                continue  # a non-workflow YAML file is not shell
            else:
                hits = violations(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for lineno, message in hits:
            print(f"{arg}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Report a local composite action or reusable workflow that no `uses:` reaches.

PROBLEM CLASS — every reference check in this pack reads the FORWARD edge: the
`uses:` names a file, and the file is there. Nothing reads the REVERSE edge. A
definition whose last caller went away stays on disk, and no run ever reads it.
It rots the way a dead shell function rots (`check_dead_shell_functions`).
Its assumptions drift from the live code. It also tells the next author that a
code path exists. That author then edits it, or copies it, and neither act
changes what CI does.

Two definition kinds carry this defect, and one sweep answers both:

  * a composite action at `.github/actions/<name>/action.y(a)ml`. A workflow
    step or another action's step reaches it with `uses: ./.github/actions/<name>`;
  * a workflow whose ONLY trigger is `workflow_call`. A job reaches it with
    `uses: ./.github/workflows/<name>.y(a)ml`. Nothing else starts it: it has no
    schedule, no `workflow_dispatch`, and no event of its own. A workflow that
    also declares another trigger is reachable, so this check passes it.

Both sides are static YAML, so the check reads them. The reference side is
every `uses:` value in every workflow and every action of this tree. Three
places hold one: a job's own `uses:`, a job step's, and an action's
`runs.steps`. The third counts because a `uses: ./…` inside an action resolves
against the caller's workspace, which makes it a real edge.

Each value is read from the PARSED key, never from the file's text. The same
words inside a `run:` script, or inside a comment, therefore reach nothing.

Two references this check reads generously, because an undercount can only
miss a dead definition and can never invent one:

  * `owner/repo/.github/workflows/<name>.yaml@ref` — a repository may call its
    own reusable workflow by full path. Offline this cannot be told from a
    foreign repository of the same shape, so the path half counts as a
    reference either way.
  * a `uses:` built by a `${{ … }}` expression. It resolves to no path this
    check can read, so it marks nothing live, and the definition it may reach
    answers with the opt-out below.

A file the parser refuses is the one case that stops the sweep. Its `uses:`
values cannot be read, so every definition they reach would read as dead. The
check reports that file and withholds every unreferenced verdict on that run.
The run is still red, so nothing is greened over.

BLIND SPOT, and the direction it errs: a caller in ANOTHER repository is
invisible to this tree. A published composite action, and a reusable workflow a
sibling repository calls, both read as unreferenced here. Name that caller in a
`# unreferenced-ok: <reason>` comment. The reason is mandatory. The comment goes
on the definition's key line — an action's `name:`, a workflow's
`workflow_call:` or `on:` — or in the comment block directly above it.

Globs every workflow and action like the other workflow lints; the passed file
list is ignored.
"""

import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import SafeLoader  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader as _LineLoader,
    annotation_window,
    is_placeholder_reason,
    workflow_files,
    workflow_triggers,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

OPT_OUT = "unreferenced-ok"
# `# unreferenced-ok: <reason>` — per-definition suppression; reason mandatory.
# An annotation READER (it extracts the reason so a placeholder can be rejected),
# not a boolean opt-out predicate. The lead mirrors `_cts_linecheck.annotation_re`:
# the token may follow the `#` directly, or after same-line comment text whose
# last character cannot belong to a token, so a longer slug never satisfies it.
_OPT_OUT = re.compile(rf"#(?:[^\r\n]*[^\w\r\n-])?{OPT_OUT}\s*:\s*(?P<reason>[^\r\n]*)$")

# The YAML key LineLoader adds to every mapping. Never a job id or a trigger name.
LINE_KEY = "__line__"
# The trigger that makes a workflow callable, and nothing else.
CALL_TRIGGER = "workflow_call"

ACTION = "composite action"
REUSABLE = "reusable workflow"


def _drop_line_key(mapping: object) -> dict:
    """MAPPING without the line tag LineLoader adds, or {} when it is not one."""
    if not isinstance(mapping, dict):
        return {}
    return {k: v for k, v in mapping.items() if k != LINE_KEY}


def _steps(container: object) -> list[dict]:
    """The step mappings of a job or of an action's `runs:` block."""
    steps = container.get("steps") if isinstance(container, dict) else None
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def uses_values(doc: object) -> list[str]:
    """Every `uses:` value DOC writes, from any of the three places one can sit.

    A job's own `uses:` calls a reusable workflow. A job step's calls an action.
    An action's `runs.steps` calls another action, and that edge counts too: a
    `uses: ./…` inside an action resolves against the caller's workspace.
    """
    if not isinstance(doc, dict):
        return []
    values: list[object] = []
    for job in _drop_line_key(doc.get("jobs")).values():
        if not isinstance(job, dict):
            continue
        values.append(job.get("uses"))
        values += [step.get("uses") for step in _steps(job)]
    values += [step.get("uses") for step in _steps(doc.get("runs"))]
    return [value for value in values if isinstance(value, str)]


def referenced_path(uses: str) -> str | None:
    """The repo-relative path USES names, or None when it names none.

    A `./…` value names this tree directly. An `owner/repo/<path>@ref` value
    names the same shape in SOME repository, which offline cannot be told from
    this one, so its path half counts — see the module docstring. A value built
    by a `${{ … }}` expression resolves to nothing this check can read.
    """
    text = uses.strip()
    if "${{" in text:
        return None
    if text.startswith("./"):
        return text.removeprefix("./").rstrip("/")
    reference, separator, _ref = text.partition("@")
    parts = reference.split("/")
    # `owner/repo` alone, or `owner/action@ref`: neither names a path inside a
    # repository, so neither can name a definition here.
    if not separator or len(parts) <= 2:
        return None
    return "/".join(parts[2:]).rstrip("/")


def calls_only(doc: object) -> bool:
    """True when DOC's ONLY trigger is `workflow_call`.

    Read through `workflow_triggers`, which handles the boolean key PyYAML
    resolves `on:` to. All three shapes GitHub accepts are read: the mapping,
    the list `on: [workflow_call]`, and the scalar `on: workflow_call`.
    """
    triggers = workflow_triggers(doc)
    if isinstance(triggers, str):
        return triggers == CALL_TRIGGER
    if isinstance(triggers, list):
        return [str(item) for item in triggers] == [CALL_TRIGGER]
    if isinstance(triggers, dict):
        return set(_drop_line_key(triggers)) == {CALL_TRIGGER}
    return False


def key_lines(text: str, *names: str) -> list[int]:
    """The 1-based line of each key along the path NAMES, outermost first.

    The composed node tree answers this, never a line scan: a key is a key
    because the parser says so, and `on:` composes as the scalar `on` whatever
    tag YAML 1.1 resolves it to. A path that stops early returns the lines it
    did reach, so a caller can anchor on the outer key when the inner one is
    absent.
    """
    try:
        node = yaml.compose(text, Loader=SafeLoader)
    except yaml.YAMLError:
        return []
    lines: list[int] = []
    for name in names:
        if not isinstance(node, yaml.MappingNode):
            return lines
        pair = next(
            (
                (key, value)
                for key, value in node.value
                if isinstance(key, yaml.ScalarNode) and key.value == name
            ),
            None,
        )
        if pair is None:
            return lines
        lines.append(pair[0].start_mark.line + 1)
        node = pair[1]
    return lines


class Definition(NamedTuple):
    """One definition a `uses:` may reach: the file to report at, the path a
    reference must name, the lines its opt-out may annotate, and what it is."""

    path: Path
    identity: str
    anchors: list[int]
    kind: str


def definition(
    path: Path, doc: object, text: str, root: Path, actions_dir: Path
) -> Definition | None:
    """What PATH defines, or None when it defines nothing a `uses:` reaches.

    A file under ACTIONS_DIR IS an action, and a `uses:` names its DIRECTORY.
    The test is the location, never the basename: a workflow may itself be
    called `action.yaml`, and reading that name as an action would report the
    whole workflows directory as one.

    A workflow is a definition only when `workflow_call` is its sole trigger.
    Any other trigger starts it without a caller.
    """
    if path.is_relative_to(actions_dir):
        return Definition(
            path,
            path.parent.relative_to(root).as_posix(),
            key_lines(text, "name"),
            ACTION,
        )
    if not calls_only(doc):
        return None
    return Definition(
        path,
        path.relative_to(root).as_posix(),
        key_lines(text, "on", CALL_TRIGGER),
        REUSABLE,
    )


def marker_window(lines: list[str], anchors: list[int]) -> list[int]:
    """The 1-based lines a definition's opt-out may sit on.

    `annotation_window` owns the placement rule — the key's own line, the line
    above it, and the unbroken comment block above that. Both anchors of a
    workflow are asked, so a reason written above `on:` reads the same as one
    written above `workflow_call:`.

    A definition with no anchor falls back to line 1. Every check here owes an
    escape hatch, and a file whose keys the composed tree cannot name would
    otherwise have nowhere to write one.
    """
    return sorted(
        {n for anchor in anchors or [1] for n in annotation_window(lines, anchor)}
    )


def suppression(lines: list[str], anchors: list[int]) -> tuple[str | None, str | None]:
    """(reason, error) for the `# unreferenced-ok:` marker on this definition.

    Both are None when no marker is present. A marker stating no real reason
    yields an error instead of a reason, so it suppresses nothing.
    """
    details = []
    for number in marker_window(lines, anchors):
        match = _OPT_OUT.search(lines[number - 1])
        if not match:
            continue
        reason = match.group("reason").strip().lstrip("#").strip()
        if not is_placeholder_reason(reason):
            return reason, None
        details.append(f"states only {reason!r}" if reason else "carries no reason")
    if not details:
        return None, None
    return None, (
        f"`# {OPT_OUT}` {details[0]}. A suppression must name the caller outside "
        "this repository, or say when a caller will arrive — otherwise the "
        f"definition is dead and the marker only hides it (`# {OPT_OUT}: <reason>`)."
    )


def message(found: Definition) -> str:
    """Why this definition is a finding, and the three ways to answer it."""
    unreachable = (
        f"this file declares `{CALL_TRIGGER}` as its only trigger. A caller is "
        "the only thing that can start it. No job in this repository calls "
        f"`./{found.identity}`."
        if found.kind == REUSABLE
        else f"no workflow or action in this repository uses this {found.kind} "
        f"with `uses: ./{found.identity}`."
    )
    return (
        f"{unreachable} Nothing runs it. An unreferenced definition rots: its "
        "assumptions drift from the live code. It also tells the next author "
        "that a code path exists. Delete it, wire the caller that wants it, or "
        f"suppress with `# {OPT_OUT}: <reason>` naming the caller in another "
        "repository."
    )


class UnreferencedViolation(NamedTuple):
    """One reported line: the file, the line number (0 when none applies), and
    the message."""

    path: Path
    line: int
    message: str


def check_repo(workflows_dir: Path, actions_dir: Path) -> list[UnreferencedViolation]:
    """Every unreferenced definition under WORKFLOWS_DIR and ACTIONS_DIR.

    Two passes, because a definition's verdict depends on every other file: the
    first reads each file once, the second judges each definition against the
    union of what any file uses.
    """
    root = workflows_dir.parent.parent
    parsed: dict[Path, tuple[object, str]] = {}
    found: list[UnreferencedViolation] = []
    for path in workflow_files(workflows_dir, actions_dir):
        text = path.read_text(encoding="utf-8")
        try:
            parsed[path] = (yaml.load(text, Loader=_LineLoader), text)
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            found.append(
                UnreferencedViolation(
                    path,
                    0,
                    f"could not parse as YAML ({first_line}); this file's "
                    "`uses:` values cannot be read, so every definition they "
                    "reach would read as unreferenced. This run therefore "
                    "reports no unreferenced definition at all — fix the syntax "
                    "(or run actionlint) and re-check.",
                )
            )

    # A file whose `uses:` values could not be read leaves the reference set
    # incomplete, and every definition it reaches would then read as dead. One
    # syntax error would cascade into a finding per action it uses, so the
    # unreferenced verdicts are withheld until the sweep is whole. The run is
    # still red: the parse failures above are the findings.
    if found:
        return found

    referenced = {
        path
        for doc, _text in parsed.values()
        for uses in uses_values(doc)
        if (path := referenced_path(uses)) is not None
    }

    for path, (doc, text) in sorted(parsed.items()):
        entry = definition(path, doc, text, root, actions_dir)
        if entry is None or entry.identity in referenced:
            continue
        lines = text.splitlines()
        reason, error = suppression(lines, entry.anchors)
        if reason:
            continue
        found.append(
            UnreferencedViolation(
                path,
                entry.anchors[-1] if entry.anchors else 0,
                f"{entry.kind} `{entry.identity}`: {error}"
                if error
                else message(entry),
            )
        )
    return found


def main() -> int:
    total = 0
    for path, line, text in check_repo(WORKFLOWS_DIR, ACTIONS_DIR):
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        loc = f"file={rel},line={line}" if line else f"file={rel}"
        print(f"::error {loc}::{text}")
        total += 1
    if total:
        print(f"\nERROR: {total} unreferenced local definition(s) found.")
        print(
            "A definition no `uses:` reaches never runs, and it still reads as a "
            "live code path to the next author."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

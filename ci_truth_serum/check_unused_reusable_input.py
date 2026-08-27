#!/usr/bin/env python3
"""Report a reusable workflow's `workflow_call` input that no caller passes.

A declared input is a promise that some caller needs it. When none does, the
input is one of two things, and both cost real work later:

  * dead surface — a parameter kept in the callee's contract, described in its
    `description:`, and threaded into a step's `env:`, that no run ever sets. A
    reader takes it for a supported feature and writes against it;
  * a re-implementation waiting to happen. This is the expensive one. The
    shared workflow already solves the problem, nobody wired it, so the next
    author who needs it writes their own copy beside it. The duplicate then
    diverges, because only one of the two gets each later fix.

The second case is what motivated this check. A decide gate shipped a
`paths-regex-file` input built to read a trigger regex out of a shell file, so
the regex could stay in the one file other tooling also sources. No workflow
passed it. A sibling workflow needing exactly that grew a 65-line bespoke script
instead, with its own copy of the range resolution, the draft skip and the path
match. Nothing was red for as long as that lasted: both paths worked, and the
declared input sat there reading as an unused option rather than as the answer.

Both sides are static YAML, so the check reads them:

  * the promise is each name under `on.workflow_call.inputs` in a workflow this
    repository calls;
  * the use is a caller job's `with:` mapping on its `uses: ./<callee>` — the
    only way a `workflow_call` input can be set.

A reusable workflow that NO local job calls is skipped entirely. Its callers may
live in another repository (`uses: owner/repo/.github/workflows/x.yaml@ref`),
which this tree cannot see, so every one of its inputs would report and none of
the reports could be checked here. That limit is deliberate: this check reports
an unused input, never an unused workflow.

Suppress one input with a `# unused-input-ok: <reason>` comment. The reason is
mandatory. The comment goes on the input's own key line or one of its direct
children, so the same text elsewhere in the file stays content. Write it for an
input a caller is about to use, or one an external caller passes.

Globs every workflow like the other workflow lints; the passed file list is
ignored.
"""

import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader as _LineLoader,
    is_placeholder_reason,
    workflow_files,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

OPT_OUT = "unused-input-ok"
# `# unused-input-ok: <reason>` — per-input suppression; reason mandatory.
# An annotation READER (it extracts the reason so a placeholder can be rejected),
# not a boolean opt-out predicate. The lead mirrors `_cts_linecheck.annotation_re`:
# the token may follow the `#` directly, or after same-line comment text whose
# last character cannot belong to a token, so a longer slug never satisfies it.
_OPT_OUT = re.compile(rf"#(?:[^\r\n]*[^\w\r\n-])?{OPT_OUT}\s*:\s*(?P<reason>[^\r\n]*)$")

# The YAML key LineLoader adds to every mapping. Never an input name or a job id.
LINE_KEY = "__line__"


def _drop_line_key(mapping: object) -> dict:
    """MAPPING without the line tag LineLoader adds, or {} when it is not one."""
    if not isinstance(mapping, dict):
        return {}
    return {k: v for k, v in mapping.items() if k != LINE_KEY}


def call_inputs(doc: object) -> dict:
    """The `on.workflow_call.inputs` mapping of DOC, or {} when it declares none.

    `on` is read through both spellings PyYAML can produce for it: the string
    key, and the boolean True that the YAML 1.1 core schema resolves `on:` to.
    """
    if not isinstance(doc, dict):
        return {}
    triggers = doc.get("on", doc.get(True))
    if not isinstance(triggers, dict):
        return {}
    call = triggers.get("workflow_call")
    if not isinstance(call, dict):
        return {}
    return _drop_line_key(call.get("inputs"))


def local_callee(job: object) -> str | None:
    """The repo-relative workflow path a job calls with `uses: ./…`, else None.

    A `uses:` naming another repository carries an `@ref` and is not a call this
    tree can resolve, so it names no local callee.
    """
    if not isinstance(job, dict):
        return None
    uses = str(job.get("uses", "")).strip()
    if not uses.startswith("./") or not uses.endswith((".yaml", ".yml")):
        return None
    return uses.removeprefix("./")


def calls(doc: object) -> list[tuple[str, set[str]]]:
    """Every `uses: ./…` call DOC makes, as (callee path, names passed in `with`).

    One entry per calling JOB, not per callee: two jobs may call the same
    workflow and pass different inputs, and the union of what any caller passes
    is what marks an input used.
    """
    if not isinstance(doc, dict):
        return []
    found = []
    for job in _drop_line_key(doc.get("jobs")).values():
        callee = local_callee(job)
        if callee is not None:
            found.append((callee, set(_drop_line_key(job.get("with")))))
    return found


def key_line(text: str, child_line: int) -> int | None:
    """The 1-based line of the key whose block starts at CHILD_LINE.

    LineLoader tags a mapping with the line of its FIRST KEY, so an input's
    parsed line is the first line of its `type:`/`default:` block, one below the
    `alpha:` line a marker sits on. The key is the nearest line above it at a
    smaller indent, which the parse guarantees exists for a nested mapping.
    """
    lines = text.splitlines()
    if not 1 <= child_line <= len(lines):
        return None
    head = lines[child_line - 1]
    indent = len(head) - len(head.lstrip())
    for number in range(child_line - 1, 0, -1):
        above = lines[number - 1]
        if above.strip() and len(above) - len(above.lstrip()) < indent:
            return number
    return None


def marker_window(text: str, line: int) -> list[str]:
    """The lines a marker for the key at 1-based LINE may sit on.

    The key's own line plus its DIRECT children — the block's shallowest child
    indent, which is where a standalone comment beside the key sits. Scoped by
    the key's parsed line rather than by re-matching its name, because an input
    may share a name with a job or a step key elsewhere in the file, and a
    marker read out of that other block would suppress a real finding.
    """
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        return []
    head = lines[line - 1]
    indent = len(head) - len(head.lstrip())
    block = []
    for follow in lines[line:]:
        if not follow.strip():
            continue
        if len(follow) - len(follow.lstrip()) <= indent:
            break
        block.append(follow)
    child_indent = min((len(b) - len(b.lstrip()) for b in block), default=None)
    return [head] + [b for b in block if len(b) - len(b.lstrip()) == child_indent]


def suppression(window: list[str]) -> tuple[str | None, str | None]:
    """(reason, error) for the `# unused-input-ok:` marker on WINDOW.

    Both are None when no marker is present. A marker stating no real reason
    yields an error instead of a reason, so it suppresses nothing.
    """
    details = []
    for line in window:
        match = _OPT_OUT.search(line)
        if not match:
            continue
        reason = match.group("reason").strip().lstrip("#").strip()
        if not is_placeholder_reason(reason):
            return reason, None
        details.append(f"states only {reason!r}" if reason else "carries no reason")
    if not details:
        return None, None
    return None, (
        f"`# {OPT_OUT}` {details[0]}. A suppression must say who passes this "
        "input, or when a caller will — otherwise the input is dead surface "
        f"and the marker only hides it (`# {OPT_OUT}: <reason>`)."
    )


def unused(doc: object, text: str, passed: set[str]) -> list[tuple[int | None, str]]:
    """(line, message) for every input of DOC that PASSED does not name."""
    found = []
    for name, spec in call_inputs(doc).items():
        if name in passed:
            continue
        child = spec.get(LINE_KEY) if isinstance(spec, dict) else None
        line = key_line(text, child) if child else None
        reason, error = suppression(marker_window(text, line) if line else [])
        if error:
            found.append((line, f"input `{name}`: {error}"))
            continue
        if reason:
            continue
        required = isinstance(spec, dict) and spec.get("required") is True
        note = (
            " It is `required: true`, so a caller that did reach this workflow "
            "would already fail to start — nothing calls it with this input at all."
            if required
            else ""
        )
        found.append(
            (
                line,
                f"input `{name}` is declared but no job in this repository passes "
                f"it in a `with:` block.{note} An unused input reads as a "
                "supported option while nothing exercises it, and the next author "
                "who needs it writes a second implementation beside this one. "
                f"Delete it, wire the caller that wants it, or suppress with "
                f"`# {OPT_OUT}: <reason>` naming who passes it.",
            )
        )
    return found


class UnusedInputViolation(NamedTuple):
    """One reported line: the file, the line number (0 when none applies), and
    the message."""

    path: Path
    line: int
    message: str


def check_repo(workflows_dir: Path) -> list[UnusedInputViolation]:
    """(path, line, message) for every unused input under WORKFLOWS_DIR.

    Two passes, because a callee's verdict depends on every other workflow: the
    first reads each file once and records what it declares and what it calls,
    the second judges each callee against the union of what its callers pass.
    """
    parsed: dict[Path, tuple[object, str]] = {}
    unparseable: list[UnusedInputViolation] = []
    for path in workflow_files(workflows_dir):
        text = path.read_text(encoding="utf-8")
        try:
            parsed[path] = (yaml.load(text, Loader=_LineLoader), text)
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            unparseable.append(
                UnusedInputViolation(
                    path,
                    0,
                    f"could not parse as YAML ({first_line}); cannot tell which "
                    "reusable inputs this file passes or declares — fix the syntax "
                    "(or run actionlint) and re-check.",
                )
            )

    # name -> the union of inputs any local job passes to it.
    passed: dict[str, set[str]] = {}
    called: set[str] = set()
    for doc, _ in parsed.values():
        for callee, names in calls(doc):
            called.add(callee)
            passed.setdefault(callee, set()).update(names)

    found = list(unparseable)
    for path, (doc, text) in parsed.items():
        if not call_inputs(doc):
            continue
        rel = str(path.relative_to(workflows_dir.parent.parent))
        # A callee nothing here calls may be called from another repository, and
        # this tree cannot see those `with:` blocks. Reporting every one of its
        # inputs would be a finding no reader could check.
        if rel not in called:
            continue
        for line, message in unused(doc, text, passed.get(rel, set())):
            found.append(UnusedInputViolation(path, line or 0, message))
    return found


def main() -> int:
    total = 0
    for path, line, message in check_repo(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        loc = f"file={rel},line={line}" if line else f"file={rel}"
        print(f"::error {loc}::{message}")
        total += 1
    if total:
        print(f"\nERROR: {total} unused reusable-workflow input(s) found.")
        print(
            "A declared input nothing passes is either dead surface or the shared "
            "answer nobody wired — and the second grows a duplicate implementation."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fail when the external clock's dispatch manifest names a workflow it cannot fire.

Some repositories fire their most important sweeps from a clock they own, off
GitHub's event bus, because that bus is the thing that fails: GitHub drops
`schedule:` fires under load. So a host outside GitHub reads a manifest — one
workflow file name per line — and POSTs a `workflow_dispatch` for each entry
every few minutes. `.github/scheduler/sweeps.txt` is the default manifest path;
`--manifest PATH` names another.

The manifest is the one place a silent dropped tick is born. The clock sends no
inputs and names each workflow by its file name. So an entry fails to fire, on
every tick, forever, when the workflow it names:

  * does not exist — a rename, a typo, or a deleted file — so the dispatch POST
    returns 404;
  * carries no `workflow_dispatch` trigger, so the POST returns 422; or
  * declares a required `workflow_dispatch` input with no default, so the
    bare POST returns 422.

Any of the three stops that sweep with no red mark anywhere GitHub can see: a
dead sweep looks exactly like a quiet one. This is the class the whole external
clock exists to remove, reappearing one level up.

This check is static, and static is a real limit here. The runtime guard — is
the clock HOST alive and firing? — needs a live API read of each sweep's newest
run age, so it lives in the repository the clock serves, not in this offline
pack. This check proves the other half: IF the clock fires, every tick lands.

No opt-out. A manifest entry the clock cannot dispatch is never intentional, so
every finding is a real defect to fix at the source. This check globs every
workflow like the other workflow lints; the passed file list is ignored.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    WORKFLOW_GLOBS,
    has_trigger,
    workflow_triggers,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
DEFAULT_MANIFEST = Path(".github") / "scheduler" / "sweeps.txt"


def parse_manifest(text: str) -> list[tuple[int, str]]:
    """(1-based line, workflow file name) for each dispatch entry in a manifest.

    Read exactly as the dispatcher reads it: take the text before the first `#`,
    then remove every whitespace character (`${wf//[[:space:]]/}` in the shell).
    A blank or comment-only line yields no entry. Reading it any other way would
    verify a name the clock never POSTs.
    """
    entries: list[tuple[int, str]] = []
    for num, raw in enumerate(text.splitlines(), 1):
        name = "".join(raw.split("#", 1)[0].split())
        if name:
            entries.append((num, name))
    return entries


def _required_inputs_without_default(doc: object) -> list[str]:
    """Every `workflow_dispatch` input DOC declares that is required and has no
    default, sorted. The clock sends no inputs, so each one 422s the dispatch."""
    triggers = workflow_triggers(doc)
    if not isinstance(triggers, dict):
        return []
    dispatch = triggers.get("workflow_dispatch")
    if not isinstance(dispatch, dict):
        return []
    inputs = dispatch.get("inputs")
    if not isinstance(inputs, dict):
        return []
    return sorted(
        str(name)
        for name, spec in inputs.items()
        if isinstance(spec, dict)
        and spec.get("required") is True
        and "default" not in spec
    )


def dispatch_defect(doc: object) -> str | None:
    """Why a bare `workflow_dispatch` POST cannot fire the workflow DOC, or None
    when it can. DOC is the parsed workflow document."""
    if not has_trigger(doc, "workflow_dispatch"):
        return (
            "carries no `workflow_dispatch` trigger, so the clock's dispatch "
            "POST returns 422 and this sweep never fires. Add a bare "
            "`workflow_dispatch:` trigger to its `on:` block"
        )
    required = _required_inputs_without_default(doc)
    if required:
        joined = ", ".join(required)
        return (
            f"declares required `workflow_dispatch` input(s) with no default "
            f"({joined}); the clock sends no inputs, so every dispatch POST "
            "returns 422 and this sweep never fires. Give each a `default:`, or "
            "make it `required: false`"
        )
    return None


def violations(
    manifest_text: str, resolve: Callable[[str], "str | None"]
) -> list[tuple[int, str]]:
    """(1-based manifest line, message) for every dispatch target the clock
    cannot fire. An empty list means every entry names a dispatchable workflow.

    RESOLVE maps a workflow file name to its source text, or None when no such
    workflow exists. It is a parameter, not a file read here, so a test and the
    fuzz suite drive this function without touching the disk.
    """
    out: list[tuple[int, str]] = []
    for line, name in parse_manifest(manifest_text):
        source = resolve(name)
        if source is None:
            out.append(
                (
                    line,
                    f"`{name}` names no workflow under .github/workflows/, so the "
                    "clock's dispatch POST returns 404 and this sweep never fires. "
                    "Correct the name here, or restore the workflow file.",
                )
            )
            continue
        try:
            doc = safe_load(source)
        except yaml.YAMLError as err:
            first = str(err).partition(chr(10))[0]
            out.append(
                (
                    line,
                    f"`{name}` did not parse as YAML ({first}); this check cannot "
                    "confirm it accepts `workflow_dispatch`. Fix the syntax (or run "
                    "actionlint) and re-check.",
                )
            )
            continue
        defect = dispatch_defect(doc)
        if defect is not None:
            out.append((line, f"`{name}` {defect}."))
    return out


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        metavar="PATH",
        help="the external clock's dispatch manifest, relative to the repo root "
        f"(default: {DEFAULT_MANIFEST})",
    )
    args = parser.parse_args(argv)

    # No manifest means no external clock here, so there is nothing to verify. The
    # note is what tells this honest empty scan from a real pass. A manifest that
    # DOES list entries never reaches it: an empty workflows tree then makes every
    # entry a 404 finding, which is louder than a note and must not be muted.
    manifest_path = REPO_ROOT / args.manifest
    if not manifest_path.exists():
        print(
            f"note: no dispatch manifest at {args.manifest} — no external clock "
            "here, so this check scanned nothing.",
            file=sys.stderr,
        )
        return 0

    existing = {
        path.name: path for glob in WORKFLOW_GLOBS for path in WORKFLOWS_DIR.glob(glob)
    }

    def resolve(name: str) -> "str | None":
        path = existing.get(name)
        return path.read_text(encoding="utf-8") if path else None

    found = violations(manifest_path.read_text(encoding="utf-8"), resolve)
    for line, message in found:
        print(f"::error file={args.manifest},line={line}::{message}")
    if found:
        print(
            f"\nERROR: {len(found)} external-clock dispatch-target violation(s) found."
        )
        print(
            "A manifest entry the clock cannot dispatch fires nothing on every "
            "tick, and no red mark says so."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

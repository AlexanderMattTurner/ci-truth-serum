#!/usr/bin/env python3
"""
Forbid a static *cancellable* workflow-level concurrency lock on a workflow that
declares a required status check.

`check-static-concurrency` already flags a static workflow-level group (no
per-ref key) on the *heuristic* required-check shape — a job that `uses:`
decide-reusable.yaml or conditions on `needs.decide.outputs`, plus an `always()`
reporter. But a required check does not have to take that shape: a workflow can
declare one purely by marking a job `# required-check: true` (the mandatory SSOT
marker that check-required-reporter enforces and sync-required-checks reads) with
no decide gate at all. Such a workflow is invisible to the decide+always()
heuristic, so a static workflow-level concurrency group on it slips through — the
class this lint was added to close after the static-concurrency lint shipped.

The hazard, when the group is BOTH static (no per-ref key) AND cancellable
(`cancel-in-progress` truthy): a sibling ref's run shares the one static slot and
cancels this run wholesale. Because the `concurrency:` block sits at the
*workflow* level, that cancellation tears down every job — including the
`always()` reporter, which `always()` does NOT survive under a run-level cancel —
so the run starts/keeps zero reporting jobs, no status is posted for this ref's
head, and the required check hangs at "Expected — Waiting for status to be
reported" forever. A per-ref/per-PR group avoids this (a superseding run is the
same ref's newer run, which re-reports); moving the `concurrency:` block onto the
expensive job avoids it (the run + reporter always execute and a superseded run
goes definitively red); `cancel-in-progress: false` avoids the cancellation.

Keyed off the explicit `# required-check: true` marker (mandatory on such
workflows), so false positives are low: this repo's required-check workflows all
use per-ref cancellable groups — the blessed pattern — and none are flagged.

Opt out with "# cancellable-required-check-ok" for a workflow whose static
cancellable group is deliberate and known-safe.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    group_is_per_ref,
    required_check_contexts,
)

OPT_OUT = "cancellable-required-check-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


def _concurrency_line(text: str) -> int:
    """Return the 1-based line number of the top-level `concurrency:` key."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^concurrency\s*:", line):
            return num
    return 1


def _opted_out(text: str) -> bool:
    """True only when the opt-out token appears inside an actual `#` comment, not
    anywhere in the byte stream — a `group: "<token>"` string value must not
    silently disable the lint (that would be a fail-open)."""
    return any(
        OPT_OUT in line.split("#", 1)[1] for line in text.splitlines() if "#" in line
    )


def _is_cancellable(value: object) -> bool:
    """True if a `cancel-in-progress` value can evaluate to cancel-on-supersede.

    PyYAML parses `true`/`false` as bools and an expression such as
    `${{ github.event_name == 'pull_request' }}` as a string. Absent (None) means
    the field defaults to false. Any non-`false` value — `true` or an expression
    that could be true at runtime — is treated as cancellable, so an expression is
    conservatively flagged rather than assumed safe."""
    if value is None or value is False:
        return False
    if value is True:
        return True
    return str(value).strip().lower() != "false"


def check_file(path: Path) -> tuple[int | None, str] | None:
    """Return (line, message) if PATH declares a required check (via a
    `# required-check: true` marker) and carries a workflow-level concurrency
    group that is both static (no per-ref key) and cancellable.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_static_concurrency &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return None, (
            f"could not parse as YAML ({first_line}); cannot verify workflow-level "
            "concurrency safety on a required-check workflow — fix the syntax (or "
            "run actionlint) and re-check."
        )
    if not isinstance(doc, dict):
        return None

    conc = doc.get("concurrency")
    if not isinstance(conc, dict) or "group" not in conc:
        return None
    if _opted_out(text):
        return None

    group = str(conc.get("group", ""))
    if group_is_per_ref(group):
        return None  # per-ref / per-PR group — only superseded by its own ref
    if not _is_cancellable(conc.get("cancel-in-progress")):
        return None  # not cancel-on-supersede — nothing tears down the reporter

    # Only a workflow that actually declares a required check can be stranded.
    if not required_check_contexts(text):
        return None

    line = _concurrency_line(text)
    return line, (
        "workflow-level concurrency.group is static (no github.ref / "
        "github.head_ref key) AND cancellable (cancel-in-progress truthy) on a "
        "workflow that declares a required check ('# required-check: true'). A "
        "sibling ref's run shares the one static slot and cancels this run "
        "wholesale — the workflow-level cancel tears down the always() reporter "
        "too, no status posts for this ref's head, and the required check hangs "
        "at 'Expected — Waiting' forever. Key the group per-ref/per-PR "
        "(github.ref / github.head_ref / pull_request.number), move the "
        "concurrency: block onto the expensive job so the run + reporter always "
        f"execute, or set cancel-in-progress: false. Add '# {OPT_OUT}' if this "
        "static cancellable group is deliberate and known-safe."
    )


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml"))
    total = 0
    for path in files:
        found = check_file(path)
        if found is None:
            continue
        line, message = found
        rel = path.relative_to(REPO_ROOT)
        loc = f"file={rel},line={line}" if line else f"file={rel}"
        print(f"::error {loc}::{message}")
        total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

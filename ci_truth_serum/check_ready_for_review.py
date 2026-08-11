#!/usr/bin/env python3
"""
Make a draft-gated workflow fire again when the pull request leaves draft.

PROBLEM CLASS — work a pipeline defers is work the pipeline must fire again to
do.

A workflow that skips its jobs while the pull request is a draft has deferred
them. GitHub delivers "the pull request left draft" as the ``pull_request``
activity type ``ready_for_review``. That type is not in the default set
(``opened``, ``synchronize``, ``reopened``). So a workflow that gates on the
draft flag and omits ``ready_for_review`` never fires again. The draft run stays
the last run. Its gated jobs are ``skipped``, and GitHub counts a skipped check
run as a satisfied required check. The check then reports green, and it did none
of its work.

Two shapes gate on draft. This lint judges both:

* the workflow names ``github.event.pull_request.draft`` — in an ``if:``, an
  ``env:`` value, or a ``with:`` input;
* a job calls a reusable workflow and passes an input whose NAME carries
  ``draft`` (``skip-on-draft: true``). The gate sits in the callee, so the
  payload field never appears in the caller. On a 50-workflow tree this second
  shape was 10 of the 18 draft-gated workflows.

The fix is one list entry. Add ``ready_for_review`` to that trigger's ``types:``.

The lint stays silent unless two things hold. The workflow declares a
``pull_request`` or ``pull_request_target`` trigger, and it gates on draft. A
reusable workflow declares no pull request trigger, so this never judges one.

Opt out with ``# ready-for-review-ok`` when the deferral is deliberate.

Globs every workflow like check_frozen_head_sha; argv is ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import opted_out  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_triggers  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ALLOW = "ready-for-review-ok"

PR_TRIGGERS = ("pull_request", "pull_request_target")
READY = "ready_for_review"

# The payload field a workflow reads to learn the pull request is a draft.
DRAFT_FIELD = re.compile(r"github\.event\.pull_request\.draft\b")
# A reusable-call input whose name says the callee gates on draft. The name is
# the only signal here: the caller passes a boolean and never names the field.
DRAFT_INPUT = re.compile(r"draft", re.IGNORECASE)

_MESSAGE = (
    "workflow gates on the pull request's draft flag but its {trigger} trigger "
    f"does not list `{READY}`, whose absence is the default. The gated jobs skip "
    "while the pull request is a draft; marking it ready fires no run, so the "
    "last run's `skipped` jobs stand — and GitHub counts a skipped check run as "
    f"a satisfied required check. Add `{READY}` to that trigger's `types:`. If "
    f"the deferral is deliberate, annotate the file with '# {ALLOW}'."
)


def _declared_pr_triggers(doc: dict) -> dict[str, object]:
    """Each pull-request trigger the workflow declares, mapped to its `types:`
    value (None when the trigger declares none, which means GitHub's default set
    — and that set excludes `ready_for_review`)."""
    triggers = workflow_triggers(doc)
    declared: dict[str, object] = {}
    for name in PR_TRIGGERS:
        if isinstance(triggers, str) and triggers == name:
            declared[name] = None
        elif isinstance(triggers, list) and name in triggers:
            declared[name] = None
        elif isinstance(triggers, dict) and name in triggers:
            value = triggers[name]
            declared[name] = value.get("types") if isinstance(value, dict) else None
    return declared


def _reads_draft_field(node: object) -> bool:
    """True when any string anywhere under NODE names the draft payload field."""
    if isinstance(node, str):
        return bool(DRAFT_FIELD.search(node))
    if isinstance(node, dict):
        return any(_reads_draft_field(v) for v in node.values())
    if isinstance(node, list):
        return any(_reads_draft_field(v) for v in node)
    return False


def _calls_reusable_with_draft_input(doc: dict) -> bool:
    """True when a job calls a reusable workflow and passes an input whose NAME
    carries `draft` — the gate then lives in the callee, so the payload field
    never appears in this file."""
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict) or not isinstance(job.get("uses"), str):
            continue
        inputs = job.get("with")
        if isinstance(inputs, dict) and any(DRAFT_INPUT.search(str(k)) for k in inputs):
            return True
    return False


def _trigger_line(text: str, trigger: str) -> int:
    """1-based line of TRIGGER's key, where the `types:` edit goes, else 1. The
    decision above is made on the parsed document; this only anchors the report.
    """
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(rf"^\s*{re.escape(trigger)}\s*:", line):
            return num
    return 1


def find_violations(text: str) -> list[tuple[int | None, str]]:
    """(line, message) when the workflow gates on draft and no declared pull
    request trigger lists `ready_for_review`. An unparseable workflow is reported
    as a violation rather than passed as clean — a false green on the file under
    test."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                f"{READY} coverage — fix the syntax (or run actionlint) and "
                "re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    declared = _declared_pr_triggers(doc)
    if not declared:
        return []
    if not (_reads_draft_field(doc) or _calls_reusable_with_draft_input(doc)):
        return []
    # One trigger carrying the type is enough: that run re-fires the whole
    # workflow, so the gated jobs get their chance whichever trigger delivered it.
    # `types:` takes a bare scalar as well as a list, and both declare the type.
    for types in declared.values():
        if types == READY or (isinstance(types, list) and READY in types):
            return []
    if opted_out(text, ALLOW):
        return []

    trigger = sorted(declared)[0]
    return [(_trigger_line(text, trigger), _MESSAGE.format(trigger=trigger))]


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for PATH's missing ready_for_review coverage."""
    return find_violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} draft-gated workflow(s) missing {READY}.")
        print(
            "A deferred job that never re-fires leaves a skipped check run "
            "standing, which GitHub counts as a satisfied required check."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

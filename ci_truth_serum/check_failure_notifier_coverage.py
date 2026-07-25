#!/usr/bin/env python3
"""Keep the failure notifier's workflow_run list in sync with the tree.

A failure-notifier workflow listens via `on.workflow_run.workflows:` — a list of
workflow display NAMES (`name:` values). `workflow_run` has no wildcard, so that
list is necessarily a generated copy of the tree; this lint is the round-trip
freshness check that makes it a sanctioned derived cache rather than
hand-maintained duplication. The invariant: the listed names cover exactly the
set of `name:` values of every workflow in `.github/workflows/` with a `push:` or
`schedule:` trigger, the notifiers themselves excluded. A workflow the list omits
fails silently forever; a stale name notifies on nothing.

The notifier is DISCOVERED, never named. A repo calls the file whatever it likes
(`build-publish-notify.yaml`, `alerts.yaml`), so matching one hardcoded basename
made this lint pass VACUOUSLY — reporting success while checking nothing — in
every repo that spelled it differently, which is the exact failure mode the
package exists to catch. A workflow is the notifier when BOTH hold:

  * it is triggered `on.workflow_run` — the only trigger that can observe another
    workflow's conclusion; and
  * it names a notification sink (`_linecheck.NOTIFIER_PATTERNS`, shared with
    check_cron_alert_coverage) in its own `name:`, a job `name:`/`uses:`, or a
    step's `uses:`/`name:`/`run:`.

Both halves are load-bearing. Without the trigger test, an ordinary CI workflow
with a Slack step would be mistaken for the notifier; without the sink test, an
unrelated `workflow_run` consumer (a post-run artifact collector, a coverage
uploader) would be held to the notifier's exhaustiveness invariant. Teach it a
house sink with `--notifier-pattern REGEX` (repeatable); the flag EXTENDS the
built-in patterns rather than replacing them.

More than one notifier is allowed: coverage is the UNION of their lists (two
notifiers may legitimately split the tree), while staleness and duplication are
judged per file, since a name no workflow carries is dead wherever it sits.

Two modes. Without flags, a repo with no discoverable notifier passes silently —
the hook can ship in default hook sets without breaking repos that haven't
adopted the notifier. With `--require-notifier`, having no notifier at all is
itself a failure — enable the flag once a repo adopts the pattern so deleting the
notifier can't silently pass.

A monitored workflow without a `name:` field is flagged: GitHub falls back to the
workflow's file path as its display name, which is what the notifier list would
then have to carry — add an explicit `name:` instead. On any mismatch the
corrected `workflows:` YAML block is printed so the fix is copy-paste. Globs
every workflow like the other workflow lints; the passed file list is ignored.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    has_trigger,
    notifier_matcher,
    step_text,
    workflow_triggers,
    WORKFLOW_GLOBS,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

MONITORED_TRIGGERS = ("push", "schedule")


def has_monitored_trigger(doc: dict) -> bool:
    """True when the workflow fires on push or schedule (the events the notifier
    must observe), whatever shape the `on:` value takes."""
    return has_trigger(doc, *MONITORED_TRIGGERS)


def names_a_sink(doc: dict, matcher: "re.Pattern[str]") -> bool:
    """True when any text in the workflow that can name a notification sink does.

    Scans the workflow's own `name:`, then each job's `name:`/`uses:` and each of
    its steps — the same fields check_cron_alert_coverage reads, so the two lints
    agree on what a sink looks like.
    """
    if matcher.search(str(doc.get("name", ""))):
        return True
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return False
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        if matcher.search(step_text(job)):
            return True
        steps = job.get("steps")
        if not isinstance(steps, list):
            continue
        if any(
            isinstance(step, dict) and matcher.search(step_text(step)) for step in steps
        ):
            return True
    return False


def is_notifier(doc: object, matcher: "re.Pattern[str]") -> bool:
    """True when DOC is a failure-notifier workflow: it observes other workflows
    (`on.workflow_run`) AND names a notification sink. Both halves are required —
    see the module docstring for what each one alone would misclassify."""
    if not isinstance(doc, dict) or not has_trigger(doc, "workflow_run"):
        return False
    return names_a_sink(doc, matcher)


def expected_names(docs: list[tuple[Path, dict]]) -> tuple[set[str], list[str]]:
    """(display names the notifier must list, unnamed-workflow warnings) over
    every monitored workflow in DOCS (the notifiers excluded by the caller).

    A workflow without `name:` contributes GitHub's fallback display name — the
    workflow file's repo-relative path — and earns a warning to add `name:`.
    """
    names: set[str] = set()
    warnings: list[str] = []
    for path, doc in docs:
        if not has_monitored_trigger(doc):
            continue
        name = doc.get("name")
        if isinstance(name, str) and name:
            names.add(name)
            continue
        fallback = str(path.relative_to(REPO_ROOT))
        names.add(fallback)
        warnings.append(
            f"{fallback}: has a push/schedule trigger but no `name:` — GitHub "
            "falls back to the file path as its display name, which the "
            "notifier list must then carry verbatim. Add an explicit `name:`."
        )
    return names, warnings


def notifier_list(doc: object) -> list[str] | None:
    """The notifier's `on.workflow_run.workflows` list, or None when the
    document doesn't carry one (a malformed notifier is a finding)."""
    triggers = workflow_triggers(doc)
    workflow_run = triggers.get("workflow_run") if isinstance(triggers, dict) else None
    workflows = (
        workflow_run.get("workflows") if isinstance(workflow_run, dict) else None
    )
    if isinstance(workflows, list) and all(isinstance(w, str) for w in workflows):
        return workflows
    return None


def corrected_block(names: set[str]) -> str:
    """The exact `workflows:` YAML block the notifier should carry, sorted so
    the output is stable and copy-paste ready."""
    lines = ["    workflows:"] + [f'      - "{name}"' for name in sorted(names)]
    return "\n".join(lines)


def workflow_files() -> list[Path]:
    return sorted(p for glob in WORKFLOW_GLOBS for p in WORKFLOWS_DIR.glob(glob))


def load_workflows() -> tuple[list[tuple[Path, dict]], list[str]]:
    """(path, parsed mapping) for every readable workflow, plus one message per
    file the parser rejected.

    An unparseable workflow is reported rather than raised: it may be the
    notifier or a workflow the notifier must list, and either way coverage can no
    longer be verified — which is a finding, not a crash.
    """
    docs: list[tuple[Path, dict]] = []
    errors: list[str] = []
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            errors.append(
                f"::error file={rel}::could not parse as YAML ({first_line}); "
                "cannot verify failure-notifier coverage — fix the syntax (or run "
                "actionlint) and re-check."
            )
            continue
        if isinstance(doc, dict):
            docs.append((path, doc))
    return docs, errors


def check_repo(require_notifier: bool, extra_patterns: Iterable[str] = ()) -> list[str]:
    """Every coverage violation for the repo, as printable messages."""
    matcher = notifier_matcher(extra_patterns)
    docs, found = load_workflows()

    notifiers = [(path, doc) for path, doc in docs if is_notifier(doc, matcher)]
    if not notifiers:
        if require_notifier:
            found.append(
                "::error::no failure-notifier workflow found under "
                ".github/workflows/ but --require-notifier is set. A notifier is a "
                "workflow triggered `on.workflow_run` that names a notification "
                "sink (ntfy, Slack, an issue-opening step, …). Add one, teach this "
                "lint your house sink with --notifier-pattern, or drop the flag."
            )
        return found

    notifier_paths = {path for path, _ in notifiers}
    expected, warnings = expected_names(
        [(path, doc) for path, doc in docs if path not in notifier_paths]
    )
    found += [f"::error::{w}" for w in warnings]

    covered: set[str] = set()
    for path, doc in notifiers:
        rel = path.relative_to(REPO_ROOT)
        listed = notifier_list(doc)
        if listed is None:
            found.append(
                f"::error file={rel}::has no `on.workflow_run.workflows` list of "
                "workflow names — the notifier cannot observe anything."
            )
            continue
        covered |= set(listed)
        stale = sorted(set(listed) - expected)
        duplicated = len(listed) != len(set(listed))
        if not stale and not duplicated:
            continue
        detail = "; ".join(
            part
            for part in (
                f"stale (matches nothing): {stale}" if stale else "",
                "duplicates present" if duplicated else "",
            )
            if part
        )
        found.append(
            f"::error file={rel}::`on.workflow_run.workflows` is out of sync with "
            f"the tree — {detail}. Replace the list with:\n"
            f"{corrected_block(expected)}"
        )

    missing = sorted(expected - covered)
    if missing:
        rel = notifiers[0][0].relative_to(REPO_ROOT)
        found.append(
            f"::error file={rel}::`on.workflow_run.workflows` does not cover the "
            f"tree — missing (fails silently): {missing}. Replace the list with:\n"
            f"{corrected_block(expected)}"
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notifier-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help="an additional regex recognizing a notification sink when "
        "discovering the notifier workflow (repeatable); extends the built-in "
        "patterns rather than replacing them",
    )
    parser.add_argument(
        "--require-notifier",
        action="store_true",
        help="also fail when no notifier workflow can be discovered at all "
        "(default: a repo that has not adopted the notifier passes silently)",
    )
    # pre-commit is configured `pass_filenames: false`, but a consumer wiring the
    # module by hand may still hand it paths; discovery globs the tree either way.
    parser.add_argument("files", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    violations = check_repo(args.require_notifier, args.notifier_pattern)
    for message in violations:
        print(message)
    if violations:
        print(f"\nERROR: {len(violations)} notifier-coverage violation(s) found.")
        print(
            "The workflow_run list is a derived copy of the tree's push/schedule "
            "workflows; a stale copy silently drops failure notifications."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

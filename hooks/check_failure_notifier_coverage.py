#!/usr/bin/env python3
"""Keep ci-failure-notify.yaml's workflow_run list in sync with the tree.

A failure-notifier workflow (`.github/workflows/ci-failure-notify.yaml`) listens
via `on.workflow_run.workflows:` — a list of workflow display NAMES (`name:`
values). `workflow_run` has no wildcard, so that list is necessarily a generated
copy of the tree; this lint is the round-trip freshness check that makes it a
sanctioned derived cache rather than hand-maintained duplication. The invariant:
the list equals exactly the set of `name:` values of every workflow in
`.github/workflows/` with a `push:` or `schedule:` trigger, excluding
ci-failure-notify.yaml itself. A workflow the list omits fails silently forever;
a stale name notifies on nothing.

Two modes. Without flags, a repo with no ci-failure-notify.yaml passes silently
— the hook can ship in default hook sets without breaking repos that haven't
adopted the notifier. With `--require-notifier`, a missing notifier workflow is
itself a failure — enable the flag once a repo adopts the pattern so deleting
the notifier can't silently pass.

A monitored workflow without a `name:` field is flagged: GitHub falls back to
the workflow's file path as its display name, which is what the notifier list
would then have to carry — add an explicit `name:` instead. On any mismatch the
corrected `workflows:` YAML block is printed so the fix is copy-paste. Globs
every workflow like the other workflow lints; the passed file list is ignored.
"""

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import WORKFLOW_GLOBS  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

NOTIFIER = "ci-failure-notify.yaml"
MONITORED_TRIGGERS = ("push", "schedule")


def _triggers(doc: dict) -> object:
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    return doc.get("on", doc.get(True))


def has_monitored_trigger(doc: dict) -> bool:
    """True when the workflow fires on push or schedule (the events the notifier
    must observe), whatever shape the `on:` value takes."""
    triggers = _triggers(doc)
    if isinstance(triggers, str):
        return triggers in MONITORED_TRIGGERS
    if isinstance(triggers, list):
        return any(t in MONITORED_TRIGGERS for t in triggers)
    if isinstance(triggers, dict):
        return any(t in triggers for t in MONITORED_TRIGGERS)
    return False


def expected_names(paths: list[Path]) -> tuple[set[str], list[str]]:
    """(display names the notifier must list, unnamed-workflow warnings) over
    every monitored workflow in PATHS (the notifier itself excluded by caller).

    A workflow without `name:` contributes GitHub's fallback display name — the
    workflow file's repo-relative path — and earns a warning to add `name:`.
    """
    names: set[str] = set()
    warnings: list[str] = []
    for path in paths:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            warnings.append(
                f"{path.relative_to(REPO_ROOT)}: could not parse as YAML "
                f"({first_line}); cannot verify failure-notifier coverage — fix the "
                "syntax (or run actionlint) and re-check."
            )
            continue
        if not isinstance(doc, dict) or not has_monitored_trigger(doc):
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
    if not isinstance(doc, dict):
        return None
    triggers = _triggers(doc)
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


def check_repo(require_notifier: bool) -> list[str]:
    """Every coverage violation for the repo, as printable messages."""
    notifier_path = WORKFLOWS_DIR / NOTIFIER
    if not notifier_path.exists():
        if require_notifier:
            return [
                f"::error::notifier workflow missing: {NOTIFIER} not found under "
                ".github/workflows/ but --require-notifier is set. Add the "
                "notifier workflow or drop the flag."
            ]
        return []

    rel = notifier_path.relative_to(REPO_ROOT)
    expected, warnings = expected_names(
        [p for p in workflow_files() if p.name != NOTIFIER]
    )
    found = [f"::error::{w}" for w in warnings]

    try:
        notifier_doc = yaml.safe_load(notifier_path.read_text())
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        found.append(
            f"::error file={rel}::could not parse as YAML ({first_line}); cannot "
            "verify failure-notifier coverage — fix the syntax (or run actionlint) "
            "and re-check."
        )
        return found

    listed = notifier_list(notifier_doc)
    if listed is None:
        found.append(
            f"::error file={rel}::has no `on.workflow_run.workflows` list of "
            "workflow names — the notifier cannot observe anything."
        )
        return found

    if set(listed) != expected or len(listed) != len(set(listed)):
        missing = sorted(expected - set(listed))
        stale = sorted(set(listed) - expected)
        detail = "; ".join(
            part
            for part in (
                f"missing (fails silently): {missing}" if missing else "",
                f"stale (matches nothing): {stale}" if stale else "",
                "duplicates present" if len(listed) != len(set(listed)) else "",
            )
            if part
        )
        found.append(
            f"::error file={rel}::`on.workflow_run.workflows` is out of sync "
            f"with the tree — {detail}. Replace the list with:\n"
            f"{corrected_block(expected)}"
        )
    return found


def main() -> int:
    require_notifier = "--require-notifier" in sys.argv[1:]
    violations = check_repo(require_notifier)
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

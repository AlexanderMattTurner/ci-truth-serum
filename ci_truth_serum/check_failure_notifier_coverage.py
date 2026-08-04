#!/usr/bin/env python3
"""Keep a failure notifier's workflow_run list covering what nothing else covers.

A failure-notifier workflow listens via `on.workflow_run.workflows:` — a list of
workflow display NAMES (`name:` values). `workflow_run` has no wildcard, so that
list is necessarily a generated copy of part of the tree; this lint is the
round-trip freshness check that makes it a sanctioned derived cache rather than
hand-maintained duplication. Two halves, each with its own invariant:

  * FRESHNESS — every listed name must name a workflow that exists. A name no
    workflow in the tree carries notifies on nothing, wherever it sits.
  * EXHAUSTIVENESS — every workflow whose failure reaches a human by no other
    route must be listed. That residual is `_failure_routing.needs_tree_notifier`
    over the push/schedule workflows: the ones that neither notify themselves,
    nor surface on a pull request, nor carry a reasoned `# cron-alert: false`.

The residual is the point. Demanding EVERY push/schedule workflow instead would
undo the sibling lint's sanctioned opt-out marker, double-page a workflow that
already alerts on its own, and page on failures a reviewer is already looking at
on the PR — and a repo whose every failure pages has trained its owner to swipe
the notifications away. Being listed is deliberately not itself a route (see
`needs_tree_notifier`): this is the check that decides what the list must hold.

Freshness and exhaustiveness are asymmetric on purpose, and the asymmetry is
load-bearing: a repo may watch MORE than the residual. Listing a workflow that
also surfaces on a PR, or one that already self-notifies, is a repo's call to
make — so a listed name is stale only when it matches no workflow at all, never
merely because it sits outside the required set.

The notifier is found by SHAPE, not by filename. A repo calls the file whatever
it likes (`ci-failure-notify.yaml` in the shared template, but also
`build-publish-notify.yaml`, `alerts.yaml`), and a check that recognizes only one
spelling reports success while checking nothing in every repo that chose
another — the exact failure mode this package exists to catch. Discovery is
`_failure_routing.is_notifier`: a `workflow_run` trigger plus a named
notification sink, both halves required. Teach it a house sink with
`--notifier-pattern REGEX` (repeatable); the flag EXTENDS the built-in patterns
rather than replacing them.

More than one notifier is allowed: coverage is the UNION of their lists (two
notifiers may legitimately split the tree), while staleness and duplication are
judged per file, since a name no workflow carries is dead wherever it sits.

Discovery FAILS CLOSED. When no notifier can be found the check reports a
failure, never a pass: it verified nothing, and "verified nothing" is exactly how
a notifier that was deleted, renamed, or never synced from the template goes
unnoticed. The message names `--notifier FILENAME` (default
`ci-failure-notify.yaml`, the shared template's spelling) — the expectation to
state when a repo's notifier is undiscoverable — and distinguishes "that file is
not there at all" from "that file is there but observes nothing". A repo that
deliberately routes failures nowhere says so with `--allow-no-notifier`.

`--require-alert-priority` adds a second invariant on the alerts themselves: a
step invoking a local composite action that DEFAULTS a `priority:` input must
pass one. Omitting it is not neutral — it inherits whatever urgency the action
chose, which for an alert action is the maximum, and a repo whose every alert
arrives at max urgency has trained its owner to swipe them all away. The trigger
is the invoked action's own declared inputs, never an action name, so a house
alert composite is covered the day it grows a defaulted `priority`. Cosmetic
inputs (`tags:`) are deliberately not required: priority is what drives phone
behaviour, and demanding every optional input would make this noise instead of a
severity discipline.

A residual workflow without a `name:` field is flagged: GitHub falls back to the
workflow's file path as its display name, which is what the notifier list would
then have to carry — add an explicit `name:` instead. On any mismatch the
corrected `workflows:` YAML block is printed so the fix is copy-paste; it keeps
the names the repo already watches and adds the ones it must. Globs every
workflow like the other workflow lints; the passed file list is ignored.
"""

import argparse
import re
import sys
from collections.abc import Iterable
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    has_trigger,
    notifier_matcher,
    workflow_files,
)
from _failure_routing import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    is_notifier,
    MONITORED_TRIGGERS,
    needs_tree_notifier,
    notifier_list,
    routing,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# The filename the shared automation template gives its notifier. It is the
# EXPECTATION this check falls back to when shape discovery finds nothing — the
# name to put in the error message, not a filter: a repo whose notifier is called
# something else is still found by shape. Override with `--notifier`.
DEFAULT_NOTIFIER = "ci-failure-notify.yaml"

# The composite-action input whose silent default is a real cost: ntfy priority
# drives phone behaviour (5 bypasses Do Not Disturb and repeats), so a call site
# that omits it inherits max urgency without anyone deciding to. `tags:` is left
# optional on purpose — it is cosmetic, and requiring every optional input would
# make the check noise rather than a severity discipline.
ALERT_PRIORITY_INPUT = "priority"


def has_monitored_trigger(doc: dict) -> bool:
    """True when the workflow fires on push or schedule (the events the notifier
    must observe), whatever shape the `on:` value takes."""
    return has_trigger(doc, *MONITORED_TRIGGERS)


def display_name(path: Path, doc: dict) -> tuple[str, bool]:
    """(the name GitHub shows for this workflow, whether it is an explicit
    `name:`). Without one GitHub falls back to the file's repo-relative path,
    which is then what a notifier list has to carry verbatim."""
    name = doc.get("name")
    if isinstance(name, str) and name:
        return name, True
    return str(path.relative_to(REPO_ROOT)), False


def residual_names(
    docs: list[tuple[Path, dict, str]], matcher: "re.Pattern[str]"
) -> tuple[set[str], list[str]]:
    """(display names the notifier must list, unnamed-workflow warnings) over the
    monitored workflows in DOCS (the notifiers excluded by the caller).

    The residual is every push/schedule workflow whose failure reaches nobody by
    any other route — `_failure_routing.needs_tree_notifier`. A workflow that
    self-notifies, surfaces on a pull request, or carries a reasoned
    `# cron-alert: false` is somebody else's responsibility already, and adding
    it here would double-page or silently overrule the opt-out. A marker that
    states no reason is no opt-out, so such a workflow stays in the residual;
    check_cron_alert_coverage reports the malformed marker itself.
    """
    names: set[str] = set()
    warnings: list[str] = []
    for path, doc, text in docs:
        if not has_monitored_trigger(doc):
            continue
        if not needs_tree_notifier(routing(doc, text, matcher)):
            continue
        name, explicit = display_name(path, doc)
        names.add(name)
        if not explicit:
            warnings.append(
                f"{name}: has a push/schedule trigger, routes its failures "
                "nowhere else, and has no `name:` — GitHub falls back to the file "
                "path as its display name, which the notifier list must then "
                "carry verbatim. Add an explicit `name:`."
            )
    return names, warnings


def corrected_block(names: set[str]) -> str:
    """The exact `workflows:` YAML block the notifier should carry, sorted so
    the output is stable and copy-paste ready."""
    lines = ["    workflows:"] + [f'      - "{name}"' for name in sorted(names)]
    return "\n".join(lines)


def load_workflows() -> tuple[list[tuple[Path, dict, str]], list[str]]:
    """(path, parsed mapping, source text) for every readable workflow, plus one
    message per file the parser rejected.

    The source text rides along because an opt-out marker is a COMMENT: the
    parser drops it, and the routing predicate has to read it back off the lines.

    An unparseable workflow is reported rather than raised: it may be the
    notifier or a workflow the notifier must list, and either way coverage can no
    longer be verified — which is a finding, not a crash.
    """
    docs: list[tuple[Path, dict, str]] = []
    errors: list[str] = []
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        text = path.read_text()
        try:
            doc = yaml.load(text, Loader=LineLoader)
        except yaml.YAMLError as err:
            first_line = str(err).partition("\n")[0]
            errors.append(
                f"::error file={rel}::could not parse as YAML ({first_line}); "
                "cannot verify failure-notifier coverage — fix the syntax (or run "
                "actionlint) and re-check."
            )
            continue
        if isinstance(doc, dict):
            docs.append((path, doc, text))
    return docs, errors


def local_action_dir(uses: object) -> Path | None:
    """The `.github/actions/<name>` directory a `uses: ./.github/actions/<name>`
    step points at, or None when the step calls anything else (a published
    action, a reusable workflow)."""
    if not isinstance(uses, str):
        return None
    prefix = "./.github/actions/"
    if not uses.startswith(prefix):
        return None
    name = uses[len(prefix) :].strip("/")
    return ACTIONS_DIR / name if name else None


def defaults_an_input(action_dir: Path, input_name: str) -> bool:
    """True when the composite action in ACTION_DIR declares INPUT_NAME as an
    OPTIONAL input — i.e. a caller that omits it silently inherits a value the
    action chose. A required input needs no lint: the caller cannot skip it."""
    for filename in ("action.yaml", "action.yml"):
        path = action_dir / filename
        if not path.exists():
            continue
        try:
            doc = yaml.safe_load(path.read_text())
        except (yaml.YAMLError, OSError):
            return False
        inputs = doc.get("inputs") if isinstance(doc, dict) else None
        spec = inputs.get(input_name) if isinstance(inputs, dict) else None
        if not isinstance(spec, dict):
            return False
        return spec.get("required") is not True
    return False


def unprioritized_alert_steps(doc: dict) -> list[tuple[int, str]]:
    """(line, step label) for every step invoking a local composite action that
    DEFAULTS `priority:` without the step stating one.

    Omitting it is not neutral: the call site inherits whatever urgency the
    action picked, which for an alert action is the maximum. A repo where every
    alert arrives at max urgency has trained its owner to swipe them all away, so
    the one that mattered is the one that gets ignored. Discovery is by the
    action's declared inputs, never by action name, so a house alert composite is
    covered the day it grows a defaulted `priority`.
    """
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    found: list[tuple[int, str]] = []
    for job in jobs.values():
        steps = job.get("steps") if isinstance(job, dict) else None
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, dict):
                continue
            action_dir = local_action_dir(step.get("uses"))
            if action_dir is None or not defaults_an_input(
                action_dir, ALERT_PRIORITY_INPUT
            ):
                continue
            with_block = step.get("with")
            if isinstance(with_block, dict) and ALERT_PRIORITY_INPUT in with_block:
                continue
            found.append(
                (
                    step.get("__line__", job.get("__line__", 1)),
                    str(step.get("name") or step["uses"]),
                )
            )
    return found


def check_repo(
    allow_no_notifier: bool = False,
    extra_patterns: Iterable[str] = (),
    notifier_name: str = DEFAULT_NOTIFIER,
    require_alert_priority: bool = False,
) -> list[str]:
    """Every coverage violation for the repo, as printable messages."""
    matcher = notifier_matcher(extra_patterns)
    docs, found = load_workflows()

    if require_alert_priority:
        for path, doc, _ in docs:
            rel = path.relative_to(REPO_ROOT)
            found += [
                f"::error file={rel},line={line}::step {label!r} calls an alert "
                f"action without an explicit `{ALERT_PRIORITY_INPUT}:`, so it "
                "silently inherits the action's default — the maximum. Every alert "
                "at max urgency trains the owner to ignore the one that matters. "
                f"State the `{ALERT_PRIORITY_INPUT}:` you mean."
                for line, label in unprioritized_alert_steps(doc)
            ]

    notifiers = [(path, doc) for path, doc, _ in docs if is_notifier(doc, matcher)]
    if not notifiers:
        # Fail CLOSED: this is the refusal that stops the check reporting a green
        # it did not earn. It verified nothing here, because the thing it verifies
        # is absent — and "absent" is exactly how a notifier gets deleted, renamed,
        # or never synced without anyone noticing. `--allow-no-notifier` is the one
        # way to a pass, and it is a stated decision rather than a silent one.
        if allow_no_notifier:
            return found
        detail = (
            f"`{notifier_name}` exists but is not a notifier — a notifier must be "
            "triggered `on.workflow_run` AND name a notification sink (ntfy, "
            "Slack, an issue-opening step, …); as written it observes nothing."
            if (WORKFLOWS_DIR / notifier_name).exists()
            else f"expected `{notifier_name}`, or any workflow triggered "
            "`on.workflow_run` that names a notification sink."
        )
        found.append(
            "::error::no failure-notifier workflow found under .github/workflows/: "
            f"{detail} Add one, point this check at yours with --notifier / "
            "--notifier-pattern, or pass --allow-no-notifier if this repo "
            "deliberately routes failures nowhere."
        )
        return found

    notifier_paths = {path for path, _ in notifiers}
    required, warnings = residual_names(
        [(path, doc, text) for path, doc, text in docs if path not in notifier_paths],
        matcher,
    )
    found += [f"::error::{w}" for w in warnings]
    # Staleness is judged against EVERY workflow in the tree, notifiers included,
    # not against the residual: a repo may deliberately watch more than the
    # minimum, so a listed name is dead only when it names no workflow at all.
    real = {display_name(path, doc)[0] for path, doc, _ in docs}

    covered: set[str] = set()
    stale_by_file: list[tuple[Path, list[str], bool]] = []
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
        stale = sorted(set(listed) - real)
        duplicated = len(listed) != len(set(listed))
        if stale or duplicated:
            stale_by_file.append((path, stale, duplicated))

    missing = sorted(required - covered)
    # What the list should hold: everything it already watches that really
    # exists, plus whatever the residual says is still unwatched. Dropping the
    # extras a repo chose to watch is not this check's call to make.
    suggested = (covered & real) | required

    for path, stale, duplicated in stale_by_file:
        detail = "; ".join(
            part
            for part in (
                f"stale (matches nothing): {stale}" if stale else "",
                "duplicates present" if duplicated else "",
            )
            if part
        )
        found.append(
            f"::error file={path.relative_to(REPO_ROOT)}::"
            f"`on.workflow_run.workflows` is out of date — {detail}. Replace the "
            f"list with:\n{corrected_block(suggested)}"
        )

    if missing:
        rel = notifiers[0][0].relative_to(REPO_ROOT)
        found.append(
            f"::error file={rel}::`on.workflow_run.workflows` does not cover the "
            "workflows whose failures reach nobody else — missing (fails "
            f"silently): {missing}. Each one has a push/schedule trigger, no "
            "reachable notification of its own, no pull-request surface, and no "
            "reasoned `# cron-alert: false`, so its red is seen only by whoever "
            f"opens the Actions tab. Replace the list with:\n"
            f"{corrected_block(suggested)}"
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
        "--notifier",
        default=DEFAULT_NOTIFIER,
        metavar="FILENAME",
        help="the filename this repo expects its notifier to carry, named in the "
        f"fail-closed message when none is discoverable (default: {DEFAULT_NOTIFIER})",
    )
    parser.add_argument(
        "--allow-no-notifier",
        action="store_true",
        help="pass when no notifier workflow can be discovered at all; without it "
        "an undiscoverable notifier is a failure, because a check that verifies "
        "nothing must not report success",
    )
    parser.add_argument(
        "--require-alert-priority",
        action="store_true",
        help="also fail a step that invokes a local composite action defaulting "
        "`priority:` without stating one — an omitted priority silently means the "
        "action's maximum",
    )
    # pre-commit is configured `pass_filenames: false`, but a consumer wiring the
    # module by hand may still hand it paths; discovery globs the tree either way.
    parser.add_argument("files", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    violations = check_repo(
        args.allow_no_notifier,
        args.notifier_pattern,
        args.notifier,
        args.require_alert_priority,
    )
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

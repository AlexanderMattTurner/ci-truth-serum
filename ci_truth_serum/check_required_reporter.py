#!/usr/bin/env python3
"""
Force every always() reporter on a gated workflow to declare whether it is a
required status check.

`check-always-reporter` guarantees a gated workflow carries one of two shapes
that fail closed when the gate itself fails: an `if: always()` reporter, or a
fail-closed twin. But the workflow YAML (which produces the check) and branch
protection (which decides whether the check blocks merges) drift independently:
a freshly added, green reporter silently escapes the required-status-check set,
and nothing in the repo records that it was meant to. This lint closes that gap.

For every workflow with a pull_request / pull_request_target trigger, each
`if: always()` reporter job — and each fail-closed twin
(`if: always() && needs.<gate>.result != 'success'`), which is the check run
branch protection reads when a gate fails — must carry an explicit
classification comment inside its job block:

    # required-check: true               -> must be a required status check
    # required-check: false  # <reason>  -> deliberately advisory (reason MANDATORY)

The comment must be trailing on the job's key line, or on its own line within
the job body. An unclassified reporter — or a `false` with no reason — fails.

A workflow whose only fail-closed shape is a twin needs the same comment on
each gated WORK job. The twin is SKIPPED on a healthy run, so the check runs
branch protection reads there are the work jobs' own. Leave one unclassified
and the ruleset requires nothing that a real failure can turn red.

This lint is the local, deterministic half of a pair: a consumer's apply
workflow derives the required-set from these `required-check: true` annotations
and syncs the branch-protection ruleset. It is opinionated — it assumes the
decide-job + always() reporter architecture. Any `if: always()` job (even a
cleanup job) demands a classification; mark such jobs `false` with a reason.

Opt the whole workflow out with "# not-required-check" on its pull_request:
trigger line (the same marker check-always-reporter honors).

A second, unrelated rule shares this file because both police the same
annotation: a job with a `uses:` key (a reusable-workflow call) may never
itself carry `# required-check: true`. GitHub reports that job's check run as
`<caller job name> / <called job name>`, never the job's own `name:` — the
name the apply workflow registers — so the ruleset would require a context
nothing reports and every PR would hang. The marker belongs on a thin
caller-local reporter job that `needs:` the call instead. Unlike the
reporter-classification rule above, this one applies to every workflow file:
the apply workflow reads `# required-check: true` from any job, on any
trigger, with no opt-out, so this check has the same unconditional scope.

A third rule polices the same annotation on a MATRIX job. A job whose `name:`
carries a `${{ matrix.X }}` reference registers one required context per
combination — `Test (3.11)`, `Test (3.12)`. GitHub posts those check runs only
when the job runs. Two things skip the job: its own `if:` closes, or a job it
`needs:` skips. Each one of those contexts then stays "Expected — Waiting for
status to be reported". The branch blocks forever, no check turns red, and no
log exists to read.

So a marked matrix job that can skip needs a sibling job. That sibling runs on
`if: always()` and carries the same `name:` template, and it covers the same
matrix, so it posts the same contexts on every run. A `uses:` job is no such
sibling, for the reason rule 2 gives. This rule reads the marker on every
workflow file, on any trigger, as rule 2 does.

"Can skip" reads a job's `if:` through `check_required_event_closure`'s
three-valued evaluator. A condition that is provably true on every event the
workflow declares — `if: github.event_name == 'pull_request'` on a workflow
that fires on nothing else — gates no run, so the job does not count as
skippable.

The rest is an over-approximation, because the evaluator binds the event facts
alone. A guard that never closes on the branch that matters —
`if: github.repository == '<owner>/<repo>'` — is skippable here and is not a
defect there. Answer such a case with `# matrix-context-ok: <reason>` on the
job's key line, or on one of its direct-child lines. The reason is mandatory,
and a bare marker suppresses nothing.

A fourth rule polices the other expressions a marked `name:` can hold. The
sync substitutes only `${{ matrix.* }}` when it builds a context. It registers
any other `${{ }}` — `${{ github.ref_name }}`, `${{ inputs.x }}` — as literal
text. GitHub evaluates every expression before it posts a check run. So no run
reports the registered context, on a skip or on a green run alike, and the
branch blocks with nothing red to read. A matrix value that is itself an
expression fails the same way: the sync substitutes the value as written, so
the expression moves into the registered context. This rule reads the marker
on every workflow file, as rules 2 and 3 do. It has no opt-out: the mismatch
is exact, not an over-approximation.

A fifth rule polices the mirror defect. A name that references a matrix key
the job's `strategy.matrix` never defines expands to zero contexts. The sync
then requires nothing for the job, so the marker silently stops gating merges
instead of blocking them. An axis with a dynamic value — `${{ fromJSON(...) }}`
— counts as defined, because only the run can expand it. Same scope as rules
2 through 4, and no opt-out.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    _classification_text,
    _job_blocks,
    _marked_jobs,
    declared_events,
    decide_gate_names,
    gated_work_jobs,
    has_fail_closed_twin,
    is_always_reporter,
    is_fail_closed_twin,
    expand_name,
    job_needs,
    unwrap_expression,
    MATRIX_REF,
    workflow_files as _workflow_files,
)
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_required_event_closure import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ExpressionError,
    _Parser,
    truth_of,
)

OPT_OUT = "not-required-check"
MARKER = "required-check"
ALLOW = "matrix-context-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
PR_TRIGGERS = ("pull_request", "pull_request_target")

# `# required-check: true|false` anywhere in a job block; group(rest) is the
# remainder of that source line, where a `false` must carry its `# <reason>`.
_CLASSIFY = re.compile(rf"#\s*{MARKER}\s*:\s*(true|false)\b(?P<rest>.*)")
# A non-empty trailing comment justifying an advisory classification.
_REASON = re.compile(r"#\s*\S")


def _locate_trigger(text: str, trigger: str) -> tuple[int, bool]:
    """Return (1-based line number, opted-out) for the first occurrence of trigger."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(rf"^\s*{trigger}\s*:", line):
            return num, annotated(line, OPT_OUT, require_reason=False)
    return 1, False


def _trigger_names(triggers: object) -> set[str]:
    """The set of trigger names an `on:` value declares, across every spelling
    (scalar / list / mapping) — so list-form `on: [pull_request, push]` is not
    silently skipped. Mirrors check_requires_concurrency's `_is_pr_triggered`."""
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list):
        return {t for t in triggers if isinstance(t, str)}
    if isinstance(triggers, dict):
        return {k for k in triggers if isinstance(k, str)}
    return set()


def _reporter_names(jobs: dict) -> list[str]:
    """Names of jobs that report for a gate: an always() reporter (bare or
    ${{ }}-wrapped) or a fail-closed twin. Both produce the check run that
    branch protection reads on a run whose gate failed, so both demand a
    classification."""
    return [
        name
        for name, cfg in jobs.items()
        if isinstance(cfg, dict)
        and (
            is_always_reporter(cfg.get("if", ""))
            or is_fail_closed_twin(cfg.get("if", ""))
        )
    ]


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every unclassified/under-justified reporter.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "required-check reporter classification — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    violations: list[tuple[int, str]] = []

    # One scan of the markers, read by three rules: the two below and the
    # twin-only branch further down.
    marked = _marked_jobs(blocks, jobs)

    # A marked job whose name: carries a matrix reference registers one context
    # per combination, and a skipped job posts none of them.
    events = declared_events(doc)
    for name, template in _unreportable_matrix_jobs(jobs, marked, blocks, events):
        line, _block = blocks.get(name, (1, ""))
        violations.append((line, _skippable_matrix_job(str(name), template)))

    # The marker rules below apply to every workflow file, regardless of trigger
    # or of the not-required-check opt-out further down: sync-required-checks
    # reads `# required-check: true` from every job in every workflow
    # (`_marked_jobs`, unfiltered by trigger), so a job carrying it poisons the
    # ruleset even off a pull_request trigger.
    for name in marked:
        cfg = jobs[name]
        line, _block = blocks.get(name, (1, ""))
        if "uses" in cfg:
            # Rule 4's static-name remedy cannot work on a uses: job — GitHub
            # names its check run '<caller> / <called>' whatever name: says —
            # so rule 2 owns the whole case.
            violations.append((line, _uses_job_required(name)))
            continue

        # The sync substitutes only ${{ matrix.* }} into a marked name, and
        # GitHub evaluates every expression before it posts a check run — any
        # other ${{ }} registers a context no run reports, green or skipped.
        # Both probes are needed: the template one catches a name whose dynamic
        # matrix expands to nothing, and the expanded one catches a matrix
        # VALUE that is itself an expression, which substitution moves into
        # the registered context.
        template = _name_template(str(name), cfg)
        stuck = [c for c in expand_name(template, _job_matrix(cfg)) if "${{" in c]
        if stuck or "${{" in MATRIX_REF.sub("", template):
            offending = stuck[0] if stuck else template
            violations.append((line, _unexpandable_name(str(name), offending)))

        # The mirror defect: a reference to an axis the matrix never defines
        # expands to ZERO contexts, so the sync requires nothing at all.
        missing = _missing_axes(cfg, template)
        if missing:
            violations.append((line, _no_axis_for_ref(str(name), template, missing)))

    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    names = _trigger_names(triggers)

    pr_line: int | None = None
    opted_out = False
    for trigger in PR_TRIGGERS:
        if trigger in names:
            line, out = _locate_trigger(text, trigger)
            if pr_line is None:
                pr_line = line
            if out:
                opted_out = True
    if pr_line is None or opted_out:
        return violations

    for name in _reporter_names(jobs):
        line, block = blocks.get(name, (pr_line, ""))
        defect = _classification_defect(block)
        if defect == "unclassified":
            violations.append((line, _unclassified(name)))
        elif defect == "no-reason":
            violations.append((line, _no_reason(name)))

    # A twin-only workflow reports through its WORK jobs on every healthy run,
    # because the twin is skipped there. Each work job therefore owes the same
    # classification the reporter owes.
    #
    # `has_always_reporter` is a SHAPE question, so an advisory `always()` job —
    # a cleanup, a notifier — answers it yes while aggregating nothing. Only a
    # reporter carrying `# required-check: true` is the aggregate that exempts
    # the work jobs; anything else leaves the hole this branch closes.
    gate_names = decide_gate_names(jobs)
    aggregate = [
        name
        for name, cfg in jobs.items()
        if name in marked
        and isinstance(cfg, dict)
        and is_always_reporter(cfg.get("if", ""))
    ]
    if gate_names and not aggregate and has_fail_closed_twin(jobs):
        for name in gated_work_jobs(jobs, gate_names):
            line, block = blocks.get(name, (pr_line, ""))
            defect = _classification_defect(block)
            if defect == "unclassified":
                violations.append((line, _unclassified_work(name)))
            elif defect == "no-reason":
                violations.append((line, _no_reason(name)))
    return violations


def _name_template(name: str, cfg: dict) -> str:
    """The check-run name GitHub posts for a job, before matrix substitution —
    the job's `name:`, or its key when it declares none. The same fallback
    `required_check_contexts` registers, so the two read one name."""
    return str(cfg.get("name", name))


def _never_closes(condition: str, events: frozenset[str]) -> bool:
    """True when CONDITION is provably true on every event the workflow declares.

    `if: github.event_name == 'pull_request'` on a workflow that fires on nothing
    else is such a condition: it gates no run, so the job it guards cannot skip
    on its own account. Anything the evaluator cannot prove — a `needs:` output,
    a `github.repository` guard, a status function — stays a skip, which is the
    conservative answer for a rule about a context that never reports.

    The three-valued evaluator is `check_required_event_closure`'s, which asks
    the mirror-image question (on which gating event is this condition provably
    FALSE). One evaluator answers both, so the package holds one notion of what
    a job condition means.
    """
    if not events:
        return False
    try:
        tree = _Parser(condition).parse()
    except ExpressionError:
        # An expression this parser does not recognize proves nothing, so the
        # job keeps its skip. The specific recovery a bare crash would deny.
        return False
    return all(truth_of(tree, {"github.event_name": event}) is True for event in events)


def _skippable_jobs(jobs: dict, events: frozenset[str]) -> set[str]:
    """Every job GitHub can leave unrun on a run of this workflow.

    Two ways in. A job's own `if:` closes, or a job it `needs:` skipped — GitHub
    skips a dependent job by default. `if: always()` closes both ways, which is
    what makes an always() reporter a stable required check.

    A FAILED dependency also skips the dependent, and that case is deliberately
    not counted: the run is red, so the merge waits on a failure somebody can
    read and fix. The skips counted here strand a required check on an all-green
    run, which is the defect this rule is about.

    The propagation is a fixpoint rather than a walk of the `needs:` graph: a
    workflow with a `needs:` cycle is one GitHub rejects, but this lint reads the
    file that has it, so it must answer instead of recurse.
    """
    configs = {str(name): cfg for name, cfg in jobs.items() if isinstance(cfg, dict)}
    always = {
        name for name, cfg in configs.items() if is_always_reporter(cfg.get("if", ""))
    }
    # `.lower()` because YAML resolves the bareword `if: true` to the boolean
    # True, which stringifies as "True" — the same unconditional job.
    conditions = {
        name: unwrap_expression(cfg.get("if", "")) for name, cfg in configs.items()
    }
    skippable = {
        name
        for name, condition in conditions.items()
        if name not in always
        and condition.lower() not in ("", "true")
        and not _never_closes(condition, events)
    }
    growing = True
    while growing:
        inherited = {
            name
            for name, cfg in configs.items()
            if name not in always and any(dep in skippable for dep in job_needs(cfg))
        }
        growing = not inherited <= skippable
        skippable |= inherited
    return skippable


def _job_matrix(cfg: dict) -> dict:
    """A job's `strategy.matrix`, or an empty one when it declares none."""
    strategy = cfg.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    return matrix if isinstance(matrix, dict) else {}


def _covering_sibling(jobs: dict, cfg: dict, template: str) -> bool:
    """True when another job posts TEMPLATE's contexts on every run.

    Such a sibling must satisfy three things. It runs on `if: always()`, so no
    run leaves it out. It carries the same `name:` template. And it covers the
    same matrix, which this reads two ways: the two `strategy.matrix` values are
    equal, or the sibling's expanded names include every one of CFG's.

    A `uses:` job never qualifies. GitHub reports a reusable-workflow call as
    `<caller job name> / <called job name>`, which is why `_uses_job_required`
    rejects the marker on such a job — a sibling of that shape would post
    different contexts and wave the marked job through.
    """
    wanted = set(expand_name(template, _job_matrix(cfg)))
    for name, sibling in jobs.items():
        if not isinstance(sibling, dict) or "uses" in sibling:
            continue
        if not is_always_reporter(sibling.get("if", "")):
            continue
        if _name_template(str(name), sibling) != template:
            continue
        matrix = _job_matrix(sibling)
        if matrix == _job_matrix(cfg):
            return True
        if wanted and wanted <= set(expand_name(template, matrix)):
            return True
    return False


def _unreportable_matrix_jobs(
    jobs: dict, marked: list[str], blocks: dict, events: frozenset[str]
) -> list[tuple[str, str]]:
    """(job key, name template) for every required matrix job that can skip and
    has no always() reporter posting its contexts."""
    skippable = _skippable_jobs(jobs, events)
    found = []
    for name in marked:
        cfg = jobs[name]
        template = _name_template(str(name), cfg)
        if not MATRIX_REF.search(template) or str(name) not in skippable:
            continue
        if _allowed(blocks.get(name, (0, ""))[1]) or _covering_sibling(
            jobs, cfg, template
        ):
            continue
        found.append((name, template))
    return found


def _allowed(block: str) -> bool:
    """True when the job's own lines carry `# matrix-context-ok: <reason>`.

    Same scope as the marker this annotation answers: the job's key line or one
    of its direct-child lines, never a step body.
    """
    return any(
        annotated(line, ALLOW) for line in _classification_text(block).split("\n")
    )


def _classification_defect(block: str) -> str | None:
    """'unclassified', 'no-reason', or None for a job block's marker comment."""
    match = _CLASSIFY.search(_classification_text(block))
    if match is None:
        return "unclassified"
    if match.group(1) == "false" and not _REASON.search(match.group("rest")):
        return "no-reason"
    return None


def _unclassified(name: str) -> str:
    return (
        f"reporter job '{name}' (always() reporter or fail-closed twin) is "
        "unclassified — a green reporter that "
        "nothing ties to branch protection silently escapes the required-check "
        f"set. Add '# {MARKER}: true' if it must be a required status check, or "
        f"'# {MARKER}: false  # <reason>' if it is deliberately advisory. Opt the "
        f"whole workflow out with '# {OPT_OUT}' on its pull_request: trigger."
    )


def _unclassified_work(name: str) -> str:
    return (
        f"gated work job '{name}' is unclassified, and this workflow fails closed "
        "through a twin rather than an always() reporter. The twin is SKIPPED on "
        "every healthy run, so the check run branch protection reads there is this "
        f"job's own. Add '# {MARKER}: true' if it must be a required status check, "
        f"or '# {MARKER}: false  # <reason>' if it is deliberately advisory."
    )


def _no_reason(name: str) -> str:
    return (
        f"job '{name}' is marked '# {MARKER}: false' but gives "
        "no reason — append '# <reason>' explaining why it is deliberately not a "
        "required check."
    )


def _uses_job_required(name: str) -> str:
    return (
        f"job '{name}' calls a reusable workflow (`uses:`) and is marked "
        f"'# {MARKER}: true' — GitHub reports that job's check run as "
        "'<caller job name> / <called job name>', never the job's own name:, so "
        "the ruleset would require a context nothing reports and every PR would "
        "hang. Move the marker to a thin caller-local reporter job that `needs:` "
        f"'{name}' instead."
    )


def _missing_axes(cfg: dict, template: str) -> set[str]:
    """The matrix keys TEMPLATE references that CFG's matrix can never bind.

    A key counts as defined when it is an axis of the job's `strategy.matrix`
    (whatever its value shape — a dynamic `${{ fromJSON(...) }}` axis is
    defined, only the run can expand it) or a key of an `include` entry. A
    dynamic `include:` binds keys only the run can know, so it too defines
    every reference. A whole-matrix expression (`matrix: ${{ ... }}`) can bind
    any key, so nothing is missing there. With no matrix at all, every
    reference is.
    """
    refs = set(MATRIX_REF.findall(template))
    if not refs:
        return set()
    strategy = cfg.get("strategy")
    raw = strategy.get("matrix") if isinstance(strategy, dict) else None
    if isinstance(raw, dict):
        include = raw.get("include") or []
        if not isinstance(include, list):
            # A dynamic `include:` binds keys only the run can know, exactly as
            # a dynamic axis does, so nothing here is provably missing.
            return set()
        defined = {k for k in raw if k not in ("include", "exclude")}
        for inc in include:
            if isinstance(inc, dict):
                defined |= set(inc)
        return refs - defined
    return refs if raw is None else set()


def _unexpandable_name(name: str, offending: str) -> str:
    return (
        f"job '{name}' is marked '# {MARKER}: true' but registers the check "
        f"context '{offending}', which still holds a "
        "${{ }} expression. sync-required-checks substitutes only "
        "${{ matrix.* }}, with the matrix values as written in the workflow, "
        "so every other expression stays in the registered text. GitHub "
        "evaluates every expression before it posts a check run, so no run — "
        "green or skipped — ever reports that context, and the branch blocks "
        "with nothing red to read. Use a static name and static matrix values "
        "(matrix references are fine), or move the marker to a static-named "
        f"always() reporter that needs: '{name}'."
    )


def _no_axis_for_ref(name: str, template: str, missing: set[str]) -> str:
    keys = ", ".join(sorted(missing))
    return (
        f"job '{name}' is marked '# {MARKER}: true' but its name '{template}' "
        f"references matrix key(s) [{keys}] that the job's strategy.matrix "
        "never defines. The sync can expand no context from that name, so it "
        "requires NOTHING for this job — the marker silently stops gating "
        "merges instead of blocking them. Fix the reference or add the axis; "
        "a dynamic axis (a "
        "${{ fromJSON(...) }} value) counts as defined."
    )


def _skippable_matrix_job(name: str, template: str) -> str:
    return (
        f"job '{name}' is marked '# {MARKER}: true' and its name '{template}' "
        "carries a ${{ matrix.* }} reference, but the job can skip — through its "
        "own if:, or through a job it needs:. The ruleset requires one context "
        "per matrix combination. A skipped job posts none of them, so each "
        "context stays 'Expected — Waiting for status to be reported' and the "
        "branch blocks with nothing red to read. Make the job unconditional and "
        "gate its steps instead. Or add a sibling job with 'if: always()', the "
        f"same name '{template}' and the same matrix, which posts those contexts "
        f"on every run. If the condition never closes on the protected branch, "
        f"add '# {ALLOW}: <reason>' to the job instead."
    )


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "An unclassified always() reporter silently escapes the "
            "required-status-check set."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

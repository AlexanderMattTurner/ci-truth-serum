#!/usr/bin/env python3
"""Fail a diagnostics step that GitHub skips on the run it is there to explain.

A job that collects evidence writes the evidence out at the end: an
`actions/upload-artifact` step holding the logs, or a `run:` step calling a
script such as `.github/scripts/collect-diagnostics.sh`. Almost every one of
them is gated `if: failure()`, because the evidence is only interesting when
something went wrong.

`failure()` is false when the run is CANCELLED, and cancellation is how GitHub
reports the failures the evidence matters most for. A job that passes its
`timeout-minutes` ends **cancelled**, not failed. So does a job a
`concurrency:` group supersedes, and a job a human stops. The step gated
`failure()` reads "skipped" in the run summary, the artifact never uploads, and
the one log line that survives is "The operation was canceled."

Worked case from the repository this lint came out of. A Playwright job had a
20-minute budget and an `if: failure()` upload of `playwright-report/`. A slow
run took 21 minutes. GitHub cancelled it, the upload was skipped, and the report
that held the failing trace was destroyed with the runner. Three people re-ran
the job over two days to try to reproduce a hang that only appeared under load.
`if: always()` on that one step would have kept the report from the first run.

WHAT THIS CHECK ASKS. The condition on a diagnostics step must NAME
cancellation. `always()` names it, and so does a plain `cancelled()`:

    if: always()                    # runs on success, failure and cancel
    if: failure() || cancelled()    # the two red outcomes, no success upload

WHAT COUNTS AS A DIAGNOSTICS STEP, and nothing else does:

  * a step that uploads an artifact (`actions/upload-artifact`, or a fork or
    local action whose name ends the same way);
  * a step that runs a script whose file name says it collects evidence —
    `collect-logs.sh`, `dump-artifacts.py`, `diagnostics.sh`, `triage.mjs`;
  * a `uses: ./…` local action whose directory name says the same.

The `run:` body is read on the real bash grammar (`_bash_ast`), so a script name
inside a message a command prints, or inside a heredoc, is text and not a call.
See `.claude/rules/shell-lint-parsing.md`.

A GitHub `if:` expression is not YAML and GitHub ships no grammar for it, so the
condition is read with a regex, the way `check_conclusion_coverage` and
`check_multi_cron_gating` read theirs. The check reads a MENTION of
cancellation, not the truth value of the whole expression: it cannot know what
`steps.x.outcome` holds. A negated mention does not count, because
`failure() && !cancelled()` still cannot run on a cancel.

The job's own `if:` is judged too. A gate at the job level skips every step
under it, so a diagnostics job gated `if: failure()` loses its artifacts in
exactly the same way. Both gates have to let the step run, so either one can
lose the evidence on its own: a step gated `always()` inside a job gated
`failure()` never starts. The message names the step's own condition first,
because that is the one its author wrote for this step.

This lint is opinionated (Tier 2): it prescribes one gate for a class of loss
that never goes red. The step is skipped, the job is cancelled, and no check
reports that the evidence is gone.

Opt out with `# failure-only-diagnostics-ok: <reason>` on the flagged step, or
in the comment block directly above it. The reason is REQUIRED; a bare
annotation does not suppress. The annotation is scoped to the ONE step it sits
on, so a job that keeps a deliberate `failure()` upload still gets a finding on
its next diagnostics step.
"""

import re
import sys
from pathlib import Path, PurePosixPath

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    LineLoader,
    MESSAGE_PREFIX,
    workflow_files,
    _job_blocks,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
OPT_OUT = "failure-only-diagnostics-ok"

MESSAGE = (
    "{where} is gated on failure() and never names cancellation, and the step "
    "{what}. A job that passes its timeout-minutes ends 'cancelled', not "
    "'failed' — so does a job a concurrency group supersedes, and one a human "
    "stops. failure() is false on all three, the step reads 'skipped', and the "
    "evidence dies with the runner. Gate it on `always()`, or on "
    "`failure() || cancelled()` to keep the success runs quiet, or annotate "
    f"'# {OPT_OUT}: <reason>'."
)

# GitHub's status functions, read out of an `if:` scalar. `always()` runs the
# step on every conclusion, cancellation included; `cancelled()` names it
# outright. A `!` in front of either flips it, and a negated mention leaves the
# step just as unable to run on a cancel — hence the captured `neg` group.
_FAILURE = re.compile(r"(?<![\w.])failure\s*\(\s*\)")
_CANCELLED = re.compile(r"(?P<neg>!\s*)?(?<![\w.])cancelled\s*\(\s*\)")
_ALWAYS = re.compile(r"(?P<neg>!\s*)?(?<![\w.])always\s*\(\s*\)")

# An action that writes a run's files out where a human can read them after the
# runner is gone. Matched on the action path with its ref stripped, so a fork
# (`my-org/upload-artifact`) and the sub-path form both count.
_UPLOAD_ACTION = re.compile(r"(?:^|/)upload-(?:pages-)?artifacts?(?:/|$)")

# A file name that says the thing it runs exists to collect evidence. Read on a
# normalized base name (lower case, every run of non-alphanumerics folded to one
# `-`, the extension dropped), so `collect_logs.sh` and `collect-logs.py` are
# one pattern rather than four.
_DIAGNOSTIC_NAME = re.compile(
    r"(?:^|-)(?:"
    r"diagnostics?"
    r"|post-?mortem"
    r"|triage"
    r"|(?:collect|dump|gather|capture|save|archive|upload)"
    r"-(?:logs?|artifacts?|traces?|diagnostics?|reports?)"
    r")(?:-|$)"
)


def covers_cancellation(condition: object) -> bool:
    """True when CONDITION can be true on a cancelled run.

    A mention of `always()` or of `cancelled()` is the evidence that the author
    decided what happens on a cancel. A negated mention is not: `!cancelled()`
    and `failure() && !cancelled()` both keep the step off the cancelled run.
    """
    text = str(condition)
    return any(
        match.group("neg") is None
        for pattern in (_CANCELLED, _ALWAYS)
        for match in pattern.finditer(text)
    )


def gated_on_failure(condition: object) -> bool:
    """True when CONDITION calls `failure()` and cannot run on a cancel."""
    if condition is None:
        return False
    return bool(_FAILURE.search(str(condition))) and not covers_cancellation(condition)


def _normalize(token: str) -> str:
    """The base name of TOKEN, extension dropped, folded to lower-case words
    joined by single hyphens — the one spelling `_DIAGNOSTIC_NAME` reads."""
    name = PurePosixPath(token.strip()).name
    stem = name.split(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")


def _script_names(script: str) -> list[str]:
    """The evidence-collecting scripts SCRIPT calls, named for the message.

    Every word of every command is a candidate, so a call reached through an
    interpreter (`bash .github/scripts/collect-logs.sh`) is read at the script
    rather than at `bash`. A command whose first word only prints text holds no
    call at all — its arguments are the message.
    """
    found: list[str] = []
    for command in iter_nodes(parse(script), "command"):
        tokens = [unquote(node_text(node)) for node in command_arguments(command)]
        if not tokens or MESSAGE_PREFIX.match(tokens[0]):
            continue
        found += [
            PurePosixPath(token).name
            for token in tokens
            if _DIAGNOSTIC_NAME.search(_normalize(token))
        ]
    return found


def diagnostic_work(step: dict) -> str | None:
    """What STEP does with a run's evidence, phrased for the message, or None
    when the step collects none."""
    uses = step.get("uses")
    if isinstance(uses, str):
        action = uses.split("@", 1)[0].strip().rstrip("/")
        if _UPLOAD_ACTION.search(action):
            return f"uploads an artifact through {action}"
        if action.startswith(".") and _DIAGNOSTIC_NAME.search(_normalize(action)):
            return f"runs the diagnostics action {action}"
    script = step.get("run")
    if isinstance(script, str):
        names = _script_names(script)
        if names:
            return f"runs {', '.join(sorted(set(names))[:3])}"
    return None


def _steps(container: object) -> list[dict]:
    steps = container.get("steps") if isinstance(container, dict) else None
    return (
        [step for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else []
    )


def _containers(doc: dict) -> list[tuple[str, dict]]:
    """The (name, mapping) pairs that hold steps: every job of a workflow, or
    the `runs:` block of a composite action."""
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        return [(str(name), job) for name, job in jobs.items() if isinstance(job, dict)]
    runs = doc.get("runs")
    return [("runs", runs)] if isinstance(runs, dict) else []


def _span_ends(steps: list[dict], last_line: int) -> dict[int, int]:
    """The last line of each step's block, keyed by the step's own line.

    A step ends where the next one starts, and the final step ends at LAST_LINE
    — the end of the job's block in a workflow, the end of the file in a
    composite action. The span is what an opt-out may be written inside.
    """
    starts = sorted(
        line for step in steps if isinstance(line := step.get("__line__"), int)
    )
    ends = {start: nxt - 1 for start, nxt in zip(starts, starts[1:])}
    if starts:
        ends[starts[-1]] = last_line
    return ends


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every diagnostics step this workflow or composite
    action skips on a cancelled run.

    A file that cannot be parsed as YAML is itself reported as a violation
    rather than passed as clean, so a syntax error can never read as "every
    diagnostics step here survives a cancel"."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                1,
                f"could not parse as YAML ({first_line}); cannot check whether its "
                "diagnostics steps survive a cancelled run — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    lines = text.splitlines()
    blocks = _job_blocks(text)
    violations: list[tuple[int, str]] = []
    for name, container in _containers(doc):
        steps = _steps(container)
        if not steps:
            continue
        key_line, block = blocks.get(name, (1, ""))
        block_end = (
            key_line + len(block.splitlines()) - 1 if block else max(len(lines), 1)
        )
        ends = _span_ends(steps, block_end)
        violations += _check_steps(steps, container, name, lines, ends, key_line)
    return violations


def _check_steps(
    steps: list[dict],
    container: dict,
    name: str,
    lines: list[str],
    ends: dict[int, int],
    key_line: int,
) -> list[tuple[int, str]]:
    """The findings of one job or composite `runs:` block."""
    job_if = container.get("if")
    found: list[tuple[int, str]] = []
    for step in steps:
        work = diagnostic_work(step)
        if work is None:
            continue
        line = step.get("__line__")
        line = line if isinstance(line, int) else key_line
        label = step.get("name")
        # Both gates have to let the step run, so either one can lose the
        # evidence on its own: a step gated `always()` inside a job gated
        # `failure()` never starts at all. The step's own condition is named
        # first, because that is the one its author wrote for this step.
        gates = (
            (
                step.get("if"),
                f"step '{label}'" if isinstance(label, str) else "this step",
            ),
            (job_if, f"the '{name}' job, which holds this step,"),
        )
        gate = next(((c, w) for c, w in gates if gated_on_failure(c)), None)
        if gate is None:
            continue
        if annotated_near(lines, line, OPT_OUT, span_end=ends.get(line)):
            continue
        found.append((line, MESSAGE.format(where=gate[1], what=work)))
    return found


def main() -> int:
    total = 0
    for path in workflow_files(WORKFLOWS_DIR, ACTIONS_DIR):
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails LOUDLY: skipping it
            # would false-green exactly the input this lint exists to read.
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            print(f"::error file={rel},line={line}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "A diagnostics step gated on failure() alone is skipped when the run "
            "is cancelled, which is how a timeout ends."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Flag `github.event.pull_request.head.sha` in a step's `run:` or `with:` value.

The event payload is frozen at trigger time: `github.event.pull_request.head.sha`
is the head commit as it stood when the workflow was *queued*, not the commit the
job actually checked out. A force-push or the routine autofix-amend (which
rewrites the PR head and force-pushes) moves the real head after the trigger
fires, so a step that scopes a diff/range to that stale SHA silently mis-scopes:
`git diff <frozen>...HEAD` spans the whole branch history (the frozen SHA is no
longer an ancestor), and `actions/checkout` `ref: <frozen>` fetches a commit that
may no longer exist. The correct head is derived from the checkout itself —
`git rev-parse HEAD` after `actions/checkout` — never from the event payload.

This lint scans every workflow/composite-action step's inline `run:` script and
its `with:` input values for the frozen expression and fails on each hit. Only
`head.sha` is matched: `github.event.pull_request.base.sha` (the correct base for
a range) and `github.event.pull_request.head.ref` (a branch name, re-resolved on
checkout) are legitimate and untouched.

A genuine use — pinning `--force-with-lease=<ref>:<frozen>` so a concurrent push
rejects the amend, where the *point* is to compare against the pre-trigger head —
opts out with a `# frozen-head-ok: <reason>` comment anywhere in the step block.
The reason is mandatory.

An `env:` value binding the frozen SHA is judged by what CONSUMES it, not by the
binding. Routing the expression through `env:` is the shape the template-
injection lints demand, so most bindings are correct and flagging them all is
noise: on a 50-workflow tree, 41 of the 45 uses sit under `env:` and none is a
defect. The lint therefore follows the variable and fires only where the frozen
value mis-scopes — as an endpoint of a `git` revision range (`$SHA...HEAD`), or
as `actions/checkout`'s `ref:`. Those are the two positions the direct rule above
already bans, so an env var is no longer a one-hop route around a Tier 1 check.

Reading the range endpoint is a structural question, so the `run:` script is
parsed with the bash grammar and only a `git` command's own words are judged
(`.claude/rules/shell-lint-parsing.md`). A range quoted inside a message a
command prints names no `git` command, so it is text and stays silent.

Globs every workflow + composite action like check_inline_run_length; argv is
ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import PathologicalInputError  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _bash_ast import command_name  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _bash_ast import command_words  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _bash_ast import iter_nodes  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _bash_ast import parse  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import LineLoader as _LineLoader  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import annotation_re  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOW = "frozen-head-ok"

# The frozen head-SHA context. `\b` after `sha` keeps `head.sha` from also
# matching a hypothetical `head.sha_short`; `base.sha` and `head.ref` never match.
FROZEN = re.compile(r"github\.event\.pull_request\.head\.sha\b")
# The opt-out: `# frozen-head-ok: <reason>` with a non-empty reason after the colon.
# Built by the shared matcher, not hand-rolled: this searches a whole multi-line
# job block, and a hand-spelled `\s*` reason gap crosses a NEWLINE — so a bare
# `# frozen-head-ok:` at end of line adopted the next line's first character as
# its "reason" and suppressed the lint with an empty claim.
_ALLOW_RE = annotation_re(ALLOW)

_MESSAGE = (
    "step uses github.event.pull_request.head.sha in run:/with: — the event "
    "payload is frozen at trigger time, so a force-push / autofix-amend moves the "
    "real head and this mis-scopes the range (git diff <frozen>...HEAD spans the "
    "whole branch; checkout ref:<frozen> fetches a stale/absent commit). Derive "
    "the head from the checkout instead (git rev-parse HEAD after actions/checkout)."
    f" If comparing against the pre-trigger head is the point (e.g. a "
    f"--force-with-lease pin), annotate the step with '# {ALLOW}: <reason>'."
)


_ENV_MESSAGE = (
    "env var {name} binds the frozen event head SHA, and this step spends it "
    "{position}. The payload SHA is frozen at trigger time, so a force-push / "
    "autofix-amend moves the real head: the range spans the whole branch, and "
    "the checkout fetches a stale or absent commit. Derive the head from the "
    "checkout instead (git rev-parse HEAD after actions/checkout). If comparing "
    "against the pre-trigger head is the point, annotate the step with "
    f"'# {ALLOW}: <reason>'."
)

# The shells that are a different LANGUAGE, where parsing the script as bash
# would report on text the grammar never understood. Named as an exclusion, not
# as an allowlist of bash spellings: a custom template
# (`bash --noprofile --norc -eo pipefail {0}`) is still bash, and an allowlist
# would skip it — a fail-open on the one shape an author writes deliberately.
_NON_BASH_SHELLS = frozenset({"python", "pwsh", "powershell", "cmd"})


def _shell_ref(name: str) -> str:
    """The regex source for a shell reference to NAME — `$NAME` or `${NAME}`. The
    negative lookahead stops `$HEAD_SHA_BASE` from matching `HEAD_SHA`, which
    would report a different variable."""
    escaped = re.escape(name)
    return rf"\$(?:{escaped}(?![A-Za-z0-9_])|\{{{escaped}\}})"


def _range_use(name: str) -> "re.Pattern[str]":
    """Matches NAME spent as an endpoint of a git revision range — `$NAME...X` or
    `X..${NAME}`."""
    ref = _shell_ref(name)
    return re.compile(rf"{ref}\s*\.{{2,3}}|\.{{2,3}}\s*{ref}")


def _env_names_binding_frozen(scope: object) -> set[str]:
    """The names SCOPE's `env:` mapping binds to the frozen head SHA."""
    env = scope.get("env") if isinstance(scope, dict) else None
    if not isinstance(env, dict):
        return set()
    return {
        str(name)
        for name, value in env.items()
        if isinstance(value, str) and FROZEN.search(value)
    }


def _git_range_words(script: str) -> list[str]:
    """Every word of every `git` command in SCRIPT. Only a `git` command's own
    words can carry a revision range, so a range inside a printed message or a
    heredoc body — neither of which is a `git` command — is never returned."""
    words: list[str] = []
    for command in iter_nodes(parse(script), "command"):
        if command_name(command) == "git":
            words += command_words(command)
    return words


def _spends_in_range(step: dict, name: str) -> bool:
    """True when STEP's shell spends NAME as a git revision-range endpoint."""
    script = step.get("run")
    if not isinstance(script, str):
        return False
    interpreter = str(step.get("shell", "bash")).split()[:1]
    if interpreter and interpreter[0] in _NON_BASH_SHELLS:
        return False
    pattern = _range_use(name)
    return any(pattern.search(word) for word in _git_range_words(script))


def _spends_as_checkout_ref(step: dict, name: str) -> bool:
    """True when STEP checks out the commit NAME names."""
    if "actions/checkout" not in str(step.get("uses", "")):
        return False
    inputs = step.get("with")
    ref = inputs.get("ref") if isinstance(inputs, dict) else None
    if not isinstance(ref, str):
        return False
    # A `with:` value is an expression, never shell, so `${{ env.NAME }}` is the
    # only spelling that reaches the variable here.
    return bool(re.search(rf"env\.{re.escape(name)}(?![A-Za-z0-9_])", ref))


def _env_scopes(doc: dict) -> list[tuple[object, list[dict]]]:
    """(scope, steps the scope's `env:` reaches) for every level that can declare
    one: the workflow, each job, each step, and a composite action's `runs`."""
    scopes: list[tuple[object, list[dict]]] = []
    all_steps = _all_steps(doc)
    scopes.append((doc, all_steps))
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict):
                scopes.append((job, _iter_steps(job)))
    runs = doc.get("runs")
    if isinstance(runs, dict):
        scopes.append((runs, _iter_steps(runs)))
    scopes += [(step, [step]) for step in all_steps]
    return scopes


def _step_candidates(step: dict) -> list[str]:
    """Every string a step exposes to this lint: its inline `run:` script and each
    scalar value under `with:`."""
    candidates: list[str] = []
    run = step.get("run")
    if isinstance(run, str):
        candidates.append(run)
    with_inputs = step.get("with")
    if isinstance(with_inputs, dict):
        candidates += [v for v in with_inputs.values() if isinstance(v, str)]
    return candidates


def _step_block(lines: list[str], start_1based: int) -> str:
    """The source lines of the step beginning at START_1BASED: that line plus every
    following line indented deeper than it (blank lines included), stopping at the
    next line indented the same or shallower — i.e. the next list item / sibling
    key. Used to find an opt-out comment scoped to the offending step, including a
    `#` comment trailing a `with:` value that PyYAML would have discarded."""
    i = start_1based - 1
    if i < 0 or i >= len(lines):
        return ""
    base_indent = len(lines[i]) - len(lines[i].lstrip())
    block = [lines[i]]
    j = i + 1
    while j < len(lines):
        line = lines[j]
        if line.strip() and (len(line) - len(line.lstrip())) <= base_indent:
            break
        block.append(line)
        j += 1
    return "\n".join(block)


def _iter_steps(container: object) -> list[dict]:
    """The step dicts of a job/composite-action `steps:` list."""
    steps = container.get("steps") if isinstance(container, dict) else None
    if not isinstance(steps, list):
        return []
    return [s for s in steps if isinstance(s, dict)]


def _all_steps(doc: dict) -> list[dict]:
    """Every step across all jobs plus a composite action's `runs.steps`."""
    steps: list[dict] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job in jobs.values():
            steps += _iter_steps(job)
    steps += _iter_steps(doc.get("runs"))
    return steps


def find_violations(text: str) -> list[tuple[int | None, str]]:
    """(line, message) for every step whose `run:`/`with:` uses the frozen head
    SHA without an opt-out. An unparseable workflow is reported as a violation
    (line None) rather than passed as clean — a false-green on the file under test."""
    try:
        doc = yaml.load(text, Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify frozen "
                "head-SHA usage — fix the syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []

    lines = text.splitlines()
    violations: list[tuple[int | None, str]] = []
    flagged: set[int | None] = set()

    def report(step: dict, message: str) -> None:
        """Record one finding for STEP unless its block opts out, or unless the
        step already carries one — a step spending the SHA directly AND through
        an env var has one edit to make, so it earns one line."""
        line = step.get("__line__")
        block = _step_block(lines, line) if isinstance(line, int) else ""
        if _ALLOW_RE.search(block) or line in flagged:
            return
        flagged.add(line)
        violations.append((line, message))

    for step in _all_steps(doc):
        if any(FROZEN.search(c) for c in _step_candidates(step)):
            report(step, _MESSAGE)

    for scope, steps in _env_scopes(doc):
        for name in sorted(_env_names_binding_frozen(scope)):
            for step in steps:
                if _spends_in_range(step, name):
                    report(
                        step, _ENV_MESSAGE.format(name=name, position="in a git range")
                    )
                elif _spends_as_checkout_ref(step, name):
                    report(
                        step,
                        _ENV_MESSAGE.format(name=name, position="as a checkout ref"),
                    )
    return violations


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every frozen-head-SHA violation in PATH."""
    return find_violations(path.read_text(encoding="utf-8"))


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            # Loud, never a silent pass: a `run:` the grammar refuses is a file
            # this lint did not read, and reporting it clean would be the false
            # green the pack exists to catch.
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} frozen head-SHA usage(s) found.")
        print(
            "The frozen event SHA mis-scopes diff ranges after a force-push; "
            "derive the head from the checkout (git rev-parse HEAD)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

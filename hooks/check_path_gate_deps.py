#!/usr/bin/env python3
"""Verify a decide-gated job's path filters cover every file the job depends on.

Repos using this pack gate expensive jobs behind a `decide` job (a call to
`decide-reusable.yaml`) plus `if: needs.decide.outputs.run == 'true'`. The
decide call declares its change filter in one of two shapes: a `filters:` spec
of dorny/paths-filter glob groups, or a `paths-regex:` single extended-regex
(ERE) string matched at runtime by `grep -qE` against the changed-file list
(an empty `paths-regex` is a deliberately keyword-only gate — path coverage is
not applicable, so nothing is ever reported uncovered for it). When the filter
omits a file the gated job actually depends on, a PR changing only that file
skips the job and the `always()` reporter goes green — a fail-open exactly when
the dependency changed. That has recurred (a composite action omitted from
every filter; a helper script omitted from a test gate) despite the rule being
documented, because nothing enforced it.

For each workflow with the decide pattern, this lint computes each gated job's
static dependencies and demands the union of the filter globs of every decide
job the gate references cover them:

  * every LOCAL composite action a step `uses: ./<dir>` — the whole dir (a
    `uses:` naming a dir absent from disk is its own hard error);
  * every `.github/scripts/…` path a `run:` body mentions, plus ONE level of
    transitivity — a referenced shell script (`.sh`/`.bash`) on disk is scanned
    for further `.github/scripts/…` references (which covers `source`/`.`
    inclusions, whose targets live there);
  * every path declared with a `# gate-deps: <path> [<path>…]` comment — the
    escape valve for semantic deps static analysis can't see (e.g. `bin/` when
    the gated tests execute host scripts). Attachment rule: the comment counts
    when it appears anywhere inside the gated job's OR its decide job's source
    block — the job key line through the last line indented deeper than it
    (`_job_blocks` scoping, shared with the required-check lint).

A dependency is covered when every git-tracked file under it matches at least
one filter glob — partial coverage still fails open for the unmatched files, so
they are reported. Globs are matched the way decide-reusable's dorny/paths-filter
applies them: against the full repo-relative path, `**` crossing `/` boundaries,
`*`/`?` confined to one segment, dotfiles matched.

Suppress a finding for one dependency with a `# path-gate-ok: <dep> <reason>`
comment inside the gated job's block; the reason is mandatory. Workflows without
the decide pattern are skipped silently. Globs every workflow like the other
workflow lints; the passed file list is ignored.

The hook is registered `always_run` (not filtered to workflow files): coverage
is a point-in-time property of the whole tree, and it can break from the
DEPENDENCY side — a new file added under a composite action's dir or a declared
`# gate-deps:` dir violates "every tracked file under the dep is matched"
without any workflow file changing, which a files-filtered hook would never
re-check. The full scan is cheap (YAML parsing plus regex matching).
"""

import re
import subprocess
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    WORKFLOW_GLOBS,
    LineLoader as _LineLoader,
    _job_blocks,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

DECIDE_WORKFLOW = "decide-reusable.yaml"
OPT_OUT = "path-gate-ok"

# `# gate-deps: <path> [<path>…]` — human-declared dependencies inside a job block.
_GATE_DEPS = re.compile(r"#\s*gate-deps:\s*(?P<paths>\S.*?)\s*$", re.MULTILINE)
# `# path-gate-ok: <dep> <reason>` — per-dependency suppression; reason mandatory.
_PATH_GATE_OK = re.compile(
    rf"#\s*{OPT_OUT}:\s*(?P<dep>\S+)[ \t]*(?P<reason>[^\n]*)", re.MULTILINE
)
# A `.github/scripts/…` path wherever it appears in a run body or shell script.
_SCRIPT_REF = re.compile(r"\.github/scripts/[A-Za-z0-9._/-]+")
# `needs.<decide-job>.outputs.run` inside a gated job's `if:`.
_GATE_REF = re.compile(r"needs\.(?P<job>[\w-]+)\.outputs\.run")


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Compile one dorny/paths-filter glob into an anchored full-path regex.

    Mirrors the action's micromatch semantics on the subset these filters use:
    `**/` matches zero or more whole segments, a trailing/bare `**` matches
    anything across segments, `*`/`?` stay within one segment, `[…]` classes
    pass through (`[!…]` negates), and dotfiles are matched (dot: true).
    """
    out = ["^"]
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**/", i) and (i == 0 or pattern[i - 1] == "/"):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 2)  # skip a leading ] / ! inside the class
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                body = pattern[i + 1 : end]
                if body.startswith("!"):
                    body = "^" + body[1:]
                out.append(f"[{body}]")
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    out.append("$")
    return re.compile("".join(out))


def filter_patterns(filters_value: object) -> list[str]:
    """Every glob string in a decide job's `filters:` spec, across all groups.

    The gate opens when ANY group matches (`decide-reusable.yaml` sets `run` to
    'true' whenever `changes != '[]'`), so coverage is the union of all groups'
    patterns. A dorny change-type entry (`- added|modified: 'x'`) contributes its
    pattern value(s).
    """
    spec = yaml.safe_load(filters_value) if isinstance(filters_value, str) else None
    if not isinstance(spec, dict):
        return []
    patterns: list[str] = []
    for group in spec.values():
        for item in group if isinstance(group, list) else []:
            if isinstance(item, str):
                patterns.append(item)
            elif isinstance(item, dict):
                for value in item.values():
                    if isinstance(value, str):
                        patterns.append(value)
                    elif isinstance(value, list):
                        patterns += [v for v in value if isinstance(v, str)]
    return patterns


def is_decide_job(job: object) -> bool:
    """True for a job calling decide-reusable.yaml with a `filters:` or
    `paths-regex:` input (the two change-filter shapes decide-reusable accepts)."""
    if not isinstance(job, dict):
        return False
    uses = str(job.get("uses", "")).partition("@")[0]
    with_ = job.get("with")
    return (
        uses.endswith(DECIDE_WORKFLOW)
        and isinstance(with_, dict)
        and (
            isinstance(with_.get("filters"), str)
            or isinstance(with_.get("paths-regex"), str)
        )
    )


def decide_matchers(with_: dict) -> list[re.Pattern[str]]:
    """The file matchers for one decide job, as the union of its declared shapes.

    `filters:` globs translate through `glob_to_regex`. A `paths-regex:` ERE
    string is compiled directly (matched with `.search()`, mirroring runtime
    `grep -qE`): an empty string is a deliberately keyword-only gate, so path
    coverage is not applicable — it becomes a match-everything matcher
    (`re.compile("")`, whose `.search` matches any string) so no dependency is
    reported uncovered. An unresolved `${{ inputs.paths-regex }}` expression
    compiles to a literal that matches no real path — fail-closed and correct,
    so the author must statically cover the deps or suppress.
    """
    matchers = [glob_to_regex(p) for p in filter_patterns(with_.get("filters"))]
    regex = with_.get("paths-regex")
    if isinstance(regex, str):
        matchers.append(re.compile(regex.strip()))
    return matchers


def gate_refs(job: dict, decide_ids: set[str]) -> set[str]:
    """The decide jobs a job is gated on: named in `needs:` AND referenced as
    `needs.<id>.outputs.run` in its `if:`."""
    needs = job.get("needs")
    needed = {needs} if isinstance(needs, str) else set(needs or [])
    referenced = set(_GATE_REF.findall(str(job.get("if", ""))))
    return needed & referenced & decide_ids


def _normalize(dep: str) -> str:
    return dep.removeprefix("./").rstrip("/")


def _run_scripts(run: object) -> list[str]:
    # Strip a trailing `.` the greedy capture pulls off a sentence-ending
    # comment (`# … .github/scripts/foo.sh.`) — no real path ends in a dot.
    return (
        [s.rstrip(".") for s in _SCRIPT_REF.findall(run)]
        if isinstance(run, str)
        else []
    )


def job_dependencies(job: dict, read_repo_file) -> tuple[list[str], list[str]]:
    """(dependency paths, missing local composite dirs) statically derivable from
    a gated job's steps. READ_REPO_FILE(rel) returns a repo-relative file's text
    or None when absent.

    Composite deps are the `uses: ./<dir>` directory; script deps are every
    `.github/scripts/…` token in a `run:` body, followed one transitive hop —
    a referenced `.sh`/`.bash` script on disk is itself scanned for further
    `.github/scripts/…` references (`source`/`.` inclusions live there).
    """
    deps: dict[str, None] = {}
    missing: list[str] = []
    steps = job.get("steps")
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict):
            continue
        uses = str(step.get("uses", "")).strip()
        if uses.startswith("./"):
            dep = _normalize(uses)
            # A local composite must carry an action.yml/action.yaml on disk.
            if any(read_repo_file(f"{dep}/{f}") for f in ("action.yml", "action.yaml")):
                deps.setdefault(dep)
            else:
                missing.append(dep)
        for script in _run_scripts(step.get("run")):
            deps.setdefault(script)
            if not script.endswith((".sh", ".bash")):
                continue
            content = read_repo_file(script)
            for nested in _run_scripts(content or ""):
                deps.setdefault(nested)
    return list(deps), missing


def declared_deps(*blocks: str) -> list[str]:
    """Paths declared via `# gate-deps:` comments across the given job blocks."""
    deps: dict[str, None] = {}
    for block in blocks:
        for match in _GATE_DEPS.finditer(block):
            for path in match.group("paths").split():
                deps.setdefault(_normalize(path))
    return list(deps)


def suppressions(block: str) -> tuple[dict[str, str], list[str]]:
    """(dep → reason, deps suppressed without a reason) from `# path-gate-ok:`
    comments in a gated job's block."""
    with_reason: dict[str, str] = {}
    reasonless: list[str] = []
    for match in _PATH_GATE_OK.finditer(block):
        dep = _normalize(match.group("dep"))
        reason = match.group("reason").strip()
        if reason:
            with_reason[dep] = reason
        else:
            reasonless.append(dep)
    return with_reason, reasonless


def tracked_files(dep: str) -> list[str]:
    """The git-tracked files under DEP (itself, when DEP is a file), asked of the
    repo being linted. Falls back to the literal path when nothing is tracked
    (an untracked-but-referenced path must still be covered by the filters)."""
    proc = subprocess.run(
        ["git", "ls-files", "--", dep],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    files = [line for line in proc.stdout.splitlines() if line]
    return files or [dep]


def uncovered_files(dep: str, patterns: list[re.Pattern[str]]) -> list[str]:
    """The tracked files under DEP that NO filter glob matches. A partial match
    is still fail-open for the unmatched files, so every file must match."""
    return [f for f in tracked_files(dep) if not any(pat.search(f) for pat in patterns)]


def _suggestion(dep: str) -> str:
    return f"'{dep}/**'" if (REPO_ROOT / dep).is_dir() else f"'{dep}'"


def analyze(doc: object, text: str, read_repo_file) -> list[tuple[int | None, str]]:
    """Every fail-open gate violation in a parsed workflow, as (line, message)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return []
    jobs = doc["jobs"]
    decide_jobs = {jid: job for jid, job in jobs.items() if is_decide_job(job)}
    if not decide_jobs:
        return []
    blocks = _job_blocks(text)
    compiled = {jid: decide_matchers(job["with"]) for jid, job in decide_jobs.items()}

    found: list[tuple[int | None, str]] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict) or job_id in decide_jobs:
            continue
        gates = gate_refs(job, set(decide_jobs))
        if not gates:
            continue
        line = job.get("__line__")
        # The job only skips when EVERY referenced gate is closed, so a dep
        # covered by ANY referenced decide job's filters cannot fail open.
        patterns = [pat for gate in gates for pat in compiled[gate]]
        gate_names = "/".join(sorted(gates))
        block = blocks.get(job_id, (0, ""))[1]
        deps, missing = job_dependencies(job, read_repo_file)
        for absent in missing:
            found.append(
                (
                    line,
                    f"job {job_id}: references missing local action `./{absent}` — "
                    "the directory does not exist on disk, so the job cannot run.",
                )
            )
        decide_blocks = [blocks.get(g, (0, ""))[1] for g in gates]
        suppressed, reasonless = suppressions(block)
        for dep in reasonless:
            found.append(
                (
                    line,
                    f"job {job_id}: `# {OPT_OUT}: {dep}` has no reason — a "
                    "suppression must say why the dependency is safe to leave "
                    f"out of the gate (`# {OPT_OUT}: {dep} <reason>`).",
                )
            )
        for dep in dict.fromkeys(deps + declared_deps(block, *decide_blocks)):
            if dep in suppressed:
                continue
            unmatched = uncovered_files(dep, patterns)
            if not unmatched:
                continue
            found.append(
                (
                    line,
                    f"job {job_id} (gated by {gate_names}) depends on `{dep}` but "
                    f"the decide filters do not match e.g. `{unmatched[0]}` — a PR "
                    "changing only that file skips this job and the reporter goes "
                    f"green (fail-open). Add {_suggestion(dep)} to the decide "
                    f"job's filters, or suppress with `# {OPT_OUT}: {dep} <reason>`.",
                )
            )
    return found


def _read_repo_file(rel: str) -> str | None:
    """Text of a repo-relative file under REPO_ROOT, None when absent/unreadable
    — a missing referenced path is a finding for the caller, not a crash."""
    try:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH. A file this lint cannot parse
    as YAML is itself reported as a violation (line ``None``) rather than
    silently passed as clean: "no findings" on unparseable input would be
    exactly the fail-open this lint exists to catch. (YAML *syntax* is
    actionlint's job — this only fires when PyYAML can't build a document.)"""
    text = path.read_text()
    try:
        doc = yaml.load(text, Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify path-gate "
                "coverage — fix the syntax (or run actionlint) and re-check.",
            )
        ]
    return analyze(doc, text, _read_repo_file)


def workflow_files() -> list[Path]:
    # Workflows only: composite actions have no jobs, so no decide gates.
    return sorted(p for glob in WORKFLOW_GLOBS for p in WORKFLOWS_DIR.glob(glob))


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} path-gate violation(s) found.")
        print(
            "A decide filter that omits a gated job's dependency fails open: a PR "
            "changing only that file skips the job and the reporter goes green."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

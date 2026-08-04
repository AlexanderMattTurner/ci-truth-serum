#!/usr/bin/env python3
"""Require a caller to grant every permission its reusable workflow asks for.

A job that runs `uses: ./.github/workflows/x.yaml` hands its own `GITHUB_TOKEN`
to the called workflow. The called workflow can lower that grant, but it can
never raise it. A job inside the callee can declare a scope the caller does not
hold. GitHub then refuses to start the call, and the whole run fails at once.
The message reads "The workflow is requesting 'pull-requests: read', but is only
allowed 'pull-requests: none'". It names the callee, which is correct as written,
so it points away from the file that must change.

The class is a permission added to a SHARED workflow while its callers stay as
they were. The callee change is correct on its own, and every caller breaks at
the same moment. This repository hit it once, and the merge pipeline stayed down
for more than an hour, because every gated workflow calls the same path filter.

Both sides are static YAML, so the check reads them:

  * the requirement is the union of the callee's declared scopes — each job's
    own `permissions:` block, or the workflow-level block the job inherits.
    A `uses:` job that declares neither is followed into ITS callee, so a
    requirement two hops down still reaches the top caller. A cycle stops the
    walk;
  * the grant is the calling job's `permissions:` block, or the caller's
    workflow-level block when the job declares none. A block lists every scope
    it grants: an absent scope is `none`, not "unchanged".

A caller that declares NO block at all is reported the same way, because the
token then carries the repository's default permissions. That default is a
repository setting, not a fact this file states, and an administrator can change
it without touching the workflow.

`read-all` and `write-all` are handled as the baseline for unlisted scopes. A
value this check cannot read counts as the strictest reading of its side: the
caller grants nothing, and the callee needs write. A typo therefore fails
closed. (`actionlint` owns the spelling of a permission value; this lint owns
the comparison between the two sides.)

Suppress one calling job with a `# reusable-permissions-ok: <reason>` comment.
The reason is mandatory. The comment goes on the job's key line, or on one of
its direct-child lines, so the same text inside a `run:` body stays content.

Only a LOCAL `./` callee is read. A `uses:` that names another repository is
skipped, because its permissions block is not in this tree. A `./` path with no
file on disk is its own hard error: that call cannot start either.

Globs every workflow like the other workflow lints; the passed file list is
ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader as _LineLoader,
    _classification_text,
    _job_blocks,
    is_placeholder_reason,
    workflow_files,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

OPT_OUT = "reusable-permissions-ok"
# `# reusable-permissions-ok: <reason>` — per-job suppression; reason mandatory.
# An annotation READER (it extracts the reason so a placeholder can be rejected),
# not a boolean opt-out predicate. The lead mirrors `_linecheck.annotation_re`:
# the token may follow the `#` directly, or after same-line comment text whose
# last character cannot belong to a token, so a longer slug never satisfies it.
_OPT_OUT = re.compile(rf"#(?:[^\r\n]*[^\w\r\n-])?{OPT_OUT}\s*:\s*(?P<reason>[^\r\n]*)$")

# The three levels a permission scope can hold, ordered by what they allow.
LEVELS = {"none": 0, "read": 1, "write": 2}
LEVEL_NAMES = {level: name for name, level in LEVELS.items()}
# An unreadable entry takes the strictest reading of its own side, so a value
# neither side can parse fails closed rather than passing the comparison.
CALLER_UNKNOWN = LEVELS["none"]
CALLEE_UNKNOWN = LEVELS["write"]

# (baseline level for every unlisted scope, explicit per-scope levels).
Grant = tuple[int, dict[str, int]]
NOTHING: Grant = (0, {})


def grant(permissions: object, unknown: int) -> Grant | None:
    """The grant a `permissions:` value states, or None when it states nothing.

    None means the key is absent, and the holder inherits. A mapping grants only
    the scopes it lists, so its baseline is `none`; `read-all` and `write-all`
    set the baseline instead. UNKNOWN is the level an unreadable entry takes, so
    a value neither side can read still fails closed on its own side: a whole
    value becomes UNKNOWN on every scope, and one bad level becomes UNKNOWN on
    that scope.
    """
    if permissions is None:
        return None
    if isinstance(permissions, str):
        keyword = permissions.strip()
        if keyword == "write-all":
            return (LEVELS["write"], {})
        if keyword == "read-all":
            return (LEVELS["read"], {})
        return (unknown, {})
    if isinstance(permissions, dict):
        return (
            LEVELS["none"],
            {
                str(scope): LEVELS.get(str(value).strip(), unknown)
                for scope, value in permissions.items()
                # `__line__` is the line tag LineLoader adds, not a scope.
                if scope != "__line__"
            },
        )
    return (unknown, {})


def level(held: Grant, scope: str) -> int:
    """The level HELD gives SCOPE — its own entry, else the baseline."""
    return held[1].get(scope, held[0])


def union(first: Grant, second: Grant) -> Grant:
    """The grant that satisfies both — the higher level of each scope."""
    scopes = set(first[1]) | set(second[1])
    return (
        max(first[0], second[0]),
        {scope: max(level(first, scope), level(second, scope)) for scope in scopes},
    )


def local_callee(job: object) -> str | None:
    """The repo-relative workflow path a job calls with `uses: ./…`, else None.

    A `uses:` naming another repository carries an `@ref` and lives outside this
    tree, so its permissions cannot be read here and it is skipped.
    """
    if not isinstance(job, dict):
        return None
    uses = str(job.get("uses", "")).strip()
    if not uses.startswith("./") or not uses.endswith((".yaml", ".yml")):
        return None
    return uses.removeprefix("./")


def _load(path: Path) -> object:
    """A workflow's parsed document, or None when it cannot be read or parsed.

    A callee this returns None for states no requirement here. That is not a
    silent pass: a local callee is a workflow file too, so `main` scans it as
    well and reports the parse failure on its own line.
    """
    try:
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_LineLoader)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def requirements(path: Path, seen: set[str] | None = None) -> Grant:
    """Every permission the workflow at PATH needs from the token of its caller.

    Each job contributes its own `permissions:` block, or the workflow-level
    block it inherits. A `uses: ./…` job that declares neither passes its
    caller's token straight down, so the callee's own requirement is followed
    and added here. SEEN carries the paths already walked, so a call cycle ends
    the walk instead of recursing forever.
    """
    seen = set() if seen is None else seen
    resolved = str(path.resolve())
    if resolved in seen:
        return NOTHING
    seen.add(resolved)

    doc = _load(path)
    if not isinstance(doc, dict):
        return NOTHING
    workflow_grant = grant(doc.get("permissions"), CALLEE_UNKNOWN)
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return workflow_grant or NOTHING

    needed, contributed = NOTHING, False
    for job_id, job in jobs.items():
        if job_id == "__line__" or not isinstance(job, dict):
            continue
        declared = grant(job.get("permissions"), CALLEE_UNKNOWN) or workflow_grant
        if declared is not None:
            needed, contributed = union(needed, declared), True
            continue
        callee = local_callee(job)
        if callee is None:
            continue
        nested = requirements(REPO_ROOT / callee, seen)
        needed, contributed = union(needed, nested), True
    # A workflow whose jobs all inherit still states its workflow-level block.
    return needed if contributed else (workflow_grant or NOTHING)


def suppression(block: str) -> tuple[str | None, str | None]:
    """(reason, error) for the `# reusable-permissions-ok:` comment on a job BLOCK.

    Both are None when the job carries no marker. A marker that states no real
    reason yields an error instead of a reason, so it suppresses nothing. The
    marker is read from the job's key line and its direct-child lines only
    (`_classification_text`), so the same text inside a `run:` body or a deeper
    string value is content, not a suppression.
    """
    details = []
    for line in _classification_text(block).splitlines():
        match = _OPT_OUT.search(line)
        if not match:
            continue
        reason = match.group("reason").strip().lstrip("#").strip()
        if not is_placeholder_reason(reason):
            return reason, None
        details.append(f"states only {reason!r}" if reason else "carries no reason")
    if not details:
        return None, None
    return None, (
        f"`# {OPT_OUT}` {details[0]}. A suppression must say why this caller may "
        "hold less than the workflow it calls, so a reviewer can check the "
        f"argument (`# {OPT_OUT}: <reason>`)."
    )


def _render(scope: str, held: int) -> str:
    return f"`{scope}: {LEVEL_NAMES[held]}`"


def shortfalls(needed: Grant, held: Grant | None) -> list[str]:
    """Every scope HELD grants below what NEEDED asks, rendered for a message.

    HELD is None for a caller that declares no block at all; every needed scope
    is then a shortfall, because the level comes from a repository setting this
    check cannot read.
    """
    missing = []
    for scope, want in sorted(needed[1].items()):
        if want == LEVELS["none"] or (held is not None and level(held, scope) >= want):
            continue
        got = "no declared level" if held is None else LEVEL_NAMES[level(held, scope)]
        missing.append(f"{_render(scope, want)} (this caller: {got})")
    if needed[0] > LEVELS["none"] and (held is None or held[0] < needed[0]):
        baseline = f"{LEVEL_NAMES[needed[0]]}-all"
        got = "no declared level" if held is None else f"{LEVEL_NAMES[held[0]]}-all"
        missing.append(f"`{baseline}` on every other scope (this caller: {got})")
    return missing


def analyze(doc: object, text: str) -> list[tuple[int | None, str]]:
    """Every caller job that holds less than its callee needs, as (line, message)."""
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return []
    workflow_grant = grant(doc.get("permissions"), CALLER_UNKNOWN)
    blocks = _job_blocks(text)

    found: list[tuple[int | None, str]] = []
    for job_id, job in doc["jobs"].items():
        callee = local_callee(job)
        if job_id == "__line__" or callee is None:
            continue
        line = job.get("__line__")
        block = blocks.get(job_id, (0, ""))[1]
        reason, error = suppression(block)
        if error:
            found.append((line, f"job {job_id}: {error}"))
        target = REPO_ROOT / callee
        if not target.is_file():
            found.append(
                (
                    line,
                    f"job {job_id}: calls `./{callee}`, which is not a file in this "
                    "repository — the run cannot start the call.",
                )
            )
            continue
        if reason:
            continue
        held = grant(job.get("permissions"), CALLER_UNKNOWN) or workflow_grant
        missing = shortfalls(requirements(target), held)
        if not missing:
            continue
        source = (
            "neither this job nor the workflow declares a `permissions:` block, so "
            "the token carries the repository default, which this file does not "
            "state"
            if held is None
            else "this caller declares less"
        )
        found.append(
            (
                line,
                f"job {job_id} calls `./{callee}`, which needs "
                f"{', '.join(missing)}, but {source}. GitHub refuses to start a "
                "called workflow that requests more than the caller holds, so "
                "every run of this workflow fails. Grant the scopes in this job's "
                f"`permissions:` block, or suppress with `# {OPT_OUT}: <reason>`.",
            )
        )
    return found


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH. A file this lint cannot parse
    as YAML is itself reported as a violation (line ``None``) rather than
    silently passed as clean: "no findings" on unparseable input would be exactly
    the fail-open this lint exists to catch. (YAML *syntax* is actionlint's job —
    this only fires when PyYAML can't build a document.)"""
    text = path.read_text()
    try:
        doc = yaml.load(text, Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify the "
                "permissions this workflow's reusable calls need — fix the syntax "
                "(or run actionlint) and re-check.",
            )
        ]
    return analyze(doc, text)


def main() -> int:
    total = 0
    # Workflows only: a composite action calls no reusable workflow.
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} reusable-call permission violation(s) found.")
        print(
            "A called workflow that requests more than its caller holds fails the "
            "whole run at start, and the error names the callee, not the caller."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

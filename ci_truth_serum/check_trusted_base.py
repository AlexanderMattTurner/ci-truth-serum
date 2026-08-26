#!/usr/bin/env python3
"""
Flag a pull_request(_target) job that runs with privilege (write permissions or a
secret in reach) while staging code from a ref the PULL-REQUEST AUTHOR chooses.
There are two such refs, and a lint that knows only the first is blind to half
the class:

HEAD REF — the canonical "pwn-request". `pull_request_target`, and a same-repo
`pull_request`, run with the base repo's `GITHUB_TOKEN` and its secrets. A job
that checks out the PR's head ref and then executes anything from it (a build, a
test, a script the PR can edit) runs the author's code with those privileges: it
can read the secrets and push with the write token.

BASE REF — the same hole reached from the other side, and the one a "does this PR
edit the machinery?" review cannot see. `github.event.pull_request.base.ref` is
chosen by the author too: nothing requires a PR to be based on the default
branch. Push branch `A` carrying a rewritten build/release script, then open PR
`B` whose BASE is `A`. A privileged job that stages "the base branch" now stages
and executes `A` with the org PATs live — while PR `B`'s own diff stays clean, so
any "does this PR edit the machinery?" refusal never sees it.

The mitigation that LOOKS convincing and does not work: narrowing the job to
`base.ref == 'main' || base.ref == 'master'`. On a repo whose default branch is
`main`, `master` is an ordinary pushable branch name — so the second disjunct
hands the attack straight back. It is therefore not treated as a rescue here.

The real eliminator: re-derive the default branch from `$GITHUB_EVENT_PATH`
(`.repository.default_branch` — written by GitHub, not supplied by the caller)
and refuse on every negative path — missing payload, unparseable payload, payload
carrying no default branch, mismatch — with no fallback, because every fallback
here ends in running author-controlled code under the credential.

This lint reports a job when it runs privileged — a `permissions:` block
(workflow- or job-level) grants anything WRITE beyond a pure read set, OR a
`secrets.*` value appears in an `env:` at the workflow, job, or step level in that
job's reach — AND it stages an author-chosen ref:

  * HEAD: any step whose `with.ref` references
    `github.event.pull_request.head.sha`, `…head.ref`, or `github.head_ref`.
  * BASE: an `actions/checkout` step whose `with.ref` references
    `github.event.pull_request.base.sha`, `…base.ref`, or `github.base_ref`.

The base conjunct is deliberately narrower than the head one, because the base
contexts have a large legitimate non-staging use the head contexts do not: a
great many workflows read `github.base_ref` / `base.sha` as DATA — a `git diff`
range, a coverage baseline, a paths comparison. Those never materialize a tree,
so only a `ref:` handed to the action that DOES materialize one counts. The
recall this trades away is a hand-rolled `git fetch && git checkout "$BASE_REF"`
inside a `run:`, and an opaque ref (`needs.decide.outputs.base_ref`) whose value
this lint cannot know and does not guess at.

A read-only, secret-free job is safe under either ref and is not flagged —
checking out untrusted code with no privilege is the *correct* way to lint or
build a pull request. The danger is only the combination with privilege.

A workflow that genuinely needs this shape and has been made safe another way
(e.g. it executes only the default branch's trusted copy of a script, resolved as
above) opts out with a `# trusted-base-ok: <reason>` comment anywhere in the file
— the reason is REQUIRED; a bare annotation does not suppress.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    _job_blocks,
    workflow_files as _workflow_files,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
PR_TRIGGERS = ("pull_request", "pull_request_target")

# A `ref:` value that resolves to the untrusted PR head, not the base merge ref.
_HEAD_REF = re.compile(
    r"github\.event\.pull_request\.head\.(?:sha|ref)|github\.head_ref"
)
# A `ref:` value that resolves to the PR's BASE branch — author-chosen, because
# nothing requires a PR to be based on the default branch (see the module
# docstring's stacked-PR attack).
_BASE_REF = re.compile(
    r"github\.event\.pull_request\.base\.(?:sha|ref)|github\.base_ref"
)
# The action that materializes a whole ref into the workspace. Only a `ref:` handed
# to THIS turns a base context into executable code on disk; the same context in an
# `env:` (a diff range, a coverage baseline) stages nothing.
_CHECKOUT_ACTION = re.compile(r"^actions/checkout(?:[@/]|$)")
_SECRET_REF = re.compile(r"secrets\.\w+")

ALLOW = "trusted-base-ok"
# Comment-scoped and reason-required via the shared matcher: a string VALUE that
# happens to carry the token must not silently disable this lint (a fail-open),
# and a bare marker states nothing.
_ALLOW_RE = annotation_re(ALLOW)

# Which author-chosen ref a job stages. The head kind is the pre-existing finding
# and keeps its own message verbatim; the base kind is reported separately because
# the remedy is a different one.
HEAD_KIND = "head"
BASE_KIND = "base"


def _opted_out(text: str) -> bool:
    """True when a reason-bearing `# trusted-base-ok: <reason>` comment appears
    anywhere in the file."""
    return bool(_ALLOW_RE.search(text))


def _is_pr_triggered(triggers: object) -> bool:
    """True if a workflow's parsed `on:` declares a pull_request(_target) trigger,
    in any of `on:`'s spellings (scalar, list, mapping)."""
    if isinstance(triggers, str):
        return triggers in PR_TRIGGERS
    if isinstance(triggers, list):
        return any(t in PR_TRIGGERS for t in triggers)
    if isinstance(triggers, dict):
        return any(t in triggers for t in PR_TRIGGERS)
    return False


def _grants_write(permissions: object) -> bool:
    """True if a `permissions:` value grants any write scope beyond a read set.

    `write-all` (string) grants everything; a mapping grants write when any scope's
    value is `write`. A pure-read mapping, `read-all`, or `{}` (which drops all
    scopes) grants nothing.
    """
    if isinstance(permissions, str):
        return permissions == "write-all"
    if isinstance(permissions, dict):
        return any(str(v) == "write" for v in permissions.values())
    return False


def _env_has_secret(env: object) -> bool:
    """True if an `env:` mapping binds any value from `secrets.*`."""
    if not isinstance(env, dict):
        return False
    return any(_SECRET_REF.search(str(v)) for v in env.values())


def _step_refs(cfg: dict) -> list[tuple[dict, str]]:
    """Every `(step, with.ref)` pair in the job, for steps that pass a `ref:`."""
    steps = cfg.get("steps")
    if not isinstance(steps, list):
        return []
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        with_block = step.get("with")
        if isinstance(with_block, dict) and "ref" in with_block:
            out.append((step, str(with_block.get("ref", ""))))
    return out


def _job_checks_out_head(cfg: dict) -> bool:
    """True if any step in the job checks out a PR-head ref via `with.ref`."""
    return any(_HEAD_REF.search(ref) for _step, ref in _step_refs(cfg))


def _job_stages_base(cfg: dict) -> bool:
    """True if the job hands an author-chosen BASE ref to `actions/checkout`, i.e.
    materializes that ref's tree — as opposed to reading a base context as data."""
    return any(
        _BASE_REF.search(ref)
        and _CHECKOUT_ACTION.match(str(step.get("uses", "")).strip())
        for step, ref in _step_refs(cfg)
    )


def _job_is_privileged(cfg: dict, workflow_write: bool, workflow_secret: bool) -> bool:
    """True if the job runs with write access or a secret in reach (job-level or
    inherited from the workflow level)."""
    if workflow_write or workflow_secret:
        return True
    if _grants_write(cfg.get("permissions")):
        return True
    if _env_has_secret(cfg.get("env")):
        return True
    steps = cfg.get("steps")
    if isinstance(steps, list):
        return any(
            isinstance(step, dict) and _env_has_secret(step.get("env"))
            for step in steps
        )
    return False


def reported_jobs(doc: object, text: str) -> list[tuple[str, str]]:
    """Every reported job as `(name, kind)`, kind being HEAD_KIND or BASE_KIND.

    A job staging BOTH author-chosen refs is reported once, as the head finding:
    that is the older and broader of the two, and one hole must not yield two
    findings. TEXT is the raw source, needed for the comment-scoped opt-out."""
    if not isinstance(doc, dict):
        return []
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    if not _is_pr_triggered(doc.get("on", doc.get(True))):
        return []
    if _opted_out(text):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    workflow_write = _grants_write(doc.get("permissions"))
    workflow_secret = _env_has_secret(doc.get("env"))
    out: list[tuple[str, str]] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        if not _job_is_privileged(cfg, workflow_write, workflow_secret):
            continue
        if _job_checks_out_head(cfg):
            out.append((str(name), HEAD_KIND))
        elif _job_stages_base(cfg):
            out.append((str(name), BASE_KIND))
    return out


def reported_job_names(doc: object, text: str) -> list[str]:
    """The names of the jobs this lint reports for a parsed workflow.

    Exported so a sibling lint can tell which holes are already covered here and
    skip them, without re-deriving (or string-scraping) the verdict."""
    return [name for name, _kind in reported_jobs(doc, text)]


def _head_message(name: str) -> str:
    return (
        f"job '{name}' checks out the PR head ref AND runs privileged "
        "(write permissions or a secret in env) on a pull_request(_target) "
        "trigger — the PR author's code executes with the base repo's token "
        "and secrets (pwn-request). Split the privileged work off the "
        "untrusted checkout, drop the write/secret from this job, or add "
        f"'# {ALLOW}: <reason>' if it only runs base-branch-trusted "
        "code."
    )


def _base_message(name: str) -> str:
    return (
        f"job '{name}' stages the PR's BASE ref (an actions/checkout whose `ref:` "
        "reads github.event.pull_request.base.ref/.sha or github.base_ref) AND runs "
        "privileged (write permissions or a secret in env) on a "
        "pull_request(_target) trigger — the base ref is chosen by the PR AUTHOR, "
        "since nothing requires a PR to be based on the default branch. Push branch "
        "A carrying a rewritten build/release script, open PR B whose BASE is A: "
        "this job stages A into its workspace with the credentials above live, so "
        "whatever it runs from that tree is A's copy — while PR B's own diff stays "
        "clean, and any 'does this PR edit the machinery?' refusal never sees it. "
        "Narrowing to base.ref == 'main' || base.ref == "
        "'master' does NOT fix this: on a repo whose default branch is 'main', "
        "'master' is an ordinary pushable branch name, so the second disjunct hands "
        "the attack back. Instead re-derive the default branch from "
        "$GITHUB_EVENT_PATH (.repository.default_branch — written by GitHub, not "
        "supplied by the caller) and refuse on every negative path (missing payload, "
        "unparseable payload, payload with no default branch, mismatch) with no "
        "fallback; or drop the write/secret from this job. Opt out with "
        f"'# {ALLOW}: <reason>' if it stages only that verified default branch."
    )


_MESSAGES = {HEAD_KIND: _head_message, BASE_KIND: _base_message}


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every job in the workflow that stages an
    author-chosen ref (PR head or PR base) while privileged.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "pull_request head/base checkout safety — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    blocks = _job_blocks(text)
    violations: list[tuple[int | None, str]] = []
    for name, kind in reported_jobs(doc, text):
        block = blocks.get(name)
        violations.append((block[0] if block else 1, _MESSAGES[kind](name)))
    return violations


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
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

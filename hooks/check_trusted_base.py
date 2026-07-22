#!/usr/bin/env python3
"""
Flag the "pwn-request" shape: a pull_request(_target) workflow that checks out the
PR HEAD *and* runs with privilege (write permissions or a secret in reach).

`pull_request_target` — and a same-repo `pull_request` — run with the base repo's
`GITHUB_TOKEN` and its secrets. If a job in such a workflow checks out the PR's
head ref and then executes anything from it (a build, a test, a script the PR can
edit), the PR author's code runs with those privileges: it can read the secrets
and push with the write token. This is the canonical pwn-request / "unsafe
checkout of untrusted code" vulnerability.

This lint reports a job when BOTH hold:

  * it checks out a PR-head ref — a step whose `with.ref` references
    `github.event.pull_request.head.sha`, `…head.ref`, or `github.head_ref`; and
  * it runs privileged — a `permissions:` block (workflow- or job-level) grants
    anything WRITE (beyond a pure read set), OR a `secrets.*` value appears in an
    `env:` at the workflow, job, or step level in that job's reach.

A read-only, secret-free job that checks out PR head is safe and is not flagged —
that is the *correct* way to lint or build untrusted code. The danger is only the
combination with privilege.

A workflow that genuinely needs this shape and has been made safe another way
(e.g. it executes only the base branch's trusted copy of a script, never the PR's)
opts out with a `# trusted-base-ok: <reason>` comment anywhere in the file — the
reason is REQUIRED; a bare annotation does not suppress.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
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
_SECRET_REF = re.compile(r"secrets\.\w+")
# The annotation only suppresses when it carries a non-empty reason after the colon.
_ALLOW_WITH_REASON = re.compile(r"#\s*trusted-base-ok:\s*\S")


def _opted_out(text: str) -> bool:
    """True only when a reason-bearing `# trusted-base-ok:` appears inside an actual
    `#` comment — a string value that happens to contain the token must not
    silently disable the lint (that would be a fail-open)."""
    return any(
        _ALLOW_WITH_REASON.search("#" + line.split("#", 1)[1])
        for line in text.splitlines()
        if "#" in line
    )


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


def _job_checks_out_head(cfg: dict) -> bool:
    """True if any step in the job checks out a PR-head ref via `with.ref`."""
    steps = cfg.get("steps")
    if not isinstance(steps, list):
        return False
    for step in steps:
        if not isinstance(step, dict):
            continue
        with_block = step.get("with")
        if isinstance(with_block, dict) and _HEAD_REF.search(
            str(with_block.get("ref", ""))
        ):
            return True
    return False


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


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every pwn-request-shaped job in the workflow.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_workflow_pipefail &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "pull_request-head checkout safety — fix the syntax (or run "
                "actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    # PyYAML parses the bareword key `on:` as the boolean True (YAML 1.1).
    triggers = doc.get("on", doc.get(True))
    if not _is_pr_triggered(triggers):
        return []
    if _opted_out(text):
        return []

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    workflow_write = _grants_write(doc.get("permissions"))
    workflow_secret = _env_has_secret(doc.get("env"))
    blocks = _job_blocks(text)
    violations: list[tuple[int | None, str]] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        if not _job_checks_out_head(cfg):
            continue
        if not _job_is_privileged(cfg, workflow_write, workflow_secret):
            continue
        block = blocks.get(str(name))
        line = block[0] if block else 1
        violations.append(
            (
                line,
                f"job '{name}' checks out the PR head ref AND runs privileged "
                "(write permissions or a secret in env) on a pull_request(_target) "
                "trigger — the PR author's code executes with the base repo's token "
                "and secrets (pwn-request). Split the privileged work off the "
                "untrusted checkout, drop the write/secret from this job, or add "
                "'# trusted-base-ok: <reason>' if it only runs base-branch-trusted "
                "code.",
            )
        )
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

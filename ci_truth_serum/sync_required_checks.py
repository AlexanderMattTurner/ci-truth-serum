#!/usr/bin/env python3
"""Sync a repo's branch-protection ruleset required checks to the workflows.

`check-required-reporter` (the ci-truth-serum pre-commit hook) forces every
`if: always()` reporter on a PR-triggered workflow to declare, in a greppable
comment, whether it must be a required status check. This script is the apply
half of that pair: it reads those annotations — the single source of truth —
across EVERY job (cheap always-run linters carry the marker too, not just the
reporters the hook polices), expands each one's `name:` over its own
`strategy.matrix` into concrete check contexts (via the shared
`required_check_contexts`, so the lint and the apply step can never read a
different verdict from the same YAML), and rewrites the repository ruleset's
`required_status_checks` rule to exactly that set — creating that rule if the
branch ruleset doesn't have one yet.

Modes:
  --check   compute the desired set, diff it against the live ruleset, and exit
            non-zero on any drift WITHOUT mutating anything (PR-safe gate).
  (default) PUT the ruleset so its required checks equal the desired set.

The mutation path needs a token (`GH_TOKEN` / `GITHUB_TOKEN`) with
`administration: write` on the repo; it fails loud if the token is missing or no
single writable branch ruleset can be located.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    WORKFLOW_GLOBS,
    required_check_contexts,
)

API_ROOT = "https://api.github.com"
WORKFLOWS_DIR = Path(".github/workflows")


def desired_contexts(workflows_dir: Path) -> list[str]:
    """The full, sorted, de-duplicated required-check set across all workflows."""
    contexts: set[str] = set()
    for glob in WORKFLOW_GLOBS:
        for path in sorted(workflows_dir.glob(glob)):
            contexts.update(required_check_contexts(path.read_text(encoding="utf-8")))
    return sorted(contexts)


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def github_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """One authenticated GitHub REST call; returns the parsed JSON body ({} on 204).

    An HTTP error exits with the method, the endpoint, the status and GitHub's
    own message, because the bare `HTTPError` traceback names none of them.
    A WRITE that answers 403 or 404 also names the missing grant: GitHub hides
    an admin endpoint the token may not write behind 404 rather than 403, so a
    404 on the ruleset PUT that follows a successful GET of that same ruleset
    is an under-scoped token, not a ruleset that went missing.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (fixed api.github.com host)
            payload = resp.read().decode()
    except urllib.error.HTTPError as exc:
        lines = [f"GitHub API {method} {url} answered {exc.code} {exc.reason}."]
        detail = exc.read().decode(errors="replace").strip()
        if detail:
            lines.append(detail)
        if exc.code in (403, 404) and method in WRITE_METHODS:
            lines.append(
                "The token in GH_TOKEN / GITHUB_TOKEN needs `administration: "
                "write` on this repository. GitHub answers 404 rather than 403 "
                "for an admin endpoint a token may read but not write, so a "
                "readable ruleset that refuses this write is an under-scoped "
                "token."
            )
        raise SystemExit("\n".join(lines)) from exc
    return json.loads(payload) if payload else {}


def find_branch_ruleset(repo: str, token: str, *, need_write: bool) -> int:
    """The id of the one branch ruleset to act on, or fail loud naming why not.

    A repo can be covered by more than one branch ruleset targeting `main` — its
    own repo-level ruleset plus an org-level ruleset inherited from the owning
    organization. Both list under `GET /repos/{repo}/rulesets` with
    `target == "branch"`, and both READ through `/repos/{repo}/rulesets/{id}`.
    Only the repo-owned one WRITES there; an org ruleset lives on a different
    endpoint and answers 404, which reads as a missing ruleset or an
    under-scoped token. So `need_write` picks the pool: a caller that only
    reads (a `--check` drift gate) may use an org-owned ruleset, and a caller
    that writes may not. A listing that omits `source_type` is treated as
    repo-owned, which is what a repository-only listing returns.
    """
    rulesets = github_request("GET", f"{API_ROOT}/repos/{repo}/rulesets", token)
    branch = [r for r in rulesets if r.get("target") == "branch"]
    repo_owned = [
        r for r in branch if r.get("source_type", "Repository") == "Repository"
    ]
    # A repo-owned ruleset is the right target for a read too: it is the one an
    # apply run would write, so --check must gate the same ruleset. The wider
    # branch list is a fallback for a read with nothing repo-owned to pick.
    candidates = repo_owned if (need_write or repo_owned) else branch
    if len(candidates) == 1:
        return candidates[0]["id"]
    counts = (
        f"found {len(branch)} branch ruleset(s), {len(repo_owned)} of them "
        "repo-owned (source_type=Repository)"
    )
    # --ruleset-id names one of several candidates. It is no remedy when the
    # count is zero: naming an organization-owned id re-creates the same 404.
    if need_write and branch and not repo_owned:
        raise SystemExit(
            f"No writable branch ruleset on {repo}: {counts}. An "
            "organization-owned ruleset is not writable through the repository "
            "endpoint this tool uses, so an organization owner must apply the "
            "change, or the repository needs a branch ruleset of its own."
        )
    kind = "writable branch" if need_write else "branch"
    raise SystemExit(
        f"Expected exactly one {kind} ruleset on {repo}: {counts}; pass "
        "--ruleset-id to name the one to use."
    )


def _find_checks_rule(ruleset: dict) -> dict | None:
    """The ruleset's required_status_checks rule, or None if it has none.

    A branch ruleset can exist with no required_status_checks rule at all (the
    repo protects the branch but requires no checks yet). That is not an error:
    the apply path bootstraps the rule (see `apply_contexts`), and the read/diff
    path treats a missing rule as an empty required set.
    """
    for rule in ruleset.get("rules", []):
        if rule.get("type") == "required_status_checks":
            return rule
    return None


def _new_checks_rule() -> dict:
    """A fresh, empty required_status_checks rule to append when the ruleset has
    none. `strict_required_status_checks_policy` is a required parameter for the
    rule type; false = don't force the branch up to date before merging.
    apply_contexts fills in the contexts before the PUT."""
    return {
        "type": "required_status_checks",
        "parameters": {
            "required_status_checks": [],
            "strict_required_status_checks_policy": False,
        },
    }


def current_contexts(rule: dict | None) -> list[str]:
    """Sorted contexts the rule already requires; [] when the ruleset has no
    required_status_checks rule yet."""
    if rule is None:
        return []
    checks = rule["parameters"]["required_status_checks"]
    return sorted(c["context"] for c in checks)


def _integration_id(rule: dict | None) -> int | None:
    """The CI app id carried on existing checks, reused for newly-added contexts
    so they bind to the same Actions integration rather than any provider. None
    when the rule is absent or carries no bound checks."""
    if rule is None:
        return None
    for check in rule["parameters"]["required_status_checks"]:
        if "integration_id" in check:
            return check["integration_id"]
    return None


def apply_contexts(
    repo: str, ruleset_id: int, ruleset: dict, want: list[str], token: str
) -> None:
    """PUT the ruleset so its required_status_checks equal `want` exactly,
    preserving every other rule and each check's integration binding. Creates
    the required_status_checks rule when the ruleset doesn't already have one."""
    rule = _find_checks_rule(ruleset)
    if rule is None:
        rule = _new_checks_rule()
        ruleset.setdefault("rules", []).append(rule)
    integration = _integration_id(rule)
    rebuilt = []
    for context in want:
        entry: dict[str, str | int] = {"context": context}
        if integration is not None:
            entry["integration_id"] = integration
        rebuilt.append(entry)
    rule["parameters"]["required_status_checks"] = rebuilt
    github_request(
        "PUT",
        f"{API_ROOT}/repos/{repo}/rulesets/{ruleset_id}",
        token,
        {"rules": ruleset["rules"]},
    )


def _diff_lines(current: list[str], want: list[str]) -> list[str]:
    cur, des = set(current), set(want)
    return [f"  + {c}" for c in sorted(des - cur)] + [
        f"  - {c}" for c in sorted(cur - des)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero without mutating the ruleset",
    )
    parser.add_argument("--ruleset-id", type=int, default=None)
    parser.add_argument(
        "--workflows-dir", type=Path, default=WORKFLOWS_DIR, help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit("No GH_TOKEN / GITHUB_TOKEN in the environment.")

    want = desired_contexts(args.workflows_dir)
    ruleset_id = args.ruleset_id or find_branch_ruleset(
        args.repo, token, need_write=not args.check
    )
    ruleset = github_request(
        "GET", f"{API_ROOT}/repos/{args.repo}/rulesets/{ruleset_id}", token
    )
    current = current_contexts(_find_checks_rule(ruleset))

    if current == want:
        print(f"Required checks already in sync ({len(want)} contexts).")
        return 0

    print("Required-check drift between workflow annotations and the ruleset:")
    print("\n".join(_diff_lines(current, want)))

    if args.check:
        print(
            "\nERROR: ruleset is out of sync. Run without --check (or the "
            "sync-required-checks workflow) to apply."
        )
        return 1

    apply_contexts(args.repo, ruleset_id, ruleset, want, token)
    print(f"\nApplied: ruleset now requires {len(want)} checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

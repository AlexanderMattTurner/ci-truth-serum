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
`required_status_checks` rule to exactly that set.

Modes:
  --check   compute the desired set, diff it against the live ruleset, and exit
            non-zero on any drift WITHOUT mutating anything (PR-safe gate).
  (default) PUT the ruleset so its required checks equal the desired set.

The mutation path needs a token (`GH_TOKEN` / `GITHUB_TOKEN`) with
`administration: write` on the repo; it fails loud if the token is missing or the
single branch ruleset can't be located (pass `--ruleset-id` to disambiguate).
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
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
            contexts.update(required_check_contexts(path.read_text()))
    return sorted(contexts)


def github_request(method: str, url: str, token: str, body: dict | None = None) -> dict:
    """One authenticated GitHub REST call; returns the parsed JSON body ({} on 204)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (fixed api.github.com host)
        payload = resp.read().decode()
    return json.loads(payload) if payload else {}


def find_branch_ruleset(repo: str, token: str) -> int:
    """The id of the repo's single active branch ruleset, or fail loud if the
    count isn't exactly one (ambiguous target — caller must pass --ruleset-id)."""
    rulesets = github_request("GET", f"{API_ROOT}/repos/{repo}/rulesets", token)
    branch = [r for r in rulesets if r.get("target") == "branch"]
    if len(branch) != 1:
        raise SystemExit(
            f"Expected exactly one branch ruleset on {repo}, found {len(branch)}; "
            "pass --ruleset-id to disambiguate."
        )
    return branch[0]["id"]


def _checks_rule(ruleset: dict) -> dict:
    for rule in ruleset.get("rules", []):
        if rule.get("type") == "required_status_checks":
            return rule
    raise SystemExit("Ruleset has no required_status_checks rule to sync.")


def current_contexts(rule: dict) -> list[str]:
    checks = rule["parameters"]["required_status_checks"]
    return sorted(c["context"] for c in checks)


def _integration_id(rule: dict) -> int | None:
    """The CI app id carried on existing checks, reused for newly-added contexts
    so they bind to the same Actions integration rather than any provider."""
    for check in rule["parameters"]["required_status_checks"]:
        if "integration_id" in check:
            return check["integration_id"]
    return None


def apply_contexts(
    repo: str, ruleset_id: int, ruleset: dict, want: list[str], token: str
) -> None:
    """PUT the ruleset so its required_status_checks equal `want` exactly,
    preserving every other rule and each check's integration binding."""
    rule = _checks_rule(ruleset)
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
    ruleset_id = args.ruleset_id or find_branch_ruleset(args.repo, token)
    ruleset = github_request(
        "GET", f"{API_ROOT}/repos/{args.repo}/rulesets/{ruleset_id}", token
    )
    current = current_contexts(_checks_rule(ruleset))

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

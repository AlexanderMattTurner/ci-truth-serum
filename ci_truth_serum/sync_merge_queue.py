#!/usr/bin/env python3
"""Hold a repo's merge queue rule at parameters that can still merge.

PROBLEM CLASS — an edit to the merge queue rule of a branch ruleset changes how
the queue merges. The edit leaves no git trace, no pull request and no run log.
Two shapes stop every merge, and neither turns a check red:

  * `grouping_strategy: HEADGREEN` merges a whole group on the last entry's
    green build, so an untested entry rides in on another entry's result.
  * `max_entries_to_merge: 0` lets a group hold no entry at merge time. GitHub
    builds each group, watches it pass, and merges none of them. Nothing fails
    and nothing times out, so the queue simply stops.

This tool repairs those two parameters. It leaves every other parameter as a
human set it. A sync that rewrote a tuned value on each push would be a second
invisible editor of the same state, which is the failure this tool exists to
remove.

Three adjacent states stop the same merges, and each one needs a human. The tool
reports them and exits non-zero:

  * the ruleset is not enforced, so the queue and every required check are off;
  * the repo forbids the merge method the queue merges with;
  * the ruleset holds no always-on bypass for the merge queue app, so it can
    refuse the push that merges a group.

Modes:
  --check              report the drift and exit non-zero, and write nothing.
  --allow-headgreen    leave the grouping alone, for a repo whose checks re-test
                       the whole group.
  --skip-bypass-check  ask nothing about the bypass, for a repo whose ruleset
                       never refuses the queue's push.
  (default)            write the two repairs, then read the ruleset back.

The write path needs a token (`GH_TOKEN` / `GITHUB_TOKEN`) with
`administration: write` on the repo. A repo whose branch ruleset carries no
merge queue rule exits 0 and says so.
"""

import argparse
import json
import os
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sync_required_checks import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    API_ROOT,
    find_branch_ruleset,
    github_request,
)

TOOL = "sync-merge-queue"

# The grouping takes one value. Under HEADGREEN an entry merges on another
# entry's build, which is a correctness property rather than a tuning choice.
REQUIRED_GROUPING = "ALLGREEN"

# A size of zero means no group can hold an entry, so every group builds, goes
# green, and waits. Both keys must be above zero. How far above is a human's to
# set, so the repair writes the smallest working value and leaves any positive
# number alone.
#   max_entries_to_merge   how many entries one merge may take
#   max_entries_to_build   how many groups GitHub builds at once
MUST_BE_POSITIVE = ("max_entries_to_merge", "max_entries_to_build")
REPAIR_VALUE = 1

# The queue merges with one method, and that method works only while the repo
# settings allow it. A queue set to a method the repo forbids stalls the same
# silent way.
ENABLING_SETTING = {
    "MERGE": "allow_merge_commit",
    "SQUASH": "allow_squash_merge",
    "REBASE": "allow_rebase_merge",
}

# GitHub merges a group by pushing the merged commit to the branch itself, and
# that push is not a pull request. The ruleset judges the push against its own
# rules, so the queue needs a bypass entry that holds for every push. The app id
# is read at run time from the slug, because it is a GitHub-side value.
MERGE_QUEUE_APP_SLUG = "github-merge-queue"
APP_ACTOR_TYPE = "Integration"
ALWAYS_BYPASS = "always"


def queue_rule(ruleset: dict) -> dict | None:
    """The ruleset's merge_queue rule, or None when it carries none."""
    for rule in ruleset.get("rules") or []:
        if rule.get("type") == "merge_queue":
            return rule
    return None


def queue_parameters(ruleset: dict) -> dict:
    """The parameters of the ruleset's merge_queue rule. A ruleset with no such
    rule answers with an empty mapping, which every parameter then repairs."""
    rule = queue_rule(ruleset)
    return (rule.get("parameters") or {}) if rule else {}


def repairs_for(ruleset: dict, *, grouping: str | None = REQUIRED_GROUPING) -> dict:
    """The parameters this run would write, mapped to the value it would write.

    The grouping draws a write when GROUPING is set and the live value differs.
    Each MUST_BE_POSITIVE key draws a write when it is zero or absent. A positive
    size is a human's tuning and stays as it is."""
    live = queue_parameters(ruleset)
    repairs = {}
    if grouping is not None and live.get("grouping_strategy") != grouping:
        repairs["grouping_strategy"] = grouping
    for key in MUST_BE_POSITIVE:
        if not live.get(key):
            repairs[key] = REPAIR_VALUE
    return repairs


def last_edit(repo: str, ruleset_id: int, token: str) -> str:
    """Who last wrote this ruleset, and when, from GitHub's ruleset history.

    This read only names the editor of a change that has no git trace. A failure
    here goes into the returned text rather than up the stack, because a run
    that raised on it would leave the queue it exists to repair broken."""
    path = f"{API_ROOT}/repos/{repo}/rulesets/{ruleset_id}/history?per_page=1"
    try:
        versions = github_request("GET", path, token)
    except urllib.error.HTTPError as error:
        return f"unreadable (HTTP {error.code})"
    if not versions:
        return "no history recorded"
    version = versions[0]
    actor = version.get("actor") or {}
    return (
        f"{version.get('updated_at')} by actor {actor.get('id')} ({actor.get('type')})"
    )


def describe(repo: str, ruleset: dict, token: str) -> None:
    """Print the whole queue rule and the last editor of the ruleset. This run
    log is the only record of a state that changes outside the tree."""
    ruleset_id = ruleset["id"]
    print(
        f"ruleset {ruleset_id}: enforcement={ruleset.get('enforcement')} "
        f"bypass_actors={len(ruleset.get('bypass_actors') or [])} "
        f"merge_queue {json.dumps(queue_parameters(ruleset), sort_keys=True)}"
    )
    print(f"ruleset {ruleset_id}: last edit {last_edit(repo, ruleset_id, token)}")


def merge_method_problem(repo: str, ruleset: dict, settings: dict) -> str | None:
    """The reason the repo forbids the method this queue merges with, or None.

    Turning that method off in the repo settings stalls the queue the same
    silent way: each group builds, goes green, and none of them merges. Only a
    human can turn the method back on, so this reports and repairs nothing."""
    method = queue_parameters(ruleset).get("merge_method")
    enabling = ENABLING_SETTING.get(method) if isinstance(method, str) else None
    if enabling and settings.get(enabling):
        return None
    allowed = (
        f"{enabling}={settings.get(enabling)!r}" if enabling else "no such setting"
    )
    return (
        f"ruleset {ruleset['id']} merges with merge_method={method!r} but "
        f"{repo} has {allowed}, so no queue group can merge"
    )


def merge_queue_bypass_problem(ruleset: dict, token: str) -> str | None:
    """The reason the merge queue cannot push its merged commit, or None.

    The queue builds the group, passes every check, and then fails the push,
    which reads on the pull request as a merge that never happened. Only a human
    can grant a bypass, so this reports it. A read that fails is reported too: a
    green answer here would claim a grant this run never saw."""
    try:
        app = github_request("GET", f"{API_ROOT}/apps/{MERGE_QUEUE_APP_SLUG}", token)
    except urllib.error.HTTPError as error:
        return (
            f"the {MERGE_QUEUE_APP_SLUG} app id is unreadable (HTTP {error.code}), "
            f"so ruleset {ruleset['id']} was not checked for its bypass entry"
        )
    app_id = app.get("id")
    for actor in ruleset.get("bypass_actors") or []:
        if actor.get("actor_type") != APP_ACTOR_TYPE or actor.get("actor_id") != app_id:
            continue
        if actor.get("bypass_mode") == ALWAYS_BYPASS:
            return None
        return (
            f"ruleset {ruleset['id']} lets the merge queue bypass it with "
            f"bypass_mode={actor.get('bypass_mode')!r}; the queue pushes outside "
            f"a pull request, so the entry needs {ALWAYS_BYPASS!r}"
        )
    return (
        f"ruleset {ruleset['id']} has no bypass entry for the merge queue app "
        f"(actor_type={APP_ACTOR_TYPE!r}, actor_id={app_id}), so the ruleset can "
        "refuse the push that merges a group"
    )


def apply_repairs(
    repo: str,
    ruleset: dict,
    repairs: dict,
    token: str,
    *,
    grouping: str | None = REQUIRED_GROUPING,
) -> None:
    """Write REPAIRS over the merge_queue parameters, keep every other parameter,
    then read the ruleset back. The PUT can answer 200 without applying, and a
    stale answer would report a repaired queue that still refuses each merge."""
    rule = queue_rule(ruleset)
    assert rule is not None, "caller checked for a merge_queue rule"
    rule["parameters"] = {**(rule.get("parameters") or {}), **repairs}
    url = f"{API_ROOT}/repos/{repo}/rulesets/{ruleset['id']}"
    github_request("PUT", url, token, {"rules": ruleset["rules"]})
    remaining = repairs_for(github_request("GET", url, token), grouping=grouping)
    if remaining:
        raise SystemExit(
            f"{TOOL}: the PUT on ruleset {ruleset['id']} was accepted but the "
            f"read-back still needs {json.dumps(remaining, sort_keys=True)}"
        )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument(
        "--check",
        action="store_true",
        help="report drift and exit non-zero without writing the ruleset",
    )
    parser.add_argument("--ruleset-id", type=int, default=None)
    parser.add_argument(
        "--allow-headgreen",
        action="store_true",
        help="leave grouping_strategy alone (this repo's checks re-test the group)",
    )
    parser.add_argument(
        "--skip-bypass-check",
        action="store_true",
        help="do not ask whether the merge queue app can push through the ruleset",
    )
    args = parser.parse_args(argv)
    grouping = None if args.allow_headgreen else REQUIRED_GROUPING

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise SystemExit("No GH_TOKEN / GITHUB_TOKEN in the environment.")

    ruleset_id = args.ruleset_id or find_branch_ruleset(args.repo, token)
    ruleset = github_request(
        "GET", f"{API_ROOT}/repos/{args.repo}/rulesets/{ruleset_id}", token
    )
    if queue_rule(ruleset) is None:
        print(f"{TOOL}: ruleset {ruleset_id} carries no merge queue rule.")
        return

    describe(args.repo, ruleset, token)
    # Every problem is collected and reported at the end. A run that exited on
    # the first one would hide the rest of the queue's state, which is the state
    # nothing else records.
    problems = []

    # A ruleset that is not enforced gates nothing: the queue and every required
    # check are off, with no git trace. The rules write cannot change
    # enforcement, so this reports it.
    if ruleset.get("enforcement") != "active":
        problems.append(
            f"ruleset {ruleset_id} has enforcement="
            f"{ruleset.get('enforcement')!r}; only a human can re-enable it in "
            "the ruleset settings"
        )

    settings = github_request("GET", f"{API_ROOT}/repos/{args.repo}", token)
    print(
        "repo merge methods: "
        + json.dumps(
            {m: settings.get(m) for m in ENABLING_SETTING.values()}, sort_keys=True
        )
    )
    bypass = (
        None if args.skip_bypass_check else merge_queue_bypass_problem(ruleset, token)
    )
    problems.extend(
        problem
        for problem in (merge_method_problem(args.repo, ruleset, settings), bypass)
        if problem
    )

    repairs = repairs_for(ruleset, grouping=grouping)
    if not repairs:
        print(f"ruleset {ruleset_id}: merge queue rule can merge.")
    elif args.check:
        problems.append(
            f"ruleset {ruleset_id} needs {json.dumps(repairs, sort_keys=True)}; "
            "a run without --check writes it"
        )
    else:
        apply_repairs(args.repo, ruleset, repairs, token, grouping=grouping)
        print(
            f"ruleset {ruleset_id}: repaired to {json.dumps(repairs, sort_keys=True)}"
        )

    if problems:
        raise SystemExit("\n".join(f"{TOOL}: {problem}" for problem in problems))


if __name__ == "__main__":
    main()

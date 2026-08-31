"""The stand-in approver must not redden every pull request the reviewer skips.

Under a review-required ruleset the Claude review IS the approval for the pull
requests it reads. A chore, style, `release:` or bot-authored pull request is
never read, so this script posts the approval instead. It ran with the default
Actions token, and GitHub refuses `addPullRequestReview` for that token whatever
its permissions. Release PR #176 failed there, and so did every other skipped
pull request: a check that can only fail teaches nothing.

The refusal is structural, so the script stands down loudly and the workflow
hands it a PAT first. These tests drive the real script with `gh` stubbed, so
the behaviour is observed rather than asserted about the source text. The last
two keep the first honest: a stand-down that swallowed a real fault would be
worse than no stand-down at all.

The job also needs a second chance. It fires once, on `opened` or
`ready_for_review`, so a firing that fails leaves the pull request with no
approval forever: `synchronize` does not re-run it, and a later push cannot
recover it. PR #176 stranded that way. The last three tests pin the on-demand
label that re-arms it, and above all that the label re-arms the EVENT only: a
label that escaped the skip set would approve any pull request a person labels.
"""

import os
import subprocess
from pathlib import Path

import yaml

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "auto-approve-skipped-pr.sh"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"
JOB = "auto-approve-skipped"
REVIEW_JOB = "review"
# The label a human adds to re-arm the approver, and the reviewer's own label.
# They must differ: one label that did both would send a release PR to a reader
# the skip set exists to spare.
APPROVE_LABEL = "needs-auto-approve"
REVIEW_LABEL = "needs-auto-review"
# The candidates the sibling reviewer-hold approver already takes, in its order.
TOKEN_LADDER = (
    "${{ secrets.TEMPLATE_SYNC_TOKEN_ORG || secrets.TEMPLATE_SYNC_TOKEN "
    "|| secrets.GITHUB_TOKEN }}"
)


def _run(tmp_path: Path, *, message: str, code: int) -> subprocess.CompletedProcess:
    """Run the approver with `gh` printing MESSAGE and exiting CODE."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" {message!r} >&2\nexit {code}\n',
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)
    env = dict(os.environ)
    env.update(
        {"PATH": f"{bindir}:{env['PATH']}", "GH_REPO": "owner/name", "PR": "176"}
    )
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False
    )


def test_a_posted_approval_reports_it_and_exits_zero(tmp_path) -> None:
    result = _run(tmp_path, message="", code=0)
    assert result.returncode == 0, result.stderr
    assert "approved PR #176" in result.stderr


def test_the_actions_token_refusal_stands_down_loudly(tmp_path) -> None:
    result = _run(
        tmp_path,
        message="GraphQL: GitHub Actions is not permitted to approve pull requests. (addPullRequestReview)",
        code=1,
    )
    assert result.returncode == 0, result.stderr
    assert "GitHub blocks approvals from GitHub Actions" in result.stderr
    assert "TEMPLATE_SYNC_TOKEN_ORG" in result.stderr, (
        "the stand-down must name the remedy; a silent exit 0 hides the reason "
        "the skipped PRs still need a human"
    )


def test_a_self_approval_refusal_stands_down_loudly(tmp_path) -> None:
    result = _run(tmp_path, message="Can not approve your own pull request", code=1)
    assert result.returncode == 0, result.stderr
    assert "authored it" in result.stderr


def test_any_other_failure_still_goes_red(tmp_path) -> None:
    """Non-vacuity for the two stand-downs: only those two messages pass."""
    result = _run(
        tmp_path, message="HTTP 503: No server is currently available", code=1
    )
    assert result.returncode != 0
    assert "failed to post the stand-in approval" in result.stderr


def test_the_job_takes_a_pat_before_the_actions_token() -> None:
    """The stand-down keeps the check green; the PAT is what makes it useful.

    Without this the job would stand down on every pull request, which is a
    green that approves nothing.
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = [
        step
        for step in doc["jobs"][JOB]["steps"]
        if str(step.get("run", "")).endswith("auto-approve-skipped-pr.sh")
    ]
    assert len(steps) == 1, "no step runs the approver, so this test guards nothing"
    assert (steps[0].get("env") or {}).get("GH_TOKEN") == TOKEN_LADDER


def _workflow() -> dict:
    """The parsed workflow. PyYAML reads the `on:` key as the boolean True."""
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _top_level_alternatives(guard: str) -> list[str]:
    """GUARD split on every `||` that sits outside a bracket.

    The nesting is the whole question here, so the split tracks depth. A term
    the parser reports at depth 0 is one the job fires on ALONE.
    """
    terms: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(guard):
        char = guard[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0 and guard[index : index + 2] == "||":
            terms.append(guard[start:index])
            index += 2
            start = index
            continue
        index += 1
    terms.append(guard[start:])
    return [term.strip() for term in terms]


def test_a_label_re_arms_the_approver() -> None:
    """One failed firing must not strand the pull request forever."""
    doc = _workflow()
    guard = str(doc["jobs"][JOB]["if"])
    assert f"github.event.label.name == '{APPROVE_LABEL}'" in guard, (
        "the approver has no on-demand re-arm. A PR whose one firing failed "
        "needs a way back, and `synchronize` does not re-run this job."
    )
    assert "github.event.action == 'opened'" in guard, (
        "the label alternative replaced the ordinary firing rather than adding "
        "to it, so no PR gets an approval without a human label"
    )
    types = doc[True]["pull_request_target"]["types"]
    assert "labeled" in types, f"the label never reaches the job: types are {types}"


def test_the_label_cannot_approve_a_pull_request_the_reviewer_reads() -> None:
    """The label re-arms the EVENT. It never bypasses the skip set.

    A label hoisted out to a top-level `||` approves whatever a person labels:
    a draft, or a `feat:` whose review is still open. The approval body then
    says the reviewer skipped the pull request, which is false, and one label
    satisfies a review-required ruleset. This is the shape the first version of
    this change shipped, so the assertion pins behaviour, not the diff.
    """
    guard = str(_workflow()["jobs"][JOB]["if"])
    alternatives = _top_level_alternatives(guard)
    assert len(alternatives) == 1, (
        f"the guard fires on any one of {len(alternatives)} independent terms. "
        "Every gate must govern every firing, so the whole guard is one "
        f"conjunction: {alternatives}"
    )
    governed = alternatives[0]
    for gate in (
        "github.event.pull_request.draft == false",
        "github.event.pull_request.user.type == 'Bot'",
        "'release:'",
    ):
        assert gate in governed, (
            f"{gate} no longer governs the label, so a labelled pull request skips it"
        )


def test_the_two_on_demand_labels_differ() -> None:
    """Non-vacuity for the label above, and a real hazard of its own.

    The reviewer job carries a label of its own. One shared label would make a
    `release:` PR both approved and read, which defeats the skip set.
    """
    review_guard = str(_workflow()["jobs"][REVIEW_JOB]["if"])
    assert f"github.event.label.name == '{REVIEW_LABEL}'" in review_guard, (
        "the reviewer's own label clause is gone, so the idiom this mirrors no "
        "longer exists and the assertion above pins a lone invention"
    )
    assert APPROVE_LABEL not in review_guard

"""The reviewer caller pins one commit, and it must name that commit twice.

`claude-review.yaml` calls the reviewer that lives in another repository. The
`uses:` line pins the commit, and `reviewer-ref:` names the same commit again.
The called workflow clones the reviewer and checks that commit out; the clone
runs beside the caller's secrets, so a mismatch runs code the caller never
pinned.

The duplication is not a choice. GitHub gives an input no way to read the
`uses:` sha, and its own fallback for this, `github.job_workflow_sha`, is empty
on a cross-repository reusable call. That emptiness is what broke every review
run on 2026-08-26: the clone step refused, and the job went red before it read
one line of any diff. So the second copy is the fix, and this test is what
stops the two copies from parting.
"""

import re

import pytest
import yaml

from tests._helpers import REPO_ROOT

_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "claude-review.yaml"
_USES_SHA = re.compile(r"@(?P<sha>[0-9a-f]{40})\b")


@pytest.mark.drift_guard(
    "GitHub exposes the calling `uses:` sha to no expression a `with:` input "
    "can read, and its own `github.job_workflow_sha` fallback is empty on a "
    "cross-repository call, so the caller must write the sha twice"
)
def test_reviewer_ref_names_the_pinned_commit() -> None:
    document = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    call = document["jobs"]["review"]
    match = _USES_SHA.search(call["uses"])
    assert match, f"the review job no longer pins a commit sha: {call['uses']}"
    assert call["with"]["reviewer-ref"] == match["sha"]

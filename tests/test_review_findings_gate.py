"""The review-findings merge gate, driven as a real subprocess.

The gate answers one question about a pull request: may it merge? It says
`pending` while the automated reviewer still owes it a review, `failure` while
an unresolved reviewer thread carries a gating finding, and `success`
otherwise. These tests run the real script with `gh` stubbed on PATH, so every
assertion is about an observed verdict or an observed API call. `jq` stays
real, because the script's predicates ARE jq programs.

The script reads `config/review-severities.json` relative to its own directory,
so each run happens in a copied tree. That copy is what lets one test hand the
gate an empty `gating` list.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from tests._helpers import REPO_ROOT

SCRIPT_REL = Path(".github/scripts/review-findings-gate.sh")
SHA = "0123456789abcdef0123456789abcdef01234567"
REVIEWER = "github-actions"

GH_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"$GH_CALLS"
case "$*" in
"api --method POST"*)
  printf '%s\n' "$@" >>"$GH_POST"
  exit 0
  ;;
esac
args=("$@")
jqprog='.'
query=''
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[i]}" in
  --jq) jqprog="${args[i + 1]}" ;;
  -f) case "${args[i + 1]}" in query=*) query="${args[i + 1]#query=}" ;; esac ;;
  esac
done
case "$query" in
*reviewThreads*) printf '%s' "$STUB_THREADS" | jq -r "$jqprog" ;;
*reviews*) printf '%s' "$STUB_REVIEWS" | jq -r "$jqprog" ;;
*) printf '%s' "$STUB_PULL" | jq -r "$jqprog" ;;
esac
"""


def _page(field: str, nodes: list) -> str:
    """One GraphQL page of `field`, with pagination already exhausted."""
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        field: {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": nodes,
                        }
                    }
                }
            }
        }
    )


def review(state: str = "COMMENTED", body: str = "Automated review.") -> dict:
    return {
        "databaseId": 1,
        "author": {"login": REVIEWER},
        "state": state,
        "body": body,
        "submittedAt": "2026-01-01T00:00:00Z",
    }


def thread(path: str, line: int, body: str, resolved: bool = False) -> dict:
    return {
        "id": "T_1",
        "isResolved": resolved,
        "path": path,
        "line": line,
        "comments": {"nodes": [{"author": {"login": REVIEWER}, "body": body}]},
    }


def pull(title: str = "feat: a change", bot: bool = False, labels=()) -> dict:
    return {
        "title": title,
        "user": {"type": "Bot" if bot else "User"},
        "labels": [{"name": n} for n in labels],
    }


def _run(
    tmp_path: Path,
    *,
    reviews: list = (),
    threads: list = (),
    pull_request: dict | None = None,
    gating: list | None = None,
    script_source: str | None = None,
) -> subprocess.CompletedProcess:
    """Run the gate in a copied tree with `gh` stubbed per API call.

    GATING replaces `config/review-severities.json`'s gating list when given.
    SCRIPT_SOURCE replaces the gate script itself, which is how the
    non-vacuity check drives the pre-change version.
    """
    tree = tmp_path / "tree"
    (tree / ".github").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / ".github/scripts", tree / ".github/scripts")
    shutil.copytree(REPO_ROOT / "config", tree / "config")
    if script_source is not None:
        (tree / SCRIPT_REL).write_text(script_source, encoding="utf-8")
    if gating is not None:
        config = tree / "config/review-severities.json"
        doc = json.loads(config.read_text(encoding="utf-8"))
        doc["gating"] = gating
        config.write_text(json.dumps(doc), encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(GH_STUB, encoding="utf-8")
    (bindir / "gh").chmod(0o755)

    calls = tmp_path / "calls"
    post = tmp_path / "post"
    calls.write_text("", encoding="utf-8")
    post.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "GH_TOKEN": "t",
            "GH_REPO": "owner/name",
            "PR": "3",
            "REPORT_SHA": SHA,
            "GH_CALLS": str(calls),
            "GH_POST": str(post),
            "STUB_REVIEWS": _page("reviews", list(reviews)),
            "STUB_THREADS": _page("reviewThreads", list(threads)),
            "STUB_PULL": json.dumps(pull_request or pull()),
            "RETRY_MAX": "1",
            "RETRY_BASE_DELAY": "0",
        }
    )
    result = subprocess.run(
        ["bash", str(tree / SCRIPT_REL)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    result.calls = calls.read_text(encoding="utf-8").splitlines()  # type: ignore[attr-defined]
    fields = {}
    for arg in post.read_text(encoding="utf-8").splitlines():
        key, sep, value = arg.partition("=")
        if sep and key in {"state", "context", "description", "target_url"}:
            fields[key] = value
    result.posted = fields  # type: ignore[attr-defined]
    return result


BLOCKING = "<!-- severity: blocking -->\na finding"


def test_an_unreviewed_pull_request_outside_the_skip_set_posts_pending(tmp_path):
    result = _run(tmp_path, reviews=[], pull_request=pull("feat: a change"))
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "pending", result.posted
    assert result.posted["context"] == "Review findings resolved"


def test_an_unreviewed_pull_request_inside_the_skip_set_posts_success(tmp_path):
    """The reviewer reads no bot pull request, so waiting for one waits forever."""
    result = _run(
        tmp_path, reviews=[], pull_request=pull("chore(deps): bump", bot=True)
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "success", result.posted
    assert "owes this pull request no review" in result.posted["description"]


def test_a_skipped_title_by_a_human_author_posts_success(tmp_path):
    """The title arm of the skip set, which the bot-author arm would mask."""
    result = _run(tmp_path, reviews=[], pull_request=pull("chore(deps): bump"))
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "success", result.posted


def test_a_reviewed_title_by_a_human_author_posts_pending(tmp_path):
    """Non-vacuity for the title arm: only the listed types skip."""
    result = _run(tmp_path, reviews=[], pull_request=pull("feat: a change"))
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "pending", result.posted


def test_the_needs_auto_review_label_takes_a_pull_request_out_of_the_skip_set(tmp_path):
    """Same bot pull request, plus the label: a review is owed again, so pending."""
    result = _run(
        tmp_path,
        reviews=[],
        pull_request=pull("chore(deps): bump", bot=True, labels=("needs-auto-review",)),
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "pending", result.posted
    assert [c for c in result.calls if "repos/owner/name/pulls/3" in c], (
        "the gate must READ the pull request to see the label; a pending it "
        "reached without looking is pending for the wrong reason"
    )


def test_a_dismissed_review_still_counts_as_a_read(tmp_path):
    """A dismissal retracts the hold, not the reading.

    approve-if-reviewer-hold-clear.sh dismisses the reviewer's
    CHANGES_REQUESTED whenever the Actions token cannot post the clearing
    approval, which is the routine path. Treating that as unreviewed turns
    every cleared hold into a permanent `pending`.
    """
    result = _run(
        tmp_path,
        reviews=[review(state="DISMISSED")],
        threads=[],
        pull_request=pull("feat: a change"),
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "success", result.posted


def test_a_dismissed_review_does_not_clear_an_unresolved_finding(tmp_path):
    """Non-vacuity for the case above: clause (b) still holds the merge."""
    result = _run(
        tmp_path,
        reviews=[review(state="DISMISSED")],
        threads=[thread("a.py", 1, "\U0001f534 a blocking finding")],
        pull_request=pull("feat: a change"),
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "failure", result.posted


def test_a_body_less_review_by_the_reviewer_is_not_a_review(tmp_path):
    """GitHub synthesizes a body-less COMMENTED review around every thread reply."""
    result = _run(
        tmp_path,
        reviews=[review(state="COMMENTED", body="")],
        threads=[],
        pull_request=pull("feat: a change"),
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "pending", result.posted


def test_an_empty_gating_list_makes_the_gate_refuse(tmp_path):
    """A predicate matching every body would red every thread; refuse instead."""
    result = _run(tmp_path, gating=[], reviews=[review()], threads=[])
    assert result.returncode != 0
    assert "refusing to run a gate that can never gate" in result.stderr
    assert result.posted == {}, result.posted


def test_the_status_description_carries_no_non_bmp_code_point(tmp_path):
    """GitHub rejects a 4-byte code point outright, and a rejected POST hangs the PR."""
    result = _run(
        tmp_path,
        reviews=[review()],
        threads=[thread("src/\U0001f680.py", 4, BLOCKING)],
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "failure", result.posted
    assert "\U0001f680" not in result.posted["description"]
    assert "src/\U0001f680.py:4" in result.stderr, "the log line keeps the full reason"
    assert "stripped non-BMP characters" in result.stderr


def test_a_description_over_140_characters_is_truncated_to_140(tmp_path):
    result = _run(
        tmp_path,
        reviews=[review()],
        threads=[thread("src/" + "a" * 200 + ".py", 7, BLOCKING)],
    )
    assert result.returncode == 0, result.stderr
    assert result.posted["state"] == "failure", result.posted
    description = result.posted["description"]
    assert len(description) == 140, description
    assert description.endswith("...")

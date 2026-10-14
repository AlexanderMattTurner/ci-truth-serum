#!/usr/bin/env python3
"""Keep ONE issue in step with the daily mutation run.

A red job or a surviving mutant rewrites the issue's body to this run's
findings, and reopens the issue when a past run closed it. A run with neither
closes the open issue with a link to the run that proved it clean. A hidden body
marker finds the issue, so a renamed title cannot open a duplicate.

Env: RESULTS (one `job=result` line per job), SURVIVORS_FILE (the Markdown
aggregate-mutation.py wrote; absent when no aggregate ran), RUN_URL, GITHUB_SHA,
GH_TOKEN. Needs `gh` with issues:write.
"""

import json
import os
import subprocess
from pathlib import Path

MARKER = "<!-- mutation-testing-ledger -->"
TITLE = "Mutation testing: surviving mutants or failed jobs"
RED_RESULTS = frozenset({"failure", "cancelled"})
# GitHub refuses an issue body longer than this many characters.
BODY_LIMIT = 65_536


def red_jobs(results: str) -> list[str]:
    """The jobs in RESULTS' `job=result` lines that failed or were cancelled."""
    red = []
    for line in results.splitlines():
        if not line.strip():
            continue
        job, sep, result = line.partition("=")
        if not sep:
            raise SystemExit(f"unreadable RESULTS line {line!r}: expected job=result")
        if result.strip() in RED_RESULTS:
            red.append(job.strip())
    return red


def render_body(red: list[str], survivors: str, run_url: str, sha: str) -> str:
    """The issue body for a run with RED jobs or SURVIVORS."""
    parts = [
        MARKER,
        f"The daily mutation run at `{sha}` found work: {run_url}",
        "Each survivor is a mutant that the module's own tests did not catch. Kill it with a test, or mark an equivalent mutant `# pragma: no mutate` with a reason. This issue closes itself when a run finds no survivors and no failed job.",
    ]
    if red:
        parts.append("## Failed jobs\n\n" + "\n".join(f"- `{job}`" for job in red))
    if not survivors:
        return "\n\n".join(parts) + "\n"
    head = "\n\n".join([*parts, "## Surviving mutants\n\n"])
    if len(head) + len(survivors) + 1 > BODY_LIMIT:
        cut = (
            "\n\nThis list stops here to fit the issue. The `mutation-survivors`"
            f" artifact of the run holds every survivor: {run_url}"
        )
        kept = survivors[: BODY_LIMIT - len(head) - len(cut) - 1]
        survivors = kept.rpartition("\n")[0] + cut
    return head + survivors + "\n"


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], check=True, capture_output=True, text=True
    ).stdout


def _find(state: str) -> int | None:
    found = json.loads(
        _gh(
            "issue",
            "list",
            "--state",
            state,
            "--search",
            f'"{MARKER}" in:body',
            "--json",
            "number",
            "--limit",
            "1",
        )
    )
    return found[0]["number"] if found else None


def main() -> None:
    run_url = os.environ["RUN_URL"]
    red = red_jobs(os.environ["RESULTS"])
    path = Path(os.environ["SURVIVORS_FILE"])
    survivors = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    open_issue = _find("open")
    if not red and not survivors:
        if open_issue is not None:
            _gh(
                "issue",
                "close",
                str(open_issue),
                "--comment",
                f"The daily mutation run found no survivors and no failed job: {run_url}",
            )
        return
    body = render_body(red, survivors, run_url, os.environ["GITHUB_SHA"])
    issue = open_issue if open_issue is not None else _find("closed")
    if issue is None:
        _gh("issue", "create", "--title", TITLE, "--body", body)
        return
    _gh("issue", "edit", str(issue), "--body", body)
    if open_issue is None:
        _gh("issue", "reopen", str(issue))


if __name__ == "__main__":
    main()

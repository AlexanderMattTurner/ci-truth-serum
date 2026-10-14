#!/usr/bin/env python3
"""Keep ONE issue in step with the daily mutation run.

A red job or a surviving mutant rewrites the issue's body to this run's
findings, and reopens the issue when a past run closed it. A run with neither
closes the open issue with a link to the run that proved it clean. A hidden body
marker finds the issue, so a renamed title cannot open a duplicate.

The survivors come from two tools: cosmic-ray for the Python modules, and
Stryker for the JS scripts. Stryker exits 0 when a mutant survives, so its
survivors reach the issue only through its JSON report.

Every write stamps the body with the run number. Two runs can overlap, and
GitHub does not order the jobs in a concurrency group. So a run that finds a
newer stamp on the issue leaves the issue as it is, and stale findings never
replace newer ones.

Env: RESULTS (one `job=result` line per job), SURVIVORS_FILE (the Markdown
aggregate-mutation.py wrote; absent when no aggregate ran), STRYKER_REPORT
(Stryker's JSON report; absent when Stryker did not pass), RUN_URL, GITHUB_SHA,
GITHUB_RUN_NUMBER, GH_TOKEN. Needs `gh` with issues:write.
"""

import json
import os
import re
import subprocess
from pathlib import Path

MARKER = "<!-- mutation-testing-ledger -->"
TITLE = "Mutation testing: surviving mutants or failed jobs"
RED_RESULTS = frozenset({"failure", "cancelled"})
# GitHub refuses an issue body longer than this many characters.
BODY_LIMIT = 65_536
STAMP = "<!-- mutation-run: {} -->"
STAMP_RE = re.compile(r"<!-- mutation-run: (?P<run>\d+) -->")
# The Stryker statuses of a mutant that no test detected.
UNDETECTED = {"Survived": "", "NoCoverage": " (no test covers it)"}


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


def stryker_markdown(report: dict) -> str:
    """The undetected mutants in Stryker's JSON REPORT, one section per file,
    or "" when there are none."""
    sections = []
    for path, found in sorted(report["files"].items()):
        lines = []
        for mutant in sorted(
            found["mutants"],
            key=lambda m: (
                m["location"]["start"]["line"],
                m["location"]["start"]["column"],
                m["mutatorName"],
            ),
        ):
            if mutant["status"] not in UNDETECTED:
                continue
            start = mutant["location"]["start"]
            lines.append(
                f"- `{path}:{start['line']}:{start['column']}`"
                f" `{mutant['mutatorName']}`{UNDETECTED[mutant['status']]}"
            )
        if lines:
            sections.append(f"### `{path}` (Stryker)\n\n" + "\n".join(lines))
    return "\n\n".join(sections)


def stamp_of(body: str) -> int:
    """The run number stamped in BODY, or 0 for a body that a run never stamped."""
    found = STAMP_RE.search(body)
    return int(found["run"]) if found else 0


def render_clean_body(run_url: str, sha: str, run_number: int) -> str:
    """The body a clean run leaves on the issue it closes."""
    return (
        f"{MARKER}\n{STAMP.format(run_number)}\n\n"
        f"The daily mutation run at `{sha}` found no survivors and no failed job: {run_url}\n"
    )


def render_body(
    red: list[str], survivors: str, run_url: str, sha: str, run_number: int
) -> str:
    """The issue body for a run with RED jobs or SURVIVORS."""
    parts = [
        f"{MARKER}\n{STAMP.format(run_number)}",
        f"The daily mutation run at `{sha}` found work: {run_url}",
        "Each survivor is a mutant that the module's own tests did not catch. Kill it with a test, or mark an equivalent mutant with a reason: `# pragma: no mutate` in Python, `// Stryker disable` in JS. This issue closes itself when a run finds no survivors and no failed job.",
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


def _find(state: str) -> dict | None:
    """The ledger issue in STATE, as `{number, body}`, or None."""
    found = json.loads(
        _gh(
            "issue",
            "list",
            "--state",
            state,
            "--search",
            f'"{MARKER}" in:body',
            "--json",
            "number,body",
            "--limit",
            "1",
        )
    )
    return found[0] if found else None


def _read_survivors() -> str:
    """The survivors of both tools, as one Markdown list."""
    found = []
    path = Path(os.environ["SURVIVORS_FILE"])
    if path.is_file():
        found.append(path.read_text(encoding="utf-8").strip())
    report = Path(os.environ["STRYKER_REPORT"])
    if report.is_file():
        found.append(stryker_markdown(json.loads(report.read_text(encoding="utf-8"))))
    return "\n\n".join(part for part in found if part)


def main() -> None:
    run_url = os.environ["RUN_URL"]
    sha = os.environ["GITHUB_SHA"]
    run_number = int(os.environ["GITHUB_RUN_NUMBER"])
    red = red_jobs(os.environ["RESULTS"])
    survivors = _read_survivors()
    issue, is_open = _find("open"), True
    if issue is None:
        issue, is_open = _find("closed"), False
    if issue is not None and stamp_of(issue["body"]) > run_number:
        print(
            f"::notice::Issue #{issue['number']} holds run"
            f" {stamp_of(issue['body'])}, which is newer than run {run_number}."
            " This run leaves the issue as it is."
        )
        return
    if not red and not survivors:
        if issue is not None and is_open:
            number = str(issue["number"])
            body = render_clean_body(run_url, sha, run_number)
            _gh("issue", "edit", number, "--body", body)
            _gh(
                "issue",
                "close",
                number,
                "--comment",
                f"The daily mutation run found no survivors and no failed job: {run_url}",
            )
        return
    body = render_body(red, survivors, run_url, sha, run_number)
    if issue is None:
        _gh("issue", "create", "--title", TITLE, "--body", body)
        return
    _gh("issue", "edit", str(issue["number"]), "--body", body)
    if not is_open:
        _gh("issue", "reopen", str(issue["number"]))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Find the workflow runs that failed before any job started.

A workflow file that GitHub cannot load never starts a job. The run still
appears in the Actions tab, and it is still `completed`, but it holds zero jobs.
That shape is what makes the failure INVISIBLE to the routing every other check
in this pack reasons about:

  * a failure notifier watching through `on.workflow_run` reports per JOB, so a
    run with no jobs gives it nothing to name;
  * `check-required-reporter`'s `if: always()` reporter is a job, so it does not
    run either, and a required check stays "Expected — Waiting" instead of red;
  * `_failure_routing` credits route 2 (WATCHED) to a workflow that some
    notifier lists. That credit is real for every failure a job produces, and
    empty for this one. The hole is in the SHAPE of the run, not in the notifier
    list, so no static read of the tree can close it.

The result is a red that reaches nobody. The static lints in this pack cannot
see it either: the file that will not load is the file the lints parsed happily,
because the YAML that PyYAML accepts is not the same set as the YAML that the
Actions loader accepts, and a `needs:` that names a job which does not exist is
valid YAML to any parser. So this is an apply-side tool that reads run HISTORY,
the way `sync-required-checks` reads a live ruleset. It is not a pre-commit lint,
and it is in no tier aggregate::

    startup-failure-scan --repo owner/name
    startup-failure-scan --repo owner/name --window-days 14 --format markdown

Run it from a weekly health job. One run listing per workflow answers the
question for the whole repo, so the cost does not grow with how busy the repo is.

THE OBVIOUS IMPLEMENTATION FAILS OPEN, which is why the listing here asks for
`status=completed` and classifies the conclusion itself. A run that failed to
load carries `conclusion: startup_failure`, not `conclusion: failure`. The runs
listing's `status=` filter takes `failure`, so the natural one-line version of
this scan — list the failures, count the ones with no jobs — returns an empty
set forever and reports the repo healthy.

Two conclusions are deliberately NOT counted as failures here, because both
produce a zero-job run in normal operation and would swamp the report:
`cancelled` (a run cancelled while it is still queued never starts a job) and
`skipped`. Every remaining failing conclusion needs a real reason to hold no
jobs, and a file that the loader rejected is the only one this tool has seen.
"""

import argparse
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

API_ROOT = "https://api.github.com"
# The GitHub REST maximum for `per_page`, and therefore the most runs one
# listing can answer for. It bounds `--runs-per-workflow` for the same reason.
PAGE_SIZE = 100

# The run conclusions that mean "this run failed". `cancelled` and `skipped` are
# excluded: both legitimately hold zero jobs, so counting them would report a
# cancelled queue as a broken workflow file.
FAILING_CONCLUSIONS = frozenset(
    {"failure", "startup_failure", "timed_out", "action_required", "stale"}
)

# A run the loader rejected before it built the job graph. It holds zero jobs by
# construction, so the scan credits it without spending a jobs listing on it.
STARTUP_FAILURE = "startup_failure"

# A workflow whose file is gone. Its old runs cannot be repaired and it cannot
# run again, so a finding on it names work that nobody can do.
DELETED_STATE = "deleted_workflow_file"


def github_request(url: str, token: str) -> dict:
    """One authenticated GitHub REST GET; returns the parsed JSON body.

    Errors propagate. A scan that swallowed a 403 would print an empty findings
    table, and an empty table reads exactly like a healthy repo.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 (fixed api.github.com host)
        return json.loads(resp.read().decode())


def _api_url(path: str, **params: object) -> str:
    """An api.github.com URL with PARAMS encoded onto PATH."""
    query = urllib.parse.urlencode({key: str(val) for key, val in params.items()})
    return f"{API_ROOT}/{path}?{query}"


def list_workflows(repo: str, token: str) -> list[dict]:
    """Every workflow the repo has, minus the ones whose file was deleted.

    Pages to the end rather than reading one page: a repo with more than 100
    workflows would otherwise have its tail scanned never, and that miss would
    be silent.
    """
    workflows: list[dict] = []
    page = 1
    while True:
        payload = github_request(
            _api_url(f"repos/{repo}/actions/workflows", per_page=PAGE_SIZE, page=page),
            token,
        )
        batch = payload["workflows"]
        workflows.extend(w for w in batch if w.get("state") != DELETED_STATE)
        if len(batch) < PAGE_SIZE:
            return workflows
        page += 1


def list_completed_runs(
    repo: str, workflow_id: int, since_iso: str, limit: int, token: str
) -> tuple[list[dict], int]:
    """The newest completed runs of one workflow since SINCE_ISO, and how many
    the window held in total.

    The two numbers differ when the workflow ran more than LIMIT times in the
    window. The caller reports that gap rather than hiding it, because a scan
    that read 100 of 597 runs and found nothing has not shown the repo is clean.
    """
    payload = github_request(
        _api_url(
            f"repos/{repo}/actions/workflows/{workflow_id}/runs",
            status="completed",
            created=f">={since_iso}",
            per_page=limit,
            page=1,
        ),
        token,
    )
    return payload["workflow_runs"], payload["total_count"]


def job_count(repo: str, run_id: int, token: str) -> int:
    """How many jobs a run holds, read from the jobs listing's own total."""
    url = _api_url(f"repos/{repo}/actions/runs/{run_id}/jobs", per_page=1)
    return github_request(url, token)["total_count"]


def is_failed(run: dict) -> bool:
    """True when RUN finished in a conclusion that means it failed."""
    return run["conclusion"] in FAILING_CONCLUSIONS


def started_no_job(repo: str, run: dict, token: str) -> bool:
    """True when RUN holds no jobs at all.

    A `startup_failure` conclusion is decided without an API call: the loader
    rejected the file before there was a job graph to build. Every other failing
    conclusion has to be asked, because a `failure` that holds no jobs is the
    same invisible shape reached by a different route.
    """
    if run["conclusion"] == STARTUP_FAILURE:
        return True
    return job_count(repo, run["id"], token) == 0


def jobless_failures(repo: str, runs: list[dict], token: str) -> list[dict]:
    """The runs among RUNS that failed and started no job."""
    return [r for r in runs if is_failed(r) and started_no_job(repo, r, token)]


@dataclass(frozen=True)
class Finding:
    """One workflow's jobless failed runs, and whether the scan read them all."""

    name: str
    path: str
    runs: list[dict]
    scanned: int
    total: int

    @property
    def truncated(self) -> bool:
        """True when the window held more runs of this workflow than were read."""
        return self.total > self.scanned

    @property
    def newest(self) -> str:
        """When the most recent jobless failure started. A Finding is built only
        from a non-empty run list, so there is always one."""
        return max(run["created_at"] for run in self.runs)


def scan(repo: str, since_iso: str, limit: int, token: str) -> list[Finding]:
    """Every workflow in REPO with at least one jobless failed run since SINCE_ISO."""
    findings = []
    for workflow in list_workflows(repo, token):
        runs, total = list_completed_runs(repo, workflow["id"], since_iso, limit, token)
        jobless = jobless_failures(repo, runs, token)
        if jobless:
            findings.append(
                Finding(
                    name=workflow["name"] or workflow["path"],
                    path=workflow["path"],
                    runs=jobless,
                    scanned=len(runs),
                    total=total,
                )
            )
    return findings


def _truncation_lines(findings: list[Finding]) -> list[str]:
    """The note that says a count is a floor, or nothing when every run was read."""
    truncated = [f for f in findings if f.truncated]
    if not truncated:
        return []
    return [
        "",
        "Counts are a floor. The window held more runs than the scan read:",
        *(f"  {f.path}: read {f.scanned} of {f.total}" for f in truncated),
    ]


def render(
    findings: list[Finding], window_days: int, limit: int, markdown: bool
) -> str:
    """The report a human reads, as plain text or as a Markdown block.

    A clean report states its own scope. The scan reads the NEWEST runs of each
    workflow, so a repo busy enough to push a fixed breakage off that page gets
    a clean answer about the runs it read, and never a claim about the window.
    """
    heading = "### Workflows that failed to start"
    scope = (
        f"Read the newest {limit} completed runs of each workflow, over the last "
        f"{window_days} days."
    )
    if not findings:
        clean = f"No workflow failed before it started a job. {scope}"
        return f"{heading}\n\n{clean}" if markdown else clean

    if markdown:
        lines = [
            heading,
            "",
            f"These runs completed with a failure and held zero jobs, in the last "
            f"{window_days} days. A run with no jobs reports to nobody: a "
            "`workflow_run` notifier has no job to name, and an `always()` "
            "reporter never runs. Check that each file below loads.",
            "",
            "| Workflow | File | Jobless failed runs | Newest |",
            "| --- | --- | --- | --- |",
            *(
                f"| {f.name} | `{f.path}` | {len(f.runs)} | {f.newest} |"
                for f in findings
            ),
        ]
    else:
        lines = [
            f"{len(findings)} workflow(s) failed before starting a job in the last "
            f"{window_days} days:",
            *(
                f"  {f.path}: {len(f.runs)} jobless failed run(s), newest {f.newest}"
                for f in findings
            ),
        ]
    return "\n".join(lines + _truncation_lines(findings))


def window_start(days: int, now: float) -> str:
    """The ISO-8601 UTC timestamp DAYS before NOW, a Unix time, to the second."""
    start = datetime.datetime.fromtimestamp(
        now - days * 86400, tz=datetime.timezone.utc
    )
    return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the workflow runs that failed before any job started."
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY"),
        help="owner/name; defaults to $GITHUB_REPOSITORY",
    )
    parser.add_argument(
        "--window-days", type=int, default=7, help="how far back to look (default 7)"
    )
    parser.add_argument(
        "--runs-per-workflow",
        type=int,
        default=PAGE_SIZE,
        help=(
            f"newest runs to read per workflow (default and maximum {PAGE_SIZE}, "
            "which is one API page)"
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "markdown"),
        default="text",
        help="markdown emits a table for a tracking issue body",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="print the report and exit 0 even when the scan finds something",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.repo:
        raise SystemExit("--repo is required, or set GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("a token is required in GH_TOKEN or GITHUB_TOKEN")
    if args.window_days < 1:
        raise SystemExit(f"--window-days must be at least 1, got {args.window_days}")
    if not 1 <= args.runs_per_workflow <= PAGE_SIZE:
        raise SystemExit(
            f"--runs-per-workflow must be between 1 and {PAGE_SIZE}, "
            f"got {args.runs_per_workflow}"
        )

    findings = scan(
        args.repo,
        window_start(args.window_days, time.time()),
        args.runs_per_workflow,
        token,
    )
    print(
        render(
            findings,
            args.window_days,
            args.runs_per_workflow,
            args.format == "markdown",
        )
    )
    return 0 if args.report_only or not findings else 1


if __name__ == "__main__":
    sys.exit(main())

"""Tests for ci_truth_serum/startup_failure_scan.py — the run-history sensor that
names the workflows whose runs failed before any job started.

Loads the tool by path and drives each function directly. Every API read goes
through `github_request`, so the suite stubs that one function with a canned
routing table: no network, no token, and each test states exactly which URLs the
scan is allowed to ask for.

The load-bearing case is `test_startup_failure_is_not_reachable_through_a_status_failure_filter`:
it pins the reason this tool classifies conclusions itself instead of asking the
API for `status=failure`, which is the shape of the bug it exists to find.
"""

import json
import urllib.parse
import urllib.request

import pytest

from tests._helpers import load_hook

mod = load_hook("startup_failure_scan.py", "startup_failure_scan")

REPO = "owner/name"
TOKEN = "t"
SINCE = "2026-07-27T00:00:00Z"


def workflow(wf_id: int, name: str, path: str, state: str = "active") -> dict:
    return {"id": wf_id, "name": name, "path": path, "state": state}


def run(run_id: int, conclusion: str, created_at: str = "2026-08-01T00:00:00Z") -> dict:
    return {"id": run_id, "conclusion": conclusion, "created_at": created_at}


class FakeApi:
    """A canned GitHub, keyed by the leading path of each request URL.

    Records every URL asked for, so a test can assert on cost (how many jobs
    listings the scan spent) as well as on the verdict.
    """

    def __init__(self, workflows=(), runs=None, jobs=None):
        self.workflows = list(workflows)
        self.runs = runs or {}
        self.jobs = jobs or {}
        self.urls: list[str] = []

    def __call__(self, url: str, token: str) -> dict:
        assert token == TOKEN, f"unauthenticated request to {url}"
        self.urls.append(url)
        path = url.split("?")[0].removeprefix(f"{mod.API_ROOT}/")
        if path == f"repos/{REPO}/actions/workflows":
            return {"total_count": len(self.workflows), "workflows": self.workflows}
        if path.endswith("/runs"):
            wf_id = int(path.rsplit("/", 2)[-2])
            runs, total = self.runs[wf_id]
            return {"total_count": total, "workflow_runs": runs}
        if path.endswith("/jobs"):
            run_id = int(path.rsplit("/", 2)[-2])
            return {"total_count": self.jobs[run_id], "jobs": []}
        raise AssertionError(f"unexpected request: {url}")

    def jobs_listings(self) -> list[str]:
        return [u for u in self.urls if "/jobs?" in u]


@pytest.fixture
def api(monkeypatch):
    """Install a FakeApi as the module's only door to GitHub."""

    def install(fake):
        monkeypatch.setattr(mod, "github_request", fake)
        return fake

    return install


# ─── classification ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "startup_failure", "timed_out", "action_required", "stale"],
)
def test_failing_conclusions_are_failures(conclusion):
    assert mod.is_failed(run(1, conclusion))


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "skipped", "neutral"])
def test_non_failing_conclusions_are_not_failures(conclusion):
    # `cancelled` and `skipped` matter most: both hold zero jobs in normal
    # operation, so counting them would report every cancelled queue as a
    # broken workflow file.
    assert not mod.is_failed(run(1, conclusion))


def test_a_missing_conclusion_raises_rather_than_reading_as_healthy():
    # A completed run always carries a conclusion. Silently treating an absent
    # one as "not a failure" is how this scan would report a clean repo it never
    # actually classified.
    with pytest.raises(KeyError):
        mod.is_failed({"id": 1})


def test_startup_failure_is_credited_without_a_jobs_listing(api):
    fake = api(FakeApi())
    assert mod.started_no_job(REPO, run(7, "startup_failure"), TOKEN) is True
    assert fake.jobs_listings() == []


def test_a_plain_failure_with_no_jobs_is_the_same_invisible_shape(api):
    fake = api(FakeApi(jobs={7: 0}))
    assert mod.started_no_job(REPO, run(7, "failure"), TOKEN) is True
    assert len(fake.jobs_listings()) == 1


def test_a_failure_that_ran_jobs_is_not_reported(api):
    api(FakeApi(jobs={7: 3}))
    assert mod.started_no_job(REPO, run(7, "failure"), TOKEN) is False


def test_jobless_failures_skips_successes_without_spending_a_listing(api):
    fake = api(FakeApi(jobs={2: 0}))
    runs = [run(1, "success"), run(2, "failure"), run(3, "cancelled")]
    assert [r["id"] for r in mod.jobless_failures(REPO, runs, TOKEN)] == [2]
    # Only the failure was priced; a success and a cancel cost nothing.
    assert len(fake.jobs_listings()) == 1


# ─── the bug this tool exists to find ────────────────────────────────────────


def test_startup_failure_is_not_reachable_through_a_status_failure_filter():
    # The API's `status=` filter matches the conclusion. A run that failed to
    # load concludes `startup_failure`, so `status=failure` never returns it and
    # the obvious implementation of this scan reports every repo healthy. This
    # tool must therefore never narrow the listing to failures server-side.
    assert "startup_failure" != "failure"
    assert mod.STARTUP_FAILURE in mod.FAILING_CONCLUSIONS
    assert "status=completed" in mod._api_url(
        "repos/o/n/actions/workflows/1/runs", status="completed"
    )


def test_the_runs_listing_asks_for_completed_not_for_failure(api):
    fake = api(FakeApi(workflows=[], runs={1: ([], 0)}))
    mod.list_completed_runs(REPO, 1, SINCE, TOKEN)
    url = fake.urls[0]
    assert "status=completed" in url
    assert "status=failure" not in url


# ─── discovery ───────────────────────────────────────────────────────────────


def test_deleted_workflows_are_skipped(api):
    api(
        FakeApi(
            workflows=[
                workflow(1, "Live", ".github/workflows/live.yaml"),
                workflow(
                    2, "Gone", ".github/workflows/gone.yaml", "deleted_workflow_file"
                ),
            ]
        )
    )
    assert [w["id"] for w in mod.list_workflows(REPO, TOKEN)] == [1]


def test_workflow_discovery_pages_past_the_first_hundred(api):
    page1 = [workflow(i, f"W{i}", f".github/workflows/w{i}.yaml") for i in range(100)]
    page2 = [workflow(100, "Last", ".github/workflows/last.yaml")]

    class Paged(FakeApi):
        def __call__(self, url, token):
            self.urls.append(url)
            return {
                "total_count": 101,
                "workflows": page2 if "page=2" in url else page1,
            }

    api(Paged())
    assert len(mod.list_workflows(REPO, TOKEN)) == 101


# ─── scan and report ─────────────────────────────────────────────────────────


def scan_fixture() -> FakeApi:
    """One healthy workflow and one whose file will not load."""
    return FakeApi(
        workflows=[
            workflow(1, "Lint", ".github/workflows/lint.yaml"),
            workflow(2, "Tests", ".github/workflows/tests.yaml"),
        ],
        runs={
            1: ([run(11, "startup_failure", "2026-08-01T09:00:00Z")], 1),
            2: ([run(21, "success"), run(22, "failure")], 2),
        },
        jobs={22: 4},
    )


def test_scan_reports_only_the_workflow_that_never_started(api):
    api(scan_fixture())
    findings = mod.scan(REPO, SINCE, TOKEN)
    assert [f.path for f in findings] == [".github/workflows/lint.yaml"]
    assert findings[0].name == "Lint"
    assert findings[0].newest == "2026-08-01T09:00:00Z"


def test_a_window_read_in_full_carries_no_floor_note(api):
    api(scan_fixture())
    finding = mod.scan(REPO, SINCE, TOKEN)[0]
    assert finding.truncated is False
    assert "floor" not in mod.render([finding], 7, markdown=False)


def test_a_workflow_past_the_api_ceiling_is_reported_as_a_floor():
    # The scan reads the whole window, so the only thing that can shorten it is
    # the API's own pagination ceiling. The count is then a floor, and the report
    # must say which workflow it could not finish.
    finding = mod.StartupFailure(
        name="Lint",
        path=".github/workflows/lint.yaml",
        runs=[run(11, "startup_failure")],
        scanned=mod.MAX_ITEMS,
        total=7000,
    )
    assert finding.truncated is True
    report = mod.render([finding], 7, markdown=False)
    assert "read 1000 of 7000" in report
    assert "floor" in report


def test_the_clean_report_says_nothing_failed_over_the_whole_window():
    for markdown in (False, True):
        report = mod.render([], 7, markdown=markdown)
        assert "No workflow failed" in report
        assert "last 7 days" in report


def test_the_markdown_report_is_a_table_row_per_workflow():
    finding = mod.StartupFailure(
        name="Lint",
        path=".github/workflows/lint.yaml",
        runs=[run(11, "startup_failure", "2026-08-01T09:00:00Z")],
        scanned=1,
        total=1,
    )
    report = mod.render([finding], 7, markdown=True)
    assert (
        "| Lint | `.github/workflows/lint.yaml` | 1 | 2026-08-01T09:00:00Z |" in report
    )
    assert report.startswith("### Workflows that failed to start")


def test_a_nameless_workflow_falls_back_to_its_path(api):
    api(
        FakeApi(
            workflows=[workflow(1, "", ".github/workflows/anon.yaml")],
            runs={1: ([run(11, "startup_failure")], 1)},
        )
    )
    assert mod.scan(REPO, SINCE, TOKEN)[0].name == ".github/workflows/anon.yaml"


# ─── window arithmetic ───────────────────────────────────────────────────────


def test_window_start_counts_back_whole_days_in_utc():
    # 2026-08-03T00:00:00Z is 1785715200 as a Unix time.
    assert mod.window_start(7, 1785715200.0) == "2026-07-27T00:00:00Z"
    assert mod.window_start(1, 1785715200.0) == "2026-08-02T00:00:00Z"


# ─── main() ──────────────────────────────────────────────────────────────────


@pytest.fixture
def token_env(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def test_main_exits_non_zero_when_it_finds_something(api, token_env, capsys):
    api(scan_fixture())
    assert mod.main(["--repo", REPO]) == 1
    assert ".github/workflows/lint.yaml" in capsys.readouterr().out


def test_main_exits_zero_on_a_clean_repo(api, token_env, capsys):
    api(
        FakeApi(
            workflows=[workflow(1, "Lint", ".github/workflows/lint.yaml")],
            runs={1: ([], 0)},
        )
    )
    assert mod.main(["--repo", REPO]) == 0
    assert "No workflow failed" in capsys.readouterr().out


def test_report_only_still_prints_the_finding_but_exits_zero(api, token_env, capsys):
    api(scan_fixture())
    assert mod.main(["--repo", REPO, "--report-only"]) == 0
    assert ".github/workflows/lint.yaml" in capsys.readouterr().out


def test_main_reads_the_repo_from_the_environment(api, token_env, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    api(scan_fixture())
    assert mod.main([]) == 1
    capsys.readouterr()


def test_main_fails_loud_without_a_repo(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("GH_TOKEN", TOKEN)
    with pytest.raises(SystemExit, match="--repo is required"):
        mod.main([])


def test_main_fails_loud_without_a_token(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="GH_TOKEN or GITHUB_TOKEN"):
        mod.main(["--repo", REPO])


@pytest.mark.parametrize("days", ["0", "-3"])
def test_main_rejects_a_window_that_covers_nothing(token_env, days):
    with pytest.raises(SystemExit, match="--window-days must be at least 1"):
        mod.main(["--repo", REPO, "--window-days", days])


# ─── pagination ──────────────────────────────────────────────────────────────


class PagedRuns(FakeApi):
    """A workflow with TOTAL completed runs, served 100 to a page."""

    def __init__(self, total: int):
        super().__init__()
        self.total = total

    def __call__(self, url: str, token: str) -> dict:
        self.urls.append(url)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        page = int(query["page"][0])
        start = (page - 1) * mod.PAGE_SIZE
        batch = [
            run(start + i, "success")
            for i in range(min(mod.PAGE_SIZE, max(0, self.total - start)))
        ]
        return {"total_count": self.total, "workflow_runs": batch}


def test_the_runs_listing_reads_the_whole_window_not_one_page(api):
    # 700 runs is a workflow at 100 a day over a week. Reading one page would
    # answer about the newest day and report it as the week.
    fake = api(PagedRuns(700))
    runs, total = mod.list_completed_runs(REPO, 1, SINCE, TOKEN)
    assert (len(runs), total) == (700, 700)
    assert len(fake.urls) == 7


def test_pagination_stops_at_the_api_ceiling(api):
    # The listing keeps serving full pages past 1000, so without the ceiling the
    # loop would run until the API cut it off — or forever.
    fake = api(PagedRuns(7000))
    runs, total = mod.list_completed_runs(REPO, 1, SINCE, TOKEN)
    assert (len(runs), total) == (mod.MAX_ITEMS, 7000)
    assert len(fake.urls) == mod.MAX_ITEMS // mod.PAGE_SIZE


def test_a_short_final_page_ends_the_read_without_an_extra_request(api):
    fake = api(PagedRuns(150))
    runs, _ = mod.list_completed_runs(REPO, 1, SINCE, TOKEN)
    assert len(runs) == 150
    assert len(fake.urls) == 2


# ─── the request itself ──────────────────────────────────────────────────────


def test_github_request_sends_the_bearer_token_and_api_version(monkeypatch):
    seen = {}

    class Resp:
        def read(self):
            return json.dumps({"total_count": 0}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req):
        seen["headers"] = dict(req.header_items())
        seen["method"] = req.get_method()
        return Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert mod.github_request(
        f"{mod.API_ROOT}/repos/{REPO}/actions/workflows", TOKEN
    ) == {"total_count": 0}
    assert seen["method"] == "GET"
    assert seen["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert seen["headers"]["X-github-api-version"] == "2022-11-28"


def test_an_api_error_propagates_instead_of_reporting_a_clean_repo(monkeypatch):
    # A swallowed 403 would print an empty findings table, which reads exactly
    # like a healthy repo — the failure mode this whole tool exists to prevent.
    def boom(url, token):
        raise urllib.request.URLError("403")

    monkeypatch.setattr(mod, "github_request", boom)
    with pytest.raises(urllib.request.URLError):
        mod.scan(REPO, SINCE, TOKEN)


def test_the_created_filter_is_url_encoded():
    url = mod._api_url("repos/o/n/actions/workflows/1/runs", created=f">={SINCE}")
    assert "created=%3E%3D2026-07-27T00%3A00%3A00Z" in url

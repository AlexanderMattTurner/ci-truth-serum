"""Tests for ci_truth_serum/sync_required_checks.py — the apply half of the
check-required-reporter pair, which rewrites a repo's branch-protection ruleset
`required_status_checks` to the set of `# required-check: true` jobs declared in
the workflows.

Loads the hook by path and drives each function directly, plus main() with
github_request / urlopen stubbed so no real network or ruleset is touched. The
marker-scoping and matrix-expansion machinery lives in `_cts_linecheck` and is tested
in test_cts_linecheck.py; here we cover the desired-set aggregation, the REST
round-trip, the ruleset helpers, and main()'s three modes.
"""

import io
import json
import textwrap
import urllib.error
import urllib.parse
import urllib.request

import pytest

from tests._helpers import load_hook

mod = load_hook("sync_required_checks.py", "sync_required_checks")


def wf(body: str) -> str:
    return textwrap.dedent(body)


# ─── desired_contexts ────────────────────────────────────────────────────────


def test_desired_contexts_dedups_and_sorts_across_files(tmp_path):
    (tmp_path / "a.yaml").write_text(
        wf(
            """\
            on: pull_request
            jobs:
              j:
                name: Beta  # required-check: true
            """
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.yml").write_text(
        wf(
            """\
            on: [push, merge_group]
            jobs:
              j:
                name: Alpha  # required-check: true
              k:
                name: Beta  # required-check: true
            """
        ),
        encoding="utf-8",
    )
    assert mod.desired_contexts(tmp_path) == ["Alpha", "Beta"]


def _tree(tmp_path, **files):
    for name, body in files.items():
        (tmp_path / name).write_text(wf(body), encoding="utf-8")
    return tmp_path


def test_desired_contexts_prefixes_reusable_jobs_with_the_caller_name(tmp_path):
    _tree(
        tmp_path,
        **{
            "ci.yaml": """\
                on: pull_request
                jobs:
                  build:
                    name: Build
                    uses: ./.github/workflows/lint.yaml
                  plain:
                    uses: ./.github/workflows/lint.yaml
                """,
            "lint.yaml": """\
                on: workflow_call
                jobs:
                  test:  # required-check: true
                    runs-on: ubuntu-latest
                  other:
                    runs-on: ubuntu-latest
                """,
        },
    )
    assert mod.desired_contexts(tmp_path) == ["Build / test", "plain / test"]


def test_desired_contexts_expands_nested_calls_and_caller_matrix(tmp_path):
    _tree(
        tmp_path,
        **{
            "ci.yaml": """\
                on: pull_request_target
                jobs:
                  outer:
                    name: Outer ${{ matrix.os }}
                    strategy:
                      matrix:
                        os: [linux, mac]
                    uses: ./.github/workflows/mid.yml
                """,
            "mid.yml": """\
                on: workflow_call
                jobs:
                  mid:
                    uses: ./.github/workflows/leaf.yaml
                """,
            "leaf.yaml": """\
                on: workflow_call
                jobs:
                  leaf:
                    name: Leaf  # required-check: true
                """,
        },
    )
    assert mod.desired_contexts(tmp_path) == [
        "Outer linux / mid / Leaf",
        "Outer mac / mid / Leaf",
    ]


def test_desired_contexts_ignores_workflows_without_a_pr_trigger(tmp_path):
    _tree(
        tmp_path,
        **{
            "nightly.yaml": """\
                on:
                  schedule:
                    - cron: "0 0 * * *"
                jobs:
                  j:
                    name: Nightly  # required-check: true
                """,
            "none.yaml": """\
                jobs:
                  j:
                    name: NoTrigger  # required-check: true
                """,
        },
    )
    assert mod.desired_contexts(tmp_path) == []


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {
                "ci.yaml": """\
                    on: pull_request
                    jobs:
                      a:
                        uses: ./.github/workflows/gone.yaml
                    """
            },
            "gone.yaml, which does not exist",
        ),
        (
            {
                "ci.yaml": """\
                    on: pull_request
                    jobs:
                      a:
                        uses: ./.github/workflows/ci.yaml
                    """
            },
            "cycle: ci.yaml -> ci.yaml",
        ),
        (
            {
                "ci.yaml": """\
                    on: pull_request
                    jobs:
                      a:  # required-check: true
                        uses: org/repo/.github/workflows/x.yaml@v1
                    """
            },
            "GitHub posts no context under the caller",
        ),
        (
            {
                "ci.yaml": """\
                    on: pull_request
                    jobs:
                      a:  # required-check: true
                        uses: ./.github/workflows/lint.yaml
                    """,
                "lint.yaml": """\
                    on: workflow_call
                    jobs:
                      t:  # required-check: true
                    """,
            },
            "GitHub posts no context under the caller",
        ),
    ],
)
def test_desired_contexts_fails_loud_on_unreadable_calls(tmp_path, files, message):
    _tree(tmp_path, **files)
    with pytest.raises(ValueError, match=message):
        mod.desired_contexts(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "on: pull_request\njobs: [a]\n",
        "on: pull_request\njobs:\n  a: 1\n  b:\n    uses: ./.github/workflows/ci.yaml\n",
    ],
)
def test_desired_contexts_tolerates_malformed_shapes(tmp_path, body):
    # A non-mapping `jobs:` holds nothing. The second case calls a file whose
    # `jobs:` is itself malformed, so the walk returns before any cycle check.
    (tmp_path / "ci.yaml").write_text("- not a workflow\n", encoding="utf-8")
    (tmp_path / "w.yaml").write_text(body, encoding="utf-8")
    assert mod.desired_contexts(tmp_path) == []


def test_desired_contexts_reads_a_malformed_strategy_as_no_matrix(tmp_path):
    (tmp_path / "ci.yaml").write_text(
        "on: pull_request\njobs:\n  a:\n    name: A  # required-check: true\n"
        "    strategy: [x]\n",
        encoding="utf-8",
    )
    assert mod.desired_contexts(tmp_path) == ["A"]


def test_desired_contexts_skips_unmarked_remote_calls(tmp_path):
    _tree(
        tmp_path,
        **{
            "ci.yaml": """\
                on: pull_request
                jobs:
                  a:
                    uses: org/repo/.github/workflows/x.yaml@v1
                  b:
                    name: B  # required-check: true
                """
        },
    )
    assert mod.desired_contexts(tmp_path) == ["B"]


# ─── ruleset helpers ─────────────────────────────────────────────────────────


def _ruleset(contexts, integration=15368):
    checks = []
    for c in contexts:
        entry = {"context": c}
        if integration is not None:
            entry["integration_id"] = integration
        checks.append(entry)
    return {
        "id": 42,
        "rules": [
            {"type": "creation"},
            {
                "type": "required_status_checks",
                "parameters": {"required_status_checks": checks},
            },
        ],
    }


def test_find_checks_rule_found_and_missing():
    rs = _ruleset(["X"])
    assert mod._find_checks_rule(rs)["type"] == "required_status_checks"
    # A branch ruleset with no required_status_checks rule is not an error now.
    assert mod._find_checks_rule({"rules": [{"type": "creation"}]}) is None


def test_new_checks_rule_shape():
    rule = mod._new_checks_rule()
    assert rule["type"] == "required_status_checks"
    assert rule["parameters"] == {
        "required_status_checks": [],
        "strict_required_status_checks_policy": False,
    }


def test_current_contexts_sorted_and_missing_rule():
    rule = mod._find_checks_rule(_ruleset(["Zed", "Abe"]))
    assert mod.current_contexts(rule) == ["Abe", "Zed"]
    assert mod.current_contexts(None) == []


def test_integration_id_present_absent_and_no_rule():
    assert (
        mod._integration_id(mod._find_checks_rule(_ruleset(["X"], integration=99)))
        == 99
    )
    assert (
        mod._integration_id(mod._find_checks_rule(_ruleset(["X"], integration=None)))
        is None
    )
    assert mod._integration_id(None) is None


def test_diff_lines_shows_adds_then_removes():
    assert mod._diff_lines(["keep", "drop"], ["keep", "add"]) == ["  + add", "  - drop"]


# ─── github_request (urlopen stubbed) ────────────────────────────────────────


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_github_request_get_parses_json(monkeypatch):
    captured = {}

    def fake_urlopen(req):
        captured["req"] = req
        return _FakeResp('{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = mod.github_request("GET", "https://api.github.com/x", "tok")
    assert out == {"ok": True}
    assert captured["req"].get_header("Authorization") == "Bearer tok"
    assert captured["req"].get_header("Accept") == "application/vnd.github+json"
    assert captured["req"].get_header("X-github-api-version") == "2022-11-28"
    assert captured["req"].data is None


def test_github_request_put_sends_body_and_handles_empty_204(monkeypatch):
    captured = {}

    def fake_urlopen(req):
        captured["req"] = req
        return _FakeResp("")  # 204-style empty body

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    out = mod.github_request("PUT", "https://api.github.com/x", "tok", {"a": 1})
    assert out == {}
    assert json.loads(captured["req"].data.decode()) == {"a": 1}
    assert captured["req"].get_header("Content-type") == "application/json"


def _raise_http_error(code, body):
    """A urlopen stub that fails the call the way GitHub does."""

    def fake_urlopen(req):  # noqa: ARG001 (signature must match urlopen)
        raise urllib.error.HTTPError(
            "https://api.github.com/x", code, "Not Found", {}, io.BytesIO(body.encode())
        )

    return fake_urlopen


@pytest.mark.parametrize("code", [403, 404])
@pytest.mark.parametrize("method", ["PUT", "PATCH", "POST", "DELETE"])
def test_github_request_write_denial_names_the_missing_grant(monkeypatch, method, code):
    """A write GitHub refuses must say which grant the token lacks.

    The ruleset PUT answering 404 after the GET of that same ruleset succeeded
    is the shape this exists for: an under-scoped token reads as a vanished
    ruleset, and the bare HTTPError traceback names neither the endpoint nor
    the grant.
    """
    monkeypatch.setattr(
        urllib.request, "urlopen", _raise_http_error(code, '{"message": "Not Found"}')
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.github_request(method, "https://api.github.com/x", "tok", {"a": 1})
    message = str(excinfo.value)
    assert "administration: write" in message
    assert f"{method} https://api.github.com/x" in message
    assert str(code) in message
    assert '{"message": "Not Found"}' in message


@pytest.mark.parametrize("code", [403, 404, 422, 500])
def test_github_request_read_failure_reports_without_blaming_the_grant(
    monkeypatch, code
):
    """A READ that fails names the endpoint and GitHub's message, and nothing
    else: a token that cannot READ the ruleset is a different fault from one
    that cannot write it, so naming `administration: write` here would send the
    reader to the wrong setting."""
    monkeypatch.setattr(
        urllib.request, "urlopen", _raise_http_error(code, '{"message": "Bad creds"}')
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.github_request("GET", "https://api.github.com/x", "tok")
    message = str(excinfo.value)
    assert "administration: write" not in message
    assert "GET https://api.github.com/x" in message
    assert "Bad creds" in message


# ─── find_branch_ruleset ─────────────────────────────────────────────────────


def test_find_branch_ruleset_single(monkeypatch):
    _route(
        monkeypatch,
        [{"id": 7, "target": "branch"}, {"id": 8, "target": "tag"}],
        [_rule("deletion", 7)],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS) == 7


def test_find_branch_ruleset_ambiguous_fails_loud(monkeypatch):
    # Both govern main and neither requires checks yet, so nothing narrows them.
    _route(
        monkeypatch,
        [{"id": 7, "target": "branch"}, {"id": 9, "target": "branch"}],
        [_rule("deletion", 7), _rule("deletion", 9)],
    )
    with pytest.raises(SystemExit, match="found 2"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)


def test_find_branch_ruleset_narrows_to_repository_source(monkeypatch):
    # Repo-level ruleset + inherited org-level ruleset both target `main`; only
    # the Repository-source one is writable through the repo PATCH endpoint.
    _route(
        monkeypatch,
        [
            {"id": 7, "target": "branch", "source_type": "Organization"},
            {"id": 8, "target": "branch", "source_type": "Repository"},
        ],
        [_rule("deletion", 7, "Organization"), _rule("deletion", 8)],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS) == 8
    # A read must gate the same ruleset an apply would write, so the repo-owned
    # one wins here too — the wider branch list is only a fallback.
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=False, rule_type=CHECKS) == 8
    )


def test_find_branch_ruleset_ignores_non_branch_targets(monkeypatch):
    # A tag ruleset alongside a single branch ruleset must not be counted.
    _route(
        monkeypatch,
        [
            {"id": 5, "target": "tag", "source_type": "Repository"},
            {"id": 6, "target": "branch", "source_type": "Repository"},
        ],
        [_rule("deletion", 6)],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS) == 6


def test_find_branch_ruleset_lone_org_ruleset_fails_loud(monkeypatch):
    """A single ORG-owned branch ruleset is readable and unwritable.

    Returning its id sends the PUT to the repository endpoint, which answers
    404 — the shape that reads as a missing ruleset or an under-scoped token
    and costs a reader the whole diagnosis. Fail here, naming the cause.
    """
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 6, "target": "branch", "source_type": "Organization"}],
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)
    message = str(excinfo.value)
    assert "1 branch ruleset(s), 0 of them repo-owned" in message
    assert "organization-owned" in message.lower()
    # --ruleset-id names one of several candidates. With none writable it only
    # re-creates the 404, so the message must not prescribe it.
    assert "--ruleset-id" not in message


def test_find_branch_ruleset_lone_org_ruleset_reads_when_no_write(monkeypatch):
    """`--check` writes nothing, so an org-owned ruleset is a fine read target."""
    _route(
        monkeypatch,
        [{"id": 6, "target": "branch", "source_type": "Organization"}],
        [_rule("deletion", 6, "Organization")],
    )
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=False, rule_type=CHECKS) == 6
    )


def test_find_branch_ruleset_no_branch_ruleset_at_all(monkeypatch):
    # Zero branch rulesets is not an organization-owned one, so the message
    # must not blame organization ownership for it.
    monkeypatch.setattr(
        mod, "github_request", lambda *a, **k: [{"id": 5, "target": "tag"}]
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)
    assert "organization-owned" not in str(excinfo.value).lower()
    assert "found 0 branch ruleset(s)" in str(excinfo.value)


def test_find_branch_ruleset_no_repository_source_still_ambiguous(monkeypatch):
    # Two branch rulesets, neither repo-owned → cannot pick a writable target.
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [
            {"id": 7, "target": "branch", "source_type": "Organization"},
            {"id": 9, "target": "branch", "source_type": "Organization"},
        ],
    )
    with pytest.raises(SystemExit, match="found 2 branch ruleset\\(s\\), 0"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)


def test_find_branch_ruleset_two_repository_source_still_ambiguous(monkeypatch):
    # Two repo-owned rulesets both require checks on main: nothing can choose.
    _route(
        monkeypatch,
        [
            {"id": 7, "target": "branch", "source_type": "Repository"},
            {"id": 9, "target": "branch", "source_type": "Repository"},
        ],
        [_rule("required_status_checks", 7), _rule("required_status_checks", 9)],
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)
    assert "2 of them repo-owned (source_type=Repository), 2 left after matching" in (
        str(excinfo.value)
    )
    assert "--ruleset-id" in str(excinfo.value)


CHECKS = "required_status_checks"


def _rule(rule_type, ruleset_id, source="Repository"):
    """One entry of GET /repos/{repo}/rules/branches/{branch}."""
    return {
        "type": rule_type,
        "ruleset_id": ruleset_id,
        "ruleset_source_type": source,
    }


def _route(monkeypatch, listing, rules, default="main"):
    """Stub github_request with the three reads discovery makes; return the URLs.

    `rules` is the rules-in-force list for `default`, served 100 per page as
    GitHub does. Any other URL fails the test, so a read the code should not
    make cannot pass by accident.
    """
    seen = []
    branch_path = f"/repos/o/r/rules/branches/{urllib.parse.quote(default, safe='')}?"

    def fake(method, url, token, body=None):
        assert method == "GET", url
        seen.append(url)
        path = url.removeprefix(mod.API_ROOT)
        if path == "/repos/o/r/rulesets":
            return listing
        if path == "/repos/o/r":
            return {"default_branch": default}
        assert path.startswith(branch_path), url
        query = urllib.parse.parse_qs(path.split("?", 1)[1])
        assert query["per_page"] == ["100"], url
        page = int(query["page"][0])
        return rules[(page - 1) * 100 : page * 100]

    monkeypatch.setattr(mod, "github_request", fake)
    return seen


# The live shape that motivated narrowing: the repo owns a ruleset protecting
# main (17962702) and one locking an install branch (25037184), and inherits an
# org ruleset (18360957). Only the first and the org one govern main.
LIVE_LISTING = [
    {"id": 18360957, "target": "branch", "source_type": "Organization"},
    {"id": 17962702, "target": "branch", "source_type": "Repository"},
    {"id": 25037184, "target": "branch", "source_type": "Repository"},
]
LIVE_MAIN_RULES = [
    _rule("deletion", 17962702),
    _rule("required_status_checks", 17962702),
    _rule("deletion", 18360957, "Organization"),
    _rule("non_fast_forward", 18360957, "Organization"),
]


@pytest.mark.parametrize("need_write", [True, False])
def test_find_branch_ruleset_picks_the_one_governing_the_default_branch(
    monkeypatch, need_write
):
    _route(monkeypatch, LIVE_LISTING, LIVE_MAIN_RULES)
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=need_write, rule_type=CHECKS)
        == 17962702
    )


@pytest.mark.parametrize("rule_type", ["required_status_checks", "merge_queue"])
def test_find_branch_ruleset_prefers_the_one_carrying_the_callers_rule(
    monkeypatch, rule_type
):
    # Both govern main; each carries a rule a different caller acts on, so the
    # caller's own rule type decides, and the other caller gets the other one.
    _route(
        monkeypatch,
        [
            {"id": 7, "target": "branch", "source_type": "Repository"},
            {"id": 9, "target": "branch", "source_type": "Repository"},
        ],
        [_rule("merge_queue", 7), _rule("required_status_checks", 9)],
    )
    want = {"merge_queue": 7, "required_status_checks": 9}[rule_type]
    found = mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=rule_type)
    assert found == want


def test_find_branch_ruleset_none_governing_the_default_branch_fails_loud(
    monkeypatch,
):
    _route(
        monkeypatch,
        LIVE_LISTING,
        [_rule("deletion", 18360957, "Organization")],
    )
    with pytest.raises(SystemExit, match="0 left after matching the default"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)


def test_find_branch_ruleset_reads_every_page_of_rules(monkeypatch):
    # 100 org rules fill page 1; the repo-owned ruleset only appears on page 2.
    filler = [_rule("deletion", 18360957, "Organization")] * 100
    seen = _route(monkeypatch, LIVE_LISTING, [*filler, _rule("deletion", 17962702)])
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)
        == 17962702
    )
    assert sum("page=" in url for url in seen) == 2


def test_find_branch_ruleset_encodes_a_default_branch_with_a_slash(monkeypatch):
    seen = _route(monkeypatch, LIVE_LISTING, LIVE_MAIN_RULES, default="release/v1")
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)
        == 17962702
    )
    assert any("/rules/branches/release%2Fv1?" in url for url in seen)


def test_find_branch_ruleset_lone_ruleset_on_another_branch_fails_loud(monkeypatch):
    # The live repo without its main ruleset: the only repo-owned one locks
    # plugin-dist. Writing main's rule onto it would leave main ungated.
    _route(
        monkeypatch,
        [
            {"id": 18360957, "target": "branch", "source_type": "Organization"},
            {"id": 25037184, "target": "branch", "source_type": "Repository"},
        ],
        [_rule("deletion", 18360957, "Organization")],
    )
    with pytest.raises(SystemExit, match="0 left after matching the default"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS)


def test_find_branch_ruleset_keeps_a_ruleset_that_is_not_enforced(monkeypatch):
    # GitHub lists active rules only, so it cannot place a disabled ruleset on a
    # branch. Keep it; the caller reports the enforcement.
    _route(
        monkeypatch,
        [{"id": 7, "target": "branch", "enforcement": "disabled"}],
        [],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True, rule_type=CHECKS) == 7


def test_find_branch_ruleset_narrows_the_read_fallback_pool(monkeypatch):
    # --check with nothing repo-owned falls back to every branch ruleset, and
    # that pool is narrowed to main as well.
    _route(
        monkeypatch,
        [
            {"id": 7, "target": "branch", "source_type": "Organization"},
            {"id": 9, "target": "branch", "source_type": "Organization"},
        ],
        [_rule("deletion", 9, "Organization")],
    )
    assert (
        mod.find_branch_ruleset("o/r", "tok", need_write=False, rule_type=CHECKS) == 9
    )


# ─── apply_contexts ──────────────────────────────────────────────────────────


def test_apply_contexts_rebuilds_checks_and_puts(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda method, url, token, body=None: sent.update(
            method=method, url=url, body=body
        ),
    )
    rs = _ruleset(["Old"], integration=15368)
    mod.apply_contexts("o/r", 42, rs, ["New A", "New B"], "tok")
    assert sent["method"] == "PUT"
    assert sent["url"].endswith("/repos/o/r/rulesets/42")
    checks = sent["body"]["rules"][1]["parameters"]["required_status_checks"]
    assert checks == [
        {"context": "New A", "integration_id": 15368},
        {"context": "New B", "integration_id": 15368},
    ]


def test_apply_contexts_omits_integration_when_none(monkeypatch):
    monkeypatch.setattr(mod, "github_request", lambda *a, **k: {})
    rs = _ruleset(["Old"], integration=None)
    mod.apply_contexts("o/r", 42, rs, ["New"], "tok")
    rule = mod._find_checks_rule(rs)
    assert rule["parameters"]["required_status_checks"] == [{"context": "New"}]


def test_apply_contexts_bootstraps_missing_rule(monkeypatch):
    sent = {}
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda method, url, token, body=None: sent.update(
            method=method, url=url, body=body
        ),
    )
    # Branch ruleset with a creation rule but NO required_status_checks rule.
    rs = {"id": 42, "rules": [{"type": "creation"}]}
    mod.apply_contexts("o/r", 42, rs, ["Gate A", "Gate B"], "tok")
    assert sent["method"] == "PUT"
    rules = sent["body"]["rules"]
    assert [r["type"] for r in rules] == ["creation", "required_status_checks"]
    created = rules[1]["parameters"]
    assert created["strict_required_status_checks_policy"] is False
    # No pre-existing check carried an integration_id, so new contexts omit it.
    assert created["required_status_checks"] == [
        {"context": "Gate A"},
        {"context": "Gate B"},
    ]


# ─── main ────────────────────────────────────────────────────────────────────


@pytest.fixture
def _workflows(tmp_path):
    (tmp_path / "w.yaml").write_text(
        wf(
            """\
            on: pull_request
            jobs:
              j:
                name: Gate A  # required-check: true
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def _run_main(monkeypatch, argv, get_ruleset, env_token="tok", put_sink=None):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    if env_token is not None:
        monkeypatch.setenv("GH_TOKEN", env_token)

    def fake_request(method, url, token, body=None):
        if url.endswith("/rulesets"):
            return [{"id": 42, "target": "branch"}]
        if url.endswith("/repos/o/r"):
            return {"default_branch": "main"}
        if "/rules/branches/main?" in url:
            return [_rule("deletion", 42)]
        if method == "PUT":
            if put_sink is not None:
                put_sink.append(body)
            return {}
        return get_ruleset

    monkeypatch.setattr(mod, "github_request", fake_request)
    return mod.main(argv)


def test_main_requires_a_token(monkeypatch, _workflows):
    with pytest.raises(SystemExit, match="No GH_TOKEN"):
        _run_main(
            monkeypatch,
            ["--repo", "o/r", "--workflows-dir", str(_workflows)],
            _ruleset(["Gate A"]),
            env_token=None,
        )


def test_main_in_sync_is_noop(monkeypatch, capsys, _workflows):
    rc = _run_main(
        monkeypatch,
        ["--repo", "o/r", "--ruleset-id", "42", "--workflows-dir", str(_workflows)],
        _ruleset(["Gate A"]),
    )
    assert rc == 0
    assert "already in sync" in capsys.readouterr().out


def test_main_check_mode_reports_drift_without_mutating(
    monkeypatch, capsys, _workflows
):
    put_sink = []
    rc = _run_main(
        monkeypatch,
        [
            "--repo",
            "o/r",
            "--ruleset-id",
            "42",
            "--check",
            "--workflows-dir",
            str(_workflows),
        ],
        _ruleset(["Stale"]),
        put_sink=put_sink,
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "+ Gate A" in out and "- Stale" in out
    assert put_sink == []  # --check never PUTs


def test_main_apply_mode_mutates_via_find_ruleset(monkeypatch, capsys, _workflows):
    put_sink = []
    rc = _run_main(
        monkeypatch,
        ["--repo", "o/r", "--workflows-dir", str(_workflows)],  # no id → discover
        _ruleset(["Stale"]),
        put_sink=put_sink,
    )
    assert rc == 0
    assert "Applied: ruleset now requires 1 checks" in capsys.readouterr().out
    assert len(put_sink) == 1
    contexts = [
        c["context"]
        for c in put_sink[0]["rules"][1]["parameters"]["required_status_checks"]
    ]
    assert contexts == ["Gate A"]


def test_main_apply_bootstraps_when_ruleset_has_no_checks_rule(
    monkeypatch, capsys, _workflows
):
    put_sink = []
    rc = _run_main(
        monkeypatch,
        ["--repo", "o/r", "--ruleset-id", "42", "--workflows-dir", str(_workflows)],
        {"id": 42, "rules": [{"type": "creation"}]},  # no required_status_checks rule
        put_sink=put_sink,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "+ Gate A" in out  # created from an empty current set
    assert "Applied: ruleset now requires 1 checks" in out
    rules = put_sink[0]["rules"]
    assert [r["type"] for r in rules] == ["creation", "required_status_checks"]
    assert [c["context"] for c in rules[1]["parameters"]["required_status_checks"]] == [
        "Gate A"
    ]


def test_main_check_mode_reports_drift_when_no_checks_rule(
    monkeypatch, capsys, _workflows
):
    put_sink = []
    rc = _run_main(
        monkeypatch,
        [
            "--repo",
            "o/r",
            "--ruleset-id",
            "42",
            "--check",
            "--workflows-dir",
            str(_workflows),
        ],
        {"id": 42, "rules": [{"type": "creation"}]},
        put_sink=put_sink,
    )
    assert rc == 1
    assert "+ Gate A" in capsys.readouterr().out
    assert put_sink == []  # --check never PUTs, even when bootstrapping would apply

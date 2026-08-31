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
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 7, "target": "branch"}, {"id": 8, "target": "tag"}],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True) == 7


def test_find_branch_ruleset_ambiguous_fails_loud(monkeypatch):
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 7, "target": "branch"}, {"id": 9, "target": "branch"}],
    )
    with pytest.raises(SystemExit, match="found 2"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True)


def test_find_branch_ruleset_narrows_to_repository_source(monkeypatch):
    # Repo-level ruleset + inherited org-level ruleset both target `main`; only
    # the Repository-source one is writable through the repo PATCH endpoint.
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [
            {"id": 7, "target": "branch", "source_type": "Organization"},
            {"id": 8, "target": "branch", "source_type": "Repository"},
        ],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True) == 8
    # A read must gate the same ruleset an apply would write, so the repo-owned
    # one wins here too — the wider branch list is only a fallback.
    assert mod.find_branch_ruleset("o/r", "tok", need_write=False) == 8


def test_find_branch_ruleset_ignores_non_branch_targets(monkeypatch):
    # A tag ruleset alongside a single branch ruleset must not be counted.
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [
            {"id": 5, "target": "tag", "source_type": "Repository"},
            {"id": 6, "target": "branch", "source_type": "Repository"},
        ],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=True) == 6


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
        mod.find_branch_ruleset("o/r", "tok", need_write=True)
    message = str(excinfo.value)
    assert "1 branch ruleset(s), 0 of them repo-owned" in message
    assert "organization-owned" in message.lower()
    # --ruleset-id names one of several candidates. With none writable it only
    # re-creates the 404, so the message must not prescribe it.
    assert "--ruleset-id" not in message


def test_find_branch_ruleset_lone_org_ruleset_reads_when_no_write(monkeypatch):
    """`--check` writes nothing, so an org-owned ruleset is a fine read target."""
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 6, "target": "branch", "source_type": "Organization"}],
    )
    assert mod.find_branch_ruleset("o/r", "tok", need_write=False) == 6


def test_find_branch_ruleset_no_branch_ruleset_at_all(monkeypatch):
    # Zero branch rulesets is not an organization-owned one, so the message
    # must not blame organization ownership for it.
    monkeypatch.setattr(
        mod, "github_request", lambda *a, **k: [{"id": 5, "target": "tag"}]
    )
    with pytest.raises(SystemExit) as excinfo:
        mod.find_branch_ruleset("o/r", "tok", need_write=True)
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
        mod.find_branch_ruleset("o/r", "tok", need_write=True)


def test_find_branch_ruleset_two_repository_source_still_ambiguous(monkeypatch):
    # More than one repo-owned branch ruleset → narrowing can't disambiguate.
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [
            {"id": 7, "target": "branch", "source_type": "Repository"},
            {"id": 9, "target": "branch", "source_type": "Repository"},
        ],
    )
    with pytest.raises(SystemExit, match="2 of them repo-owned"):
        mod.find_branch_ruleset("o/r", "tok", need_write=True)


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

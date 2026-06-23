"""Tests for hooks/sync_required_checks.py — the apply half of the
check-required-reporter pair, which rewrites a repo's branch-protection ruleset
`required_status_checks` to the set of `# required-check: true` jobs declared in
the workflows.

Loads the hook by path and drives each function directly, plus main() with
github_request / urlopen stubbed so no real network or ruleset is touched. The
marker-scoping and matrix-expansion machinery lives in `_linecheck` and is tested
in test_linecheck.py; here we cover the desired-set aggregation, the REST
round-trip, the ruleset helpers, and main()'s three modes.
"""

import json
import textwrap
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
        )
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
        )
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


def test_checks_rule_found_and_missing():
    rs = _ruleset(["X"])
    assert mod._checks_rule(rs)["type"] == "required_status_checks"
    with pytest.raises(SystemExit, match="no required_status_checks rule"):
        mod._checks_rule({"rules": [{"type": "creation"}]})


def test_current_contexts_sorted():
    rule = mod._checks_rule(_ruleset(["Zed", "Abe"]))
    assert mod.current_contexts(rule) == ["Abe", "Zed"]


def test_integration_id_present_and_absent():
    assert mod._integration_id(mod._checks_rule(_ruleset(["X"], integration=99))) == 99
    assert (
        mod._integration_id(mod._checks_rule(_ruleset(["X"], integration=None))) is None
    )


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


# ─── find_branch_ruleset ─────────────────────────────────────────────────────


def test_find_branch_ruleset_single(monkeypatch):
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 7, "target": "branch"}, {"id": 8, "target": "tag"}],
    )
    assert mod.find_branch_ruleset("o/r", "tok") == 7


def test_find_branch_ruleset_ambiguous_fails_loud(monkeypatch):
    monkeypatch.setattr(
        mod,
        "github_request",
        lambda *a, **k: [{"id": 7, "target": "branch"}, {"id": 9, "target": "branch"}],
    )
    with pytest.raises(SystemExit, match="found 2"):
        mod.find_branch_ruleset("o/r", "tok")


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
    rule = mod._checks_rule(rs)
    assert rule["parameters"]["required_status_checks"] == [{"context": "New"}]


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
        )
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

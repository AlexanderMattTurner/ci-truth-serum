"""Tests for ci_truth_serum/sync_merge_queue.py — the repair half of a merge
queue whose ruleset parameters stop it merging.

Loads the module by path and drives each function directly, plus main() against a
fake GitHub that stores one ruleset and applies each PUT to it. The read-back
therefore answers with what the write left behind, so a PUT that drops the
repair is a failing test rather than a green one.
"""

import json
import sys
import urllib.error

import pytest

from tests._helpers import load_hook

mod = load_hook("sync_merge_queue.py", "sync_merge_queue")
# `find_branch_ruleset` is shared with sync_required_checks and calls THAT
# module's `github_request`, so a stub must replace both bindings. Patching one
# alone would leave the ruleset discovery talking to api.github.com.
shared = sys.modules["sync_required_checks"]

# The 2026-08-09 incident: every other parameter is a working, human-set value.
HEALTHY = {
    "check_response_timeout_minutes": 60,
    "grouping_strategy": "ALLGREEN",
    "max_entries_to_build": 5,
    "max_entries_to_merge": 5,
    "merge_method": "MERGE",
    "min_entries_to_merge": 1,
    "min_entries_to_merge_wait_minutes": 5,
}


MERGE_QUEUE_APP_ID = 4321  # what /apps/github-merge-queue answers with, below
QUEUE_BYPASS = {
    "actor_id": MERGE_QUEUE_APP_ID,
    "actor_type": "Integration",
    "bypass_mode": "always",
}


def ruleset(parameters=None, *, enforcement="active", with_queue=True, bypass=None):
    rules = [{"type": "creation"}]
    if with_queue:
        rules.append(
            {
                "type": "merge_queue",
                "parameters": dict(HEALTHY if parameters is None else parameters),
            }
        )
    return {
        "id": 42,
        "enforcement": enforcement,
        "bypass_actors": [dict(QUEUE_BYPASS)] if bypass is None else bypass,
        "rules": rules,
    }


SETTINGS = {
    "allow_merge_commit": True,
    "allow_squash_merge": False,
    "allow_rebase_merge": False,
}


# ─── queue_rule / queue_parameters ───────────────────────────────────────────


def test_queue_rule_found_and_missing():
    assert mod.queue_rule(ruleset())["type"] == "merge_queue"
    assert mod.queue_rule(ruleset(with_queue=False)) is None
    assert mod.queue_parameters(ruleset(with_queue=False)) == {}


def test_queue_parameters_reads_the_live_values():
    assert mod.queue_parameters(ruleset())["max_entries_to_merge"] == 5


# ─── repairs_for ─────────────────────────────────────────────────────────────


def test_a_hand_tuned_rule_draws_no_repair():
    assert mod.repairs_for(ruleset()) == {}


def test_the_incident_case_repairs_only_the_zeroed_size():
    # 2026-08-09: GitHub built every group, watched it pass, and merged none.
    live = {**HEALTHY, "max_entries_to_merge": 0}
    assert mod.repairs_for(ruleset(live)) == {"max_entries_to_merge": 1}


def test_headgreen_grouping_repairs_only_the_grouping():
    live = {**HEALTHY, "grouping_strategy": "HEADGREEN"}
    assert mod.repairs_for(ruleset(live)) == {"grouping_strategy": "ALLGREEN"}


def test_an_absent_size_repairs_like_a_zero():
    live = {k: v for k, v in HEALTHY.items() if k != "max_entries_to_build"}
    assert mod.repairs_for(ruleset(live)) == {"max_entries_to_build": 1}


def test_a_ruleset_with_no_queue_rule_needs_every_governed_parameter():
    assert mod.repairs_for(ruleset(with_queue=False)) == {
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 1,
        "max_entries_to_merge": 1,
    }


def test_the_grouping_survives_when_the_caller_opts_out():
    # A repo whose checks re-test the whole group can run HEADGREEN, so the
    # opt-out leaves the grouping alone and still repairs a zeroed size.
    live = {**HEALTHY, "grouping_strategy": "HEADGREEN", "max_entries_to_merge": 0}
    assert mod.repairs_for(ruleset(live), grouping=None) == {"max_entries_to_merge": 1}


@pytest.mark.parametrize("size", [1, 2, 5, 100])
def test_any_positive_size_is_a_humans_tuning_and_survives(size):
    live = {**HEALTHY, "max_entries_to_merge": size, "max_entries_to_build": size}
    assert mod.repairs_for(ruleset(live)) == {}


# ─── merge_method_problem ────────────────────────────────────────────────────


def test_the_allowed_method_draws_no_problem():
    assert mod.merge_method_problem("o/r", ruleset(), SETTINGS) is None


def test_a_method_the_repo_forbids_is_reported():
    live = {**HEALTHY, "merge_method": "SQUASH"}
    problem = mod.merge_method_problem("o/r", ruleset(live), SETTINGS)
    assert "allow_squash_merge=False" in problem


def test_a_method_github_has_no_setting_for_is_reported():
    live = {**HEALTHY, "merge_method": "TELEPORT"}
    problem = mod.merge_method_problem("o/r", ruleset(live), SETTINGS)
    assert "no such setting" in problem


# ─── merge_queue_bypass_problem ──────────────────────────────────────────────


def _app_lookup(monkeypatch, answer=None):
    def fake(method, url, token, body=None):
        assert url.endswith(f"/apps/{mod.MERGE_QUEUE_APP_SLUG}"), url
        if isinstance(answer, Exception):
            raise answer
        return {"id": MERGE_QUEUE_APP_ID} if answer is None else answer

    monkeypatch.setattr(mod, "github_request", fake)


def test_an_always_on_bypass_for_the_queue_app_draws_no_problem(monkeypatch):
    _app_lookup(monkeypatch)
    assert mod.merge_queue_bypass_problem(ruleset(), "tok") is None


def test_a_ruleset_with_no_queue_bypass_is_reported(monkeypatch):
    _app_lookup(monkeypatch)
    problem = mod.merge_queue_bypass_problem(ruleset(bypass=[]), "tok")
    assert "no bypass entry for the merge queue app" in problem


def test_a_bypass_for_another_app_is_not_the_queues(monkeypatch):
    # An Actions or Dependabot bypass carries the same actor_type, so the id is
    # what tells them apart.
    _app_lookup(monkeypatch)
    other = [{"actor_id": 15368, "actor_type": "Integration", "bypass_mode": "always"}]
    problem = mod.merge_queue_bypass_problem(ruleset(bypass=other), "tok")
    assert "no bypass entry for the merge queue app" in problem


def test_a_pull_request_only_bypass_is_reported(monkeypatch):
    # The queue pushes the merged commit outside any pull request, so a bypass
    # scoped to pull requests never applies to it.
    _app_lookup(monkeypatch)
    scoped = [{**QUEUE_BYPASS, "bypass_mode": "pull_request"}]
    problem = mod.merge_queue_bypass_problem(ruleset(bypass=scoped), "tok")
    assert "bypass_mode='pull_request'" in problem


def test_an_unreadable_app_lookup_is_reported_not_passed(monkeypatch):
    _app_lookup(
        monkeypatch,
        urllib.error.HTTPError("u", 404, "Not Found", {}, None),  # type: ignore[arg-type]
    )
    problem = mod.merge_queue_bypass_problem(ruleset(), "tok")
    assert "unreadable (HTTP 404)" in problem


# ─── last_edit ───────────────────────────────────────────────────────────────


def _history(monkeypatch, answer):
    def fake(method, url, token, body=None):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(mod, "github_request", fake)


def test_last_edit_names_the_actor(monkeypatch):
    _history(
        monkeypatch,
        [{"updated_at": "2026-08-09T17:00:00Z", "actor": {"id": 7, "type": "User"}}],
    )
    assert mod.last_edit("o/r", 42, "tok") == "2026-08-09T17:00:00Z by actor 7 (User)"


def test_last_edit_survives_a_refused_history_read(monkeypatch):
    # Attribution is not the repair, so a refusal here must not raise past it.
    _history(
        monkeypatch,
        urllib.error.HTTPError("u", 403, "Forbidden", {}, None),  # type: ignore[arg-type]
    )
    assert mod.last_edit("o/r", 42, "tok") == "unreadable (HTTP 403)"


def test_last_edit_reports_an_empty_history(monkeypatch):
    _history(monkeypatch, [])
    assert mod.last_edit("o/r", 42, "tok") == "no history recorded"


# ─── apply_repairs ───────────────────────────────────────────────────────────


class FakeGitHub:
    """One stored ruleset, plus the repo settings. A PUT replaces the stored
    rules, so a later GET answers with what the write left behind."""

    def __init__(self, stored, settings=None, *, honour_put=True):
        self.stored = stored
        self.settings = dict(SETTINGS if settings is None else settings)
        self.honour_put = honour_put
        self.puts: list[dict] = []

    def request(self, method, url, token, body=None):
        if method == "PUT":
            self.puts.append(body)
            if self.honour_put:
                self.stored["rules"] = json.loads(json.dumps(body["rules"]))
            return {}
        if url.endswith("/rulesets"):
            return [{"id": 42, "target": "branch", "source_type": "Repository"}]
        if "/rulesets/" in url and "history" in url:
            return [{"updated_at": "2026-08-09T17:00:00Z", "actor": {"id": 7}}]
        if "/rulesets/" in url:
            return json.loads(json.dumps(self.stored))
        if f"/apps/{mod.MERGE_QUEUE_APP_SLUG}" in url:
            return {"id": MERGE_QUEUE_APP_ID}
        return self.settings


def test_apply_repairs_keeps_every_ungoverned_parameter(monkeypatch):
    live = {**HEALTHY, "max_entries_to_merge": 0}
    # The client's document and the server's state are separate objects, as they
    # are in a real run. Sharing one would let an in-memory edit stand in for the
    # read-back this function exists to make.
    fake = FakeGitHub(ruleset(live))
    monkeypatch.setattr(mod, "github_request", fake.request)
    mod.apply_repairs("o/r", ruleset(live), {"max_entries_to_merge": 1}, "tok")
    written = fake.puts[0]["rules"][1]["parameters"]
    assert written == {**HEALTHY, "max_entries_to_merge": 1}
    assert [r["type"] for r in fake.puts[0]["rules"]] == ["creation", "merge_queue"]


def test_apply_repairs_refuses_a_put_that_did_not_apply(monkeypatch):
    live = {**HEALTHY, "max_entries_to_merge": 0}
    fake = FakeGitHub(ruleset(live), honour_put=False)
    monkeypatch.setattr(mod, "github_request", fake.request)
    with pytest.raises(SystemExit, match="read-back still needs"):
        mod.apply_repairs("o/r", ruleset(live), {"max_entries_to_merge": 1}, "tok")


# ─── main ────────────────────────────────────────────────────────────────────


def run_main(monkeypatch, fake, argv, env_token="tok"):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    if env_token is not None:
        monkeypatch.setenv("GH_TOKEN", env_token)
    monkeypatch.setattr(mod, "github_request", fake.request)
    monkeypatch.setattr(shared, "github_request", fake.request)
    return mod.main(argv)


def test_main_requires_a_token_before_any_request(monkeypatch):
    fake = FakeGitHub(ruleset())
    with pytest.raises(SystemExit, match="No GH_TOKEN"):
        run_main(monkeypatch, fake, ["--repo", "o/r"], env_token=None)
    assert fake.puts == []


def test_main_allow_headgreen_leaves_the_grouping(monkeypatch, capsys):
    fake = FakeGitHub(ruleset({**HEALTHY, "grouping_strategy": "HEADGREEN"}))
    run_main(monkeypatch, fake, ["--repo", "o/r", "--allow-headgreen"])
    assert fake.puts == []
    assert "merge queue rule can merge" in capsys.readouterr().out


def test_main_on_a_repo_with_no_merge_queue_says_so(monkeypatch, capsys):
    fake = FakeGitHub(ruleset(with_queue=False))
    run_main(monkeypatch, fake, ["--repo", "o/r"])
    assert "carries no merge queue rule" in capsys.readouterr().out
    assert fake.puts == []


def test_main_on_a_working_queue_writes_nothing(monkeypatch, capsys):
    fake = FakeGitHub(ruleset())
    run_main(monkeypatch, fake, ["--repo", "o/r"])
    out = capsys.readouterr().out
    assert "merge queue rule can merge" in out
    assert "last edit 2026-08-09T17:00:00Z" in out  # the invisible edit is logged
    assert fake.puts == []


def test_main_repairs_the_incident_and_reads_it_back(monkeypatch, capsys):
    fake = FakeGitHub(ruleset({**HEALTHY, "max_entries_to_merge": 0}))
    run_main(monkeypatch, fake, ["--repo", "o/r"])
    assert "repaired to" in capsys.readouterr().out
    assert fake.puts[0]["rules"][1]["parameters"]["max_entries_to_merge"] == 1


def test_main_check_mode_reports_the_drift_and_writes_nothing(monkeypatch):
    fake = FakeGitHub(ruleset({**HEALTHY, "grouping_strategy": "HEADGREEN"}))
    with pytest.raises(SystemExit, match="grouping_strategy"):
        run_main(monkeypatch, fake, ["--repo", "o/r", "--check"])
    assert fake.puts == []


def test_main_reds_on_a_ruleset_that_is_not_enforced_and_still_repairs(monkeypatch):
    live = {**HEALTHY, "max_entries_to_merge": 0}
    fake = FakeGitHub(ruleset(live, enforcement="disabled"))
    with pytest.raises(SystemExit, match="enforcement='disabled'"):
        run_main(monkeypatch, fake, ["--repo", "o/r"])
    # An unenforced ruleset gates nothing, but the parameters are still repaired
    # so the queue works the moment a human re-enables it.
    assert fake.puts[0]["rules"][1]["parameters"]["max_entries_to_merge"] == 1


def test_main_reds_when_the_repo_forbids_the_queues_merge_method(monkeypatch):
    fake = FakeGitHub(
        ruleset({**HEALTHY, "merge_method": "SQUASH"}),
        settings={**SETTINGS, "allow_squash_merge": False},
    )
    with pytest.raises(SystemExit, match="no queue group can merge"):
        run_main(monkeypatch, fake, ["--repo", "o/r"])
    assert fake.puts == []  # the parameters themselves are fine


def test_main_reds_when_the_queue_cannot_push_the_merged_commit(monkeypatch):
    fake = FakeGitHub(ruleset(bypass=[]))
    with pytest.raises(SystemExit, match="no bypass entry for the merge queue app"):
        run_main(monkeypatch, fake, ["--repo", "o/r"])


def test_main_reports_every_problem_in_one_exit(monkeypatch):
    # A run that stopped at the first problem would hide the rest of a state
    # nothing else records.
    fake = FakeGitHub(
        ruleset(
            {**HEALTHY, "merge_method": "SQUASH"}, enforcement="evaluate", bypass=[]
        ),
        settings={**SETTINGS, "allow_squash_merge": False},
    )
    with pytest.raises(SystemExit) as caught:
        run_main(monkeypatch, fake, ["--repo", "o/r"])
    reported = str(caught.value)
    assert "enforcement='evaluate'" in reported
    assert "no queue group can merge" in reported
    assert "no bypass entry for the merge queue app" in reported


def test_main_skip_bypass_check_asks_nothing_about_the_bypass(monkeypatch):
    # A repo whose ruleset does not refuse the queue's push opts out, and the
    # run then makes no app lookup at all.
    class NoAppLookup(FakeGitHub):
        def request(self, method, url, token, body=None):
            assert mod.MERGE_QUEUE_APP_SLUG not in url, "the opt-out must skip it"
            return super().request(method, url, token, body)

    fake = NoAppLookup(ruleset(bypass=[]))
    run_main(monkeypatch, fake, ["--repo", "o/r", "--skip-bypass-check"])
    assert fake.puts == []


def test_main_takes_an_explicit_ruleset_id_without_listing(monkeypatch):
    class NoListing(FakeGitHub):
        def request(self, method, url, token, body=None):
            assert not url.endswith("/rulesets"), "--ruleset-id must skip the listing"
            return super().request(method, url, token, body)

    run_main(monkeypatch, NoListing(ruleset()), ["--repo", "o/r", "--ruleset-id", "42"])

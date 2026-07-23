"""Tests for ci_truth_serum/check_cron_comment.py — the lint that fails a workflow whose
schedule comment (hourly/daily/weekly/monthly/every N …) contradicts the cron
expression it annotates. Ambiguity always passes: only a clean claim against a
clean shape can contradict.

Drives ``classify()`` for cron-shape parsing and ``violations()`` / ``main()``
for the comment pairing and exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_cron_comment.py", "check_cron_comment")


# ── classify ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "expr, expected",
    [
        ("0 * * * *", "hourly"),
        ("30 * * * *", "hourly"),
        ("*/15 * * * *", "every 15 minutes"),
        ("0 */4 * * *", "every 4 hours"),
        ("0 6 * * *", "daily"),
        ("0 9 * * 1", "weekly"),
        ("0 9 1 * *", "monthly"),
        # ambiguous / exotic shapes → None (never a finding)
        ("0 6 * * 1-5", None),  # range
        ("0 6,18 * * *", None),  # list
        ("0 6 * 1 *", None),  # fixed month
        ("0 6 */2 * *", None),  # step day-of-month
        ("* * * * *", None),  # every minute — unclassified
        ("0 6 * *", None),  # wrong field count
        ("@daily", None),  # macro form
    ],
)
def test_classify(expr: str, expected: str | None) -> None:
    assert mod.classify(expr) == expected


# ── violations ───────────────────────────────────────────────────────────
def _sched(comment: str, cron: str, trailing: str = "") -> str:
    return f'on:\n  schedule:\n    {comment}\n    - cron: "{cron}"{trailing}\n'


def test_daily_claim_on_weekly_cron_is_flagged() -> None:
    hits = mod.violations(_sched("# Run daily at 6am UTC", "0 6 * * 1"))
    assert [line for line, _ in hits] == [4]
    assert "daily" in hits[0][1] and "weekly" in hits[0][1]


def test_trailing_comment_claim_is_paired() -> None:
    hits = mod.violations(_sched("# schedule:", "0 6 * * 1", trailing=" # daily"))
    assert [line for line, _ in hits] == [4]


@pytest.mark.parametrize(
    "comment, cron",
    [
        ("# Run weekly, Monday at 9am UTC", "0 9 * * 1"),
        ("# daily at 06:00", "0 6 * * *"),
        ("# hourly", "0 * * * *"),
        ("# monthly report", "0 9 1 * *"),
        ("# every 15 minutes", "*/15 * * * *"),
        ("# every 4 hours", "0 */4 * * *"),
        ("# Mondays 06:00 UTC", "0 6 * * 1"),  # no cadence claim at all
        ("# daily-ish, see docs", "0 6,18 * * *"),  # unclassifiable cron passes
        ("# weekly", "0 9 * * MON"),  # named weekday — unclassifiable, passes
    ],
)
def test_consistent_or_ambiguous_pairs_pass(comment: str, cron: str) -> None:
    assert mod.violations(_sched(comment, cron)) == []


def test_claim_window_stops_at_a_sibling_cron_line() -> None:
    # The "# daily" belongs to the first schedule; the second (weekly) must not
    # inherit it.
    text = (
        "on:\n"
        "  schedule:\n"
        "    # daily\n"
        '    - cron: "0 6 * * *"\n'
        '    - cron: "0 6 * * 1"\n'
    )
    assert mod.violations(text) == []


def test_comment_more_than_three_lines_above_is_ignored() -> None:
    text = '# daily\nx: 1\ny: 2\nz: 3\n- cron: "0 6 * * 1"\n'
    assert mod.violations(text) == []


def test_opt_out_on_cron_line() -> None:
    text = _sched("# daily", "0 6 * * 1", trailing=" # cron-comment-ok")
    assert mod.violations(text) == []


# ── main ─────────────────────────────────────────────────────────────────
def _wire(tmp_path, monkeypatch, text: str):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "s.yaml").write_text(text)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")


def test_main_flags_contradiction(tmp_path, monkeypatch, capsys) -> None:
    _wire(tmp_path, monkeypatch, _sched("# daily", "0 6 * * 1"))
    assert mod.main() == 1
    assert "::error file=.github/workflows/s.yaml,line=4::" in capsys.readouterr().out


def test_main_clean_repo_passes(tmp_path, monkeypatch) -> None:
    _wire(tmp_path, monkeypatch, _sched("# weekly", "0 9 * * 1"))
    assert mod.main() == 0

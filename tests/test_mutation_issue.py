"""Tests for .github/scripts/mutation-issue.py — the daily mutation ledger.

They pin which jobs count as red, the body a red or survivor run writes, the
Stryker survivors it reads, and which `gh` writes `main()` chooses for each
state of the issue. A recorder on `_gh` stands in for GitHub, so the tests read
the calls and send none.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "mutation-issue.py"
_spec = importlib.util.spec_from_file_location("mutation_issue", _SRC)
mi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mi)


def test_red_jobs_counts_failed_and_cancelled_only() -> None:
    results = (
        "plan=success\nmutation=failure\naggregate=skipped\n\nmutation-js=cancelled\n"
    )
    assert mi.red_jobs(results) == ["mutation", "mutation-js"]


def test_red_jobs_refuses_an_unreadable_line() -> None:
    with pytest.raises(SystemExit, match="unreadable RESULTS line"):
        mi.red_jobs("mutation failure")


def test_body_carries_the_marker_failed_jobs_and_survivors() -> None:
    body = mi.render_body(
        ["mutation"], "### `check_a`\n\n- `x.py:3` `op`", "RUN", "abc", 12
    )
    assert body.startswith(mi.MARKER + "\n<!-- mutation-run: 12 -->\n\n")
    assert mi.stamp_of(body) == 12
    assert "## Failed jobs\n\n- `mutation`" in body
    assert body.endswith("## Surviving mutants\n\n### `check_a`\n\n- `x.py:3` `op`\n")
    assert "`abc`" in body and "RUN" in body


def test_body_omits_the_sections_a_run_has_nothing_for() -> None:
    body = mi.render_body([], "### `check_a`", "RUN", "abc", 1)
    assert "## Failed jobs" not in body
    assert "## Surviving mutants" in body


def test_body_cuts_the_survivors_to_fit_the_issue() -> None:
    survivors = "\n".join(f"- `x.py:{n}` `op`" for n in range(10_000))
    body = mi.render_body([], survivors, "RUN", "abc", 1)
    assert len(body) <= mi.BODY_LIMIT
    assert "\n- `x.py:0` `op`\n" in body
    assert body.endswith("artifact of the run holds every survivor: RUN\n")
    # The cut falls between two lines, never inside one.
    assert body.split("\n\nThis list stops here")[0].endswith("` `op`")


def test_body_keeps_survivors_that_fit_whole() -> None:
    survivors = "- `x.py:3` `op`"
    assert mi.render_body([], survivors, "RUN", "abc", 1).endswith(survivors + "\n")


def test_an_unstamped_body_counts_as_older_than_any_run() -> None:
    assert mi.stamp_of(f"{mi.MARKER}\n\nwritten before runs stamped the issue") == 0


def _mutant(line: int, column: int, mutator: str, status: str) -> dict:
    return {
        "id": f"{line}-{column}",
        "mutatorName": mutator,
        "location": {
            "start": {"line": line, "column": column},
            "end": {"line": line, "column": column + 3},
        },
        "status": status,
    }


def test_stryker_markdown_lists_undetected_mutants_in_source_order() -> None:
    report = {
        "files": {
            "scripts/b.mjs": {
                "mutants": [
                    _mutant(9, 2, "BlockStatement", "Survived"),
                    _mutant(4, 7, "ConditionalExpression", "NoCoverage"),
                    _mutant(4, 3, "EqualityOperator", "Survived"),
                    _mutant(5, 1, "StringLiteral", "Killed"),
                    _mutant(6, 1, "StringLiteral", "Timeout"),
                ]
            },
            "scripts/a.mjs": {"mutants": [_mutant(2, 5, "BooleanLiteral", "Survived")]},
            "scripts/c.mjs": {"mutants": [_mutant(1, 1, "StringLiteral", "Killed")]},
        }
    }
    assert mi.stryker_markdown(report) == (
        "### `scripts/a.mjs` (Stryker)\n\n"
        "- `scripts/a.mjs:2:5` `BooleanLiteral`\n\n"
        "### `scripts/b.mjs` (Stryker)\n\n"
        "- `scripts/b.mjs:4:3` `EqualityOperator`\n"
        "- `scripts/b.mjs:4:7` `ConditionalExpression` (no test covers it)\n"
        "- `scripts/b.mjs:9:2` `BlockStatement`"
    )


def test_stryker_markdown_is_empty_when_every_mutant_died() -> None:
    report = {"files": {"scripts/a.mjs": {"mutants": [_mutant(1, 1, "X", "Killed")]}}}
    assert mi.stryker_markdown(report) == ""


def _stub_gh(  # noqa: PLR0913 -- one argument per input that main() reads
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    survivors: str,
    open_issue: tuple[int, int] | None,
    closed_issue: tuple[int, int] | None,
    stryker: dict | None = None,
) -> list[tuple[str, ...]]:
    """Record every `gh` call, and answer each issue lookup from its --state.

    An issue is `(number, stamped run)`. This run is run 10.
    """
    calls: list[tuple[str, ...]] = []
    found = {"open": open_issue, "closed": closed_issue}

    def fake_gh(*args: str) -> str:
        calls.append(args)
        if args[:2] != ("issue", "list"):
            return ""
        issue = found[args[args.index("--state") + 1]]
        if issue is None:
            return "[]"
        number, run = issue
        return json.dumps([{"number": number, "body": mi.STAMP.format(run)}])

    monkeypatch.setattr(mi, "_gh", fake_gh)
    survivors_file = tmp_path / "survivors.md"
    if survivors:
        survivors_file.write_text(survivors, encoding="utf-8")
    stryker_file = tmp_path / "mutation.json"
    if stryker is not None:
        stryker_file.write_text(json.dumps(stryker), encoding="utf-8")
    for name, value in (
        ("RUN_URL", "RUN"),
        ("GITHUB_SHA", "abc"),
        ("GITHUB_RUN_NUMBER", "10"),
        ("SURVIVORS_FILE", str(survivors_file)),
        ("STRYKER_REPORT", str(stryker_file)),
    ):
        monkeypatch.setenv(name, value)
    return calls


@pytest.mark.parametrize(
    ("results", "survivors", "open_issue", "closed_issue", "writes"),
    [
        pytest.param(
            "gate=success\nmutation=success\n",
            "",
            (7, 9),
            None,
            [("issue", "edit", "7"), ("issue", "close", "7")],
            id="clean-closes-the-open-issue",
        ),
        pytest.param(
            "mutation=success\n",
            "",
            None,
            (9, 9),
            [],
            id="clean-with-none-open-writes-nothing",
        ),
        pytest.param(
            "mutation=failure\n",
            "",
            None,
            (9, 9),
            [("issue", "edit", "9"), ("issue", "reopen", "9")],
            id="red-reopens-the-closed-issue",
        ),
        pytest.param(
            "gate=failure\n",
            "",
            (4, 10),
            None,
            [("issue", "edit", "4")],
            id="red-gate-edits-the-open-issue-of-the-same-run",
        ),
        pytest.param(
            "mutation=success\n",
            "### `check_a`",
            None,
            None,
            [("issue", "create", "--title")],
            id="survivors-create-the-issue-when-none-exists",
        ),
        pytest.param(
            "mutation=failure\n",
            "",
            None,
            (9, 11),
            [],
            id="red-leaves-the-issue-a-newer-clean-run-closed",
        ),
        pytest.param(
            "mutation=success\n",
            "",
            (4, 11),
            None,
            [],
            id="clean-leaves-the-issue-a-newer-red-run-wrote",
        ),
    ],
)
def test_main_chooses_the_writes_for_each_issue_state(  # noqa: PLR0913 -- the parametrized cases
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    results: str,
    survivors: str,
    open_issue: tuple[int, int] | None,
    closed_issue: tuple[int, int] | None,
    writes: list[tuple[str, ...]],
) -> None:
    calls = _stub_gh(monkeypatch, tmp_path, survivors, open_issue, closed_issue)
    monkeypatch.setenv("RESULTS", results)
    mi.main()
    assert [c[:3] for c in calls if c[:2] != ("issue", "list")] == writes


def test_main_stamps_the_body_it_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _stub_gh(monkeypatch, tmp_path, "", (7, 9), None)
    monkeypatch.setenv("RESULTS", "mutation=success\n")
    mi.main()
    (edit,) = [c for c in calls if c[:2] == ("issue", "edit")]
    assert edit[3:] == ("--body", mi.render_clean_body("RUN", "abc", 10))
    assert mi.stamp_of(edit[4]) == 10


def test_main_writes_both_tools_survivors_into_the_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stryker = {
        "files": {"scripts/a.mjs": {"mutants": [_mutant(2, 5, "X", "Survived")]}}
    }
    calls = _stub_gh(monkeypatch, tmp_path, "### `check_a`\n", (4, 9), None, stryker)
    monkeypatch.setenv("RESULTS", "mutation=success\n")
    mi.main()
    (edit,) = [c for c in calls if c[:2] == ("issue", "edit")]
    survivors = "### `check_a`\n\n" + mi.stryker_markdown(stryker)
    assert edit[3:] == ("--body", mi.render_body([], survivors, "RUN", "abc", 10))


def test_main_opens_the_issue_for_stryker_survivors_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stryker = {
        "files": {"scripts/a.mjs": {"mutants": [_mutant(2, 5, "X", "Survived")]}}
    }
    calls = _stub_gh(monkeypatch, tmp_path, "", None, None, stryker)
    monkeypatch.setenv("RESULTS", "mutation-js=success\n")
    mi.main()
    assert [c[:2] for c in calls if c[:2] != ("issue", "list")] == [("issue", "create")]

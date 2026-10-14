"""Tests for ci_truth_serum/check_grant_wildcard.py — the lint that bans a Claude
Code `permissions.allow` shell rule whose `*` extends a word.

`Bash(git diff*)` matches `git difftool` as well as `git diff`, so an allow rule
written that way approves a command nobody wrote down. The rule is structural:
the character before any `*` must not be a letter or a digit.

Drives ``findings()`` over settings text decoded by ``_cts_claude_settings``, and
``main()`` for the exit-code and annotation contract.
"""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mod = load_hook("check_grant_wildcard.py", "check_grant_wildcard")
settings = load_hook("_cts_claude_settings.py", "_cts_claude_settings")


def _text(*allow: object, deny: list[str] | None = None) -> str:
    """Settings text whose allow list is ALLOW, one rule per line from line 4."""
    doc = {"permissions": {"allow": list(allow), "deny": deny or []}}
    return json.dumps(doc, indent=2) + "\n"


def _lines(text: str) -> list[int]:
    return [line for line, _ in mod.findings(settings.decode(text), text)]


@pytest.mark.parametrize(
    "pattern",
    [
        "docker compose*",
        "docker build*",
        "git diff*",
        "git show*",
        "git merge*",
        "git checkout*",
        "git fetch*",
        "git remote -v*",
        # a bare program name with no space at all
        "docker*",
        # a digit before the wildcard is still part of the word
        "python3*",
        # the wildcard need not be last
        "git diff* --stat",
        # a `*` inside a word
        "git d*ff",
        # a later `*` extends a word even when an earlier one does not
        "git log * main*",
    ],
)
def test_fires_on_a_word_extending_wildcard(pattern: str) -> None:
    assert _lines(_text(f"Bash({pattern})")) == [4]


@pytest.mark.parametrize(
    "pattern",
    [
        # the space is part of the rule, and the rule still matches the bare command
        "git diff *",
        "docker build *",
        "make *",
        # no wildcard at all
        "git diff",
        "git remote -v",
        # the `:*` suffix is the same as a trailing ` *`
        "pnpm test:*",
        "npm run lint:*",
        # a `-` before the wildcard: still `git config`, never a new command
        "git config --get-*",
        # a wildcard that stands for a path argument
        "python *.py",
        "python3 *.py *",
        "./scripts/ci-*",
        # a leading wildcard has no character before it
        "*",
        "*.sh",
        "* --version",
    ],
)
def test_a_wildcard_that_starts_at_a_delimiter_passes(pattern: str) -> None:
    assert _lines(_text(f"Bash({pattern})")) == []


def test_powershell_rules_follow_the_same_shape() -> None:
    text = _text("PowerShell(Get-Child*)", "PowerShell(Get-ChildItem *)")
    assert _lines(text) == [4]


@pytest.mark.parametrize(
    "rule",
    [
        "Read(//tmp/scratch*)",
        "Edit(src/gen*)",
        "WebFetch(domain:example*)",
        "mcp__puppeteer__*",
        "Bash",
        "Bash(git diff*",
        "bash(git diff*)",
    ],
)
def test_a_rule_that_is_not_a_shell_rule_is_out_of_scope(rule: str) -> None:
    assert _lines(_text(rule)) == []


def test_deny_and_ask_rules_are_out_of_scope() -> None:
    doc = {
        "permissions": {
            "allow": ["Bash(git diff)"],
            "deny": ["Bash(*iptables*)", "Bash(rm -rf*)"],
            "ask": ["Bash(git push*)"],
        }
    }
    assert _lines(json.dumps(doc, indent=2)) == []


def test_reports_every_offending_rule_at_its_own_line() -> None:
    text = _text("Bash(git diff)", "Bash(git show*)", "Bash(make *)", "Bash(docker*)")
    assert _lines(text) == [5, 7]


def test_an_escaped_rule_is_reported_at_its_own_line() -> None:
    text = '{\n  "permissions": {\n    "allow": [\n      "Bash(git diff\\u002a)"\n    ]\n  }\n}\n'
    assert json.loads(text)["permissions"]["allow"] == ["Bash(git diff*)"]
    assert _lines(text) == [4]


def test_two_identical_rules_each_report_their_own_line() -> None:
    assert _lines(_text("Bash(docker*)", "Bash(git diff)", "Bash(docker*)")) == [4, 6]


@pytest.mark.parametrize(
    "doc",
    [
        {"hooks": {}},
        {"permissions": "nope"},
        {"permissions": {"deny": ["Bash(rm -rf*)"]}},
        {"permissions": {"allow": "Bash(git diff*)"}},
        {"permissions": {"allow": [3, None, {"rule": "Bash(git diff*)"}]}},
    ],
)
def test_a_shape_that_holds_no_allow_rule_gives_no_finding(doc: dict) -> None:
    assert _lines(json.dumps(doc)) == []


def test_malformed_json_fails_loud() -> None:
    with pytest.raises(json.JSONDecodeError):
        _lines('{"permissions": {"allow": [')


def test_main_reports_the_rule_and_the_remedy_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "settings.json"
    bad.write_text(_text("Bash(docker compose*)"), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        mod.main([str(bad)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"::error file={bad},line=4::the allow rule `Bash(docker compose*)`" in out
    assert "`Bash(git diff *)`" in out


def test_main_is_silent_and_returns_on_a_clean_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "settings.json"
    good.write_text(_text("Bash(git diff *)", "Bash(pnpm test:*)"), encoding="utf-8")
    mod.main([str(good)])
    assert capsys.readouterr() == ("", "")


def test_main_says_when_there_is_no_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    mod.main([])
    assert "scanned nothing" in capsys.readouterr().err


_RULE = st.one_of(
    st.text(alphabet="Bash()*: -gitdf3.", max_size=16),
    st.builds(lambda p: f"Bash({p})", st.text(alphabet="* :-ab1.", max_size=10)),
    st.none(),
    st.integers(),
)


@given(allow=st.lists(_RULE, max_size=6), indent=st.sampled_from([None, 2]))
def test_findings_never_raise_and_each_names_a_real_rule_line(
    allow: list[object], indent: int | None
) -> None:
    text = json.dumps({"permissions": {"allow": allow}}, indent=indent)
    hits = mod.findings(settings.decode(text), text)
    lines = text.splitlines()
    offending = [
        r
        for r in allow
        if isinstance(r, str)
        and (pattern := mod.command_pattern(r)) is not None
        and mod.extends_a_word(pattern)
    ]
    assert len(hits) == len(offending)
    for line, message in hits:
        assert "Bash(" in lines[line - 1]
        assert "`*`" in message

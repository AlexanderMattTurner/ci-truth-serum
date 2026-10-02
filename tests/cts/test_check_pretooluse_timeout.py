"""Tests for ci_truth_serum/check_pretooluse_timeout.py — the lint that requires an
explicit numeric `timeout` on every Claude Code PreToolUse hook entry.

Drives ``findings()`` over settings text decoded by ``_cts_claude_settings``, and
``main()`` for the argv, discovery and exit-code contract. The property tests at
the end also cover the located decoder both settings checks share.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import HOOKS_DIR, load_hook

mod = load_hook("check_pretooluse_timeout.py", "check_pretooluse_timeout")
settings = load_hook("_cts_claude_settings.py", "_cts_claude_settings")


def _text(*entries: object, event: str = "PreToolUse") -> str:
    """Settings text with ENTRIES in one hook group, one JSON value per line."""
    doc = {"hooks": {event: [{"matcher": "", "hooks": list(entries)}]}}
    return json.dumps(doc, indent=2) + "\n"


def _findings(text: str) -> list[tuple[int, str]]:
    return mod.findings(settings.decode(text), text)


def _lines(text: str) -> list[int]:
    return [line for line, _ in _findings(text)]


# In `_text`'s indent=2 encoding the first entry's `{` is on line 7.
_FIRST = 7


def test_flags_command_without_timeout() -> None:
    hits = _findings(_text({"type": "command", "command": "node gate.mjs"}))
    assert [line for line, _ in hits] == [_FIRST]
    assert "hooks.PreToolUse[0].hooks[0]" in hits[0][1]
    assert "gate.mjs" in hits[0][1]


@pytest.mark.parametrize("timeout", [1800, 12.5, 0.5])
def test_accepts_a_numeric_timeout(timeout: float) -> None:
    entry = {"type": "command", "command": "node gate.mjs", "timeout": timeout}
    assert _lines(_text(entry)) == []


@pytest.mark.parametrize(
    "timeout", [True, False, "600", None, [600], {"s": 600}, 0, -5, -0.5]
)
def test_a_non_positive_or_non_number_is_not_a_timeout(timeout: object) -> None:
    # A JSON boolean decodes to a Python bool, which is an int subclass.
    entry = {"type": "command", "command": "x", "timeout": timeout}
    assert _lines(_text(entry)) == [_FIRST]


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "prompt", "prompt": "judge this"},
        {"type": "agent", "prompt": "review the call"},
        {"type": "http", "url": "https://gate.example/check"},
        {"type": "mcp_tool", "tool": "gate"},
    ],
)
def test_every_hook_type_needs_a_timeout(entry: dict) -> None:
    assert _lines(_text(entry)) == [_FIRST]


def test_an_async_hook_is_exempt() -> None:
    # Claude Code does not enforce a timeout on an async command hook.
    entry = {"type": "command", "command": "log.sh", "async": True}
    assert _lines(_text(entry)) == []
    assert _lines(_text({**entry, "async": False})) == [_FIRST]


@pytest.mark.parametrize("event", ["PostToolUse", "Stop", "UserPromptSubmit"])
def test_only_pretooluse_is_checked(event: str) -> None:
    assert _lines(_text({"type": "command", "command": "x"}, event=event)) == []


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"hooks": "nope"},
        {"hooks": {"PreToolUse": "not-a-list"}},
        {"hooks": {"PreToolUse": ["not-a-dict"]}},
        {"hooks": {"PreToolUse": [{"hooks": "not-a-list"}]}},
        {"hooks": {"PreToolUse": [{"hooks": ["not-a-dict", 3, None]}]}},
    ],
)
def test_a_shape_that_holds_no_entry_gives_no_finding(doc: dict) -> None:
    assert _lines(json.dumps(doc)) == []


def test_locators_count_skipped_elements_and_each_entry_has_its_own_line() -> None:
    text = (
        "{\n"
        '  "hooks": {\n'
        '    "PreToolUse": [\n'
        '      "stray",\n'
        '      {"hooks": [\n'
        '        {"command": "a", "timeout": 5},\n'
        '        {"command": "b"}\n'
        "      ]},\n"
        '      {"hooks": [{"command": "c"}]}\n'
        "    ]\n"
        "  }\n"
        "}\n"
    )
    hits = _findings(text)
    assert [line for line, _ in hits] == [7, 9]
    assert "hooks.PreToolUse[1].hooks[1] (command='b')" in hits[0][1]
    assert "hooks.PreToolUse[2].hooks[0] (command='c')" in hits[1][1]


def test_a_duplicate_key_resolves_as_json_loads_does() -> None:
    # The last `hooks` wins, as it does for the JSON parser Claude Code uses.
    text = (
        '{"hooks": {"PreToolUse": [{"hooks": [{"command": "a", "timeout": 5}]}]},\n'
        ' "hooks": {"PreToolUse": [{"hooks": [{"command": "b"}]}]}}\n'
    )
    assert _lines(text) == [2]


def test_malformed_json_fails_loud() -> None:
    with pytest.raises(json.JSONDecodeError):
        _findings('{"hooks": {"PreToolUse": [')


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_main_reports_each_finding_as_an_annotation_and_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = _write(tmp_path / "settings.json", _text({"command": "node gate.mjs"}))
    with pytest.raises(SystemExit) as exc:
        mod.main([str(bad)])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"::error file={bad},line={_FIRST}::hooks.PreToolUse[0].hooks[0]" in out
    assert "1 violation(s) found" in out


def test_main_is_silent_and_returns_on_a_clean_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _write(tmp_path / "s.json", _text({"command": "x", "timeout": 30}))
    mod.main([str(good)])
    assert capsys.readouterr() == ("", "")


def test_main_checks_every_file_before_exiting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    one = _write(tmp_path / "a.json", _text({"command": "a"}))
    two = _write(tmp_path / "b.json", _text({"command": "b"}))
    with pytest.raises(SystemExit):
        mod.main([str(one), str(two)])
    out = capsys.readouterr().out
    assert f"file={one}," in out and f"file={two}," in out


def test_main_reads_both_project_settings_files_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / ".claude/settings.json", _text({"command": "shared"}))
    _write(tmp_path / ".claude/settings.local.json", _text({"command": "local"}))
    with pytest.raises(SystemExit):
        mod.main([])
    out = capsys.readouterr().out
    assert "file=.claude/settings.json," in out
    assert "file=.claude/settings.local.json," in out


def test_main_says_when_there_is_no_settings_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    mod.main([])
    assert "scanned nothing" in capsys.readouterr().err


def test_main_fails_loud_on_a_named_file_that_is_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        mod.main([str(tmp_path / "absent.json")])


def test_main_refuses_a_top_level_that_is_not_an_object(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.json", "[]\n")
    with pytest.raises(ValueError, match="not a JSON object"):
        mod.main([str(path)])


def test_the_script_runs_as_pre_commit_runs_it(tmp_path: Path) -> None:
    bad = _write(tmp_path / "settings.json", _text({"command": "x"}))
    done = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_pretooluse_timeout.py"), str(bad)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 1, done.stderr
    assert f"file={bad},line={_FIRST}" in done.stdout


# ── Property tests: the located decoder and the check never disagree with JSON ──

_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=8),
)
_JSON = st.recursive(
    _SCALARS,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.text(max_size=6), inner, max_size=4),
    ),
    max_leaves=20,
)


def _located(value: object) -> list[object]:
    """Every object and string value inside VALUE, depth first."""
    found: list[object] = []
    if isinstance(value, (dict, str)):
        found.append(value)
    if isinstance(value, dict):
        for item in value.values():
            found += _located(item)
    elif isinstance(value, list):
        for item in value:
            found += _located(item)
    return found


@given(value=_JSON, indent=st.sampled_from([None, 0, 2]), ascii=st.booleans())
def test_decode_matches_json_loads_and_each_offset_opens_its_value(
    value: object, indent: int | None, ascii: bool
) -> None:
    text = json.dumps(value, indent=indent, ensure_ascii=ascii)
    decoded = settings.decode(text)
    assert decoded == json.loads(text)
    for node in _located(decoded):
        opener = "{" if isinstance(node, dict) else '"'
        assert text[node.offset] == opener
        assert 1 <= settings.line_of(text, node.offset) <= text.count("\n") + 1


_ENTRY = st.dictionaries(
    st.sampled_from(["type", "command", "prompt", "timeout", "async"]),
    st.one_of(_SCALARS, st.lists(_SCALARS, max_size=2)),
    max_size=5,
)


@given(
    groups=st.lists(
        st.one_of(
            st.fixed_dictionaries({"hooks": st.lists(st.one_of(_ENTRY, _SCALARS))}),
            _SCALARS,
        ),
        max_size=4,
    )
)
def test_findings_never_raise_and_name_real_lines(groups: list[object]) -> None:
    text = json.dumps({"hooks": {"PreToolUse": groups}}, indent=1)
    hits = mod.findings(settings.decode(text), text)
    lines = text.splitlines()
    for line, _ in hits:
        assert lines[line - 1].lstrip().startswith("{")
    assert mod.findings(settings.decode(text), text) == hits

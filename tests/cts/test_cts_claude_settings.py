"""Tests for ci_truth_serum/_cts_claude_settings.py, the shared reader of Claude
Code settings files: a JSON decoder that keeps where each value starts, and the
command-line driver the settings checks share."""

import json

import pytest

from tests._helpers import load_hook

cs = load_hook("_cts_claude_settings.py", "cts_claude_settings")

_TEXT = (
    '{\n  "hooks": {\n    "PreToolUse": [\n      {"matcher": "Bash"}\n    ]\n  }\n}\n'
)


@pytest.mark.parametrize(
    "text",
    [_TEXT, '{"a": 1, "a": 2}', '["x", {"y": "z"}]', '"bare"', "3", "null"],
)
def test_decode_returns_what_json_loads_returns(text):
    assert cs.decode(text) == json.loads(text)


def test_objects_and_string_values_carry_their_offsets():
    doc = cs.decode(_TEXT)
    entry = doc["hooks"]["PreToolUse"][0]
    assert doc.offset == 0
    assert _TEXT[entry.offset] == "{"
    assert cs.line_of(_TEXT, entry.offset) == 4
    assert _TEXT[entry["matcher"].offset : entry["matcher"].offset + 6] == '"Bash"'
    assert all(type(key) is str for key in doc)


def test_malformed_json_raises_like_json_loads():
    with pytest.raises(json.JSONDecodeError):
        cs.decode('{"a": }')


@pytest.mark.parametrize(("offset", "line"), [(0, 1), (2, 2), (len(_TEXT) - 1, 7)])
def test_line_of_counts_newlines_before_the_offset(offset, line):
    assert cs.line_of(_TEXT, offset) == line


def test_named_files_are_taken_as_given(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cs.settings_paths(["a.json", "b.json"]) == [
        cs.Path("a.json"),
        cs.Path("b.json"),
    ]


def test_defaults_are_the_project_settings_files_that_exist(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
    assert cs.settings_paths([]) == [cs.Path(".claude/settings.local.json")]


def test_no_settings_file_says_it_scanned_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cs.settings_paths([]) == []
    assert "scanned nothing" in capsys.readouterr().err


def _flag_every_matcher(doc, text):
    return [
        (cs.line_of(text, entry.offset), f"matcher {entry['matcher']}")
        for entry in doc.get("hooks", {}).get("PreToolUse", [])
    ]


def test_run_prints_each_finding_and_exits_one(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text(_TEXT, encoding="utf-8")
    with pytest.raises(SystemExit) as exit_info:
        cs.run([str(path)], "d", _flag_every_matcher, "the summary")
    assert exit_info.value.code == 1
    out = capsys.readouterr().out
    assert f"::error file={path},line=4::matcher Bash" in out
    assert "1 violation(s) found.\nthe summary" in out


def test_run_returns_quietly_when_nothing_fires(tmp_path, capsys):
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    assert cs.run([str(path)], "d", _flag_every_matcher, "s") is None
    assert capsys.readouterr().out == ""


def test_run_refuses_a_file_whose_top_level_is_not_an_object(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="not a JSON object"):
        cs.run([str(path)], "d", _flag_every_matcher, "s")

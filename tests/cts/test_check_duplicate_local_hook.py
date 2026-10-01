"""Tests for ci_truth_serum/check_duplicate_local_hook.py — the lint that refuses
a `repo: local` pre-commit hook whose id or command is defined twice.

Drives ``findings()`` over small configs for each rule, then ``main()`` over a
real file for the CLI contract.
"""

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

mod = load_hook("check_duplicate_local_hook.py", "check_duplicate_local_hook")


def _config(*hooks: str, remote: str = "") -> str:
    """A config whose one local repo holds HOOKS, each a block of hook lines."""
    body = "".join(hooks)
    return f"repos:\n{remote}  - repo: local\n    hooks:\n{body}"


def _hook(hook_id: str, entry: str, extra: str = "", above: str = "") -> str:
    lines = [above] if above else []
    lines += [f"      - id: {hook_id}{extra}", f"        entry: {entry}"]
    lines += ["        language: system"]
    return "\n".join(lines) + "\n"


def _lines(text: str) -> list[int]:
    return [line for line, _ in mod.findings(text)]


def test_a_repeated_id_is_reported_on_its_second_copy() -> None:
    text = _config(_hook("a", "x.py"), _hook("a", "y.py"))
    [(line, message)] = mod.findings(text)
    assert line == 7
    assert "`a` is defined 2 times" in message
    assert "no opt-out" in message


def test_distinct_ids_and_commands_report_nothing() -> None:
    assert mod.findings(_config(_hook("a", "x.py"), _hook("b", "y.py"))) == []


def test_a_repeated_command_is_reported() -> None:
    [(line, message)] = mod.findings(_config(_hook("a", "x.py"), _hook("b", "x.py")))
    assert line == 7
    assert "`a`, `b` all run `x.py`" in message
    assert "# duplicate-hook-ok: <reason>" in message


def test_a_repeated_id_and_command_report_both() -> None:
    text = _config(_hook("a", "x.py"), _hook("a", "x.py"))
    assert len(mod.findings(text)) == 2


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (None, "x.py"),
        ([], "x.py"),
        (["--check"], "x.py --check"),
        (["--fix", "-v"], "x.py --fix -v"),
    ],
)
def test_command_is_the_entry_plus_its_args(args, expected: str) -> None:
    hook: dict[str, object] = {"id": "h", "entry": "x.py"}
    if args is not None:
        hook["args"] = args
    assert mod.command_of(hook) == expected


def test_different_args_make_different_commands() -> None:
    second = _hook("b", "x.py", "") + "        args: [--fast]\n"
    assert mod.findings(_config(_hook("a", "x.py"), second)) == []


@pytest.mark.parametrize(
    ("extra", "above", "expected"),
    [
        # On the id line, with a reason.
        ("  # duplicate-hook-ok: a second pass over the docs", "", []),
        # In the comment block above the hook.
        ("", "      # duplicate-hook-ok: a second pass over the docs", []),
        # A bare marker states nothing.
        ("  # duplicate-hook-ok:", "", [7]),
        ("  # duplicate-hook-ok", "", [7]),
        # Another comment is not the marker.
        ("  # some other note", "", [7]),
    ],
)
def test_the_opt_out_needs_a_reason(extra: str, above: str, expected) -> None:
    text = _config(_hook("a", "x.py"), _hook("b", "x.py", extra, above))
    assert _lines(text) == expected


def test_a_marker_in_the_entry_line_counts() -> None:
    second = _hook("b", "x.py  # duplicate-hook-ok: a second pass")
    assert mod.findings(_config(_hook("a", "x.py"), second)) == []


def test_a_marker_inside_a_quoted_value_is_content() -> None:
    second = _hook("b", "x.py") + '        name: "# duplicate-hook-ok: not a comment"\n'
    assert _lines(_config(_hook("a", "x.py"), second)) == [7]


def test_an_excused_pair_does_not_admit_a_third_copy() -> None:
    """Excusing a hook drops it from its command group rather than dissolving the
    group, so an accidental third copy is still reported."""
    marked = _hook("b", "x.py", "  # duplicate-hook-ok: a second pass")
    text = _config(_hook("a", "x.py"), marked, _hook("c", "x.py"))
    [(line, message)] = mod.findings(text)
    assert line == 10
    assert "`a`, `b`, `c` all run `x.py`" in message


def test_an_excused_id_is_still_reported() -> None:
    marked = _hook("a", "y.py", "  # duplicate-hook-ok: a second pass")
    assert _lines(_config(_hook("a", "x.py"), marked)) == [7]


def test_a_quoted_id_is_the_name_yaml_gives_it() -> None:
    text = _config(_hook('"a"', "x.py"), _hook("a", "y.py"))
    assert "`a` is defined 2 times" in mod.findings(text)[0][1]


def test_a_hook_outside_a_local_repo_is_not_read() -> None:
    remote = (
        "  - repo: https://example.invalid/x\n    rev: v1\n    hooks:\n      - id: a\n"
    )
    text = _config(_hook("a", "x.py"), remote=remote)
    assert [hook.hook_id for hook in mod.local_hooks(text)] == ["a"]
    assert mod.findings(text) == []


def test_a_config_with_no_local_hooks_is_clean() -> None:
    assert mod.findings("repos: []\n") == []


@pytest.mark.parametrize(
    "text",
    [
        "repos: {}\n",
        "- a\n",
        "repos:\n  - repo: local\n",
        "repos:\n  - repo: local\n    hooks:\n      - entry: x.py\n",
        "repos:\n  - repo: local\n    hooks:\n      - id: a\n        args: --x\n",
    ],
)
def test_a_config_pre_commit_would_refuse_fails_loud(text: str) -> None:
    with pytest.raises(ValueError):
        mod.findings(text)


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        # The id rule.
        (
            _config(_hook("a", "x.py"), _hook("a", "y.py")),
            _config(_hook("a", "x.py"), _hook("b", "y.py")),
        ),
        # The command rule.
        (
            _config(_hook("a", "x.py"), _hook("b", "x.py")),
            _config(_hook("a", "x.py"), _hook("b", "y.py")),
        ),
        # The opt-out.
        (
            _config(_hook("a", "x.py"), _hook("b", "x.py")),
            _config(_hook("a", "x.py"), _hook("b", "x.py", "  # duplicate-hook-ok: x")),
        ),
    ],
)
def test_each_rule_decides_a_verdict(text: str, clean: str) -> None:
    """Non-vacuity: each pair differs in one rule, and the verdict flips with it."""
    assert (len(mod.findings(text)), mod.findings(clean)) == (1, [])


def test_main_reads_the_config_and_names_the_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".pre-commit-config.yaml").write_text(
        _config(_hook("a", "x.py"), _hook("a", "y.py")), encoding="utf-8"
    )
    assert mod.main([]) == 1
    assert ".pre-commit-config.yaml:7: local hook id `a`" in capsys.readouterr().err


def test_main_takes_a_config_path(tmp_path) -> None:
    clean = tmp_path / "clean.yaml"
    clean.write_text(_config(_hook("a", "x.py")), encoding="utf-8")
    assert mod.main(["--config", str(clean)]) == 0


def test_main_fails_loud_without_a_config(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        mod.main([])


_HOOK_IDS = st.sampled_from(["a", "b", '"a"', "c"])
_ENTRIES = st.sampled_from(["x.py", "y.py", "x.py  # duplicate-hook-ok: why"])


@given(st.lists(st.tuples(_HOOK_IDS, _ENTRIES), min_size=1, max_size=6))
def test_findings_name_real_hook_lines(pairs) -> None:
    """Fuzz: any generated config yields findings only on lines that start a hook."""
    text = _config(*(_hook(hook_id, entry) for hook_id, entry in pairs))
    starts = {hook.start for hook in mod.local_hooks(text)}
    assert {line for line, _ in mod.findings(text)} <= starts


@given(st.text(max_size=200))
def test_findings_on_any_text_return_or_raise_a_yaml_or_value_error(text) -> None:
    """Fuzz: arbitrary text never crashes with an unexpected exception."""
    try:
        assert isinstance(mod.findings(text), list)
    except (ValueError, yaml.YAMLError):
        pass

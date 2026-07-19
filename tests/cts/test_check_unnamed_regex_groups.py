"""Tests for hooks/check_unnamed_regex_groups.py — the pre-commit lint that bans
unnamed capture groups in regex literals passed to re.* calls.

Drives the module's functions directly so every branch (literal detection, the
re.* call shape filter, and main()'s exit code) is asserted in isolation.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_unnamed_regex_groups.py", "check_unnamed_regex_groups")


@pytest.mark.parametrize(
    "pattern, unnamed",
    [
        ("(foo)", True),  # bare capture group
        ("(?P<name>foo)", False),  # named -> fine
        ("(?:foo)", False),  # non-capturing -> fine
        ("(?P<a>x)(b)", True),  # one named, one unnamed -> still flagged
        ("plain", False),  # no groups
        ("(unbalanced", False),  # re.error -> not flagged (can't compile)
    ],
)
def test_has_unnamed_group(pattern: str, unnamed: bool) -> None:
    assert mod._has_unnamed_group(pattern) is unnamed


def test_literal_str_extracts_only_string_constants() -> None:
    import ast

    assert mod._literal_str(ast.parse("'x'", mode="eval").body) == "x"
    assert mod._literal_str(ast.parse("123", mode="eval").body) is None


def _check_source(tmp_path: Path, source: str) -> list[tuple[int, str]]:
    path = tmp_path / "sample.py"
    path.write_text(source, encoding="utf-8")
    return mod.check_file(path)


def test_check_file_flags_unnamed_group(tmp_path: Path) -> None:
    assert _check_source(tmp_path, "import re\nre.search('(foo)', s)\n") == [
        (2, "(foo)")
    ]


@pytest.mark.parametrize(
    "source",
    [
        "import re\nre.search('(?P<name>foo)', s)\n",  # named group
        "import re\nre.compile('(?:foo)')\n",  # non-capturing
        "re.search(pattern, s)\n",  # non-literal first arg -> can't evaluate
        "re.unknown('(foo)', s)\n",  # attr not in _RE_FUNCS
        "other.search('(foo)', s)\n",  # not the `re` module
        "import re\nre.compile()\n",  # re.* call in _RE_FUNCS but no args
        "foo('(bar)')\n",  # not an attribute call at all
    ],
)
def test_check_file_ignores_safe_or_unrelated_calls(
    tmp_path: Path, source: str
) -> None:
    assert _check_source(tmp_path, source) == []


@pytest.mark.parametrize(
    "source, lineno",
    [
        # `import re as x` — an unnamed group via the module alias must be caught.
        ("import re as rex\nrex.search('(foo)', s)\n", 2),
        # `from re import compile` — a bare `compile(...)` call must be caught.
        ("from re import compile\ncompile('(bar)')\n", 2),
        # `from re import search as s` — the aliased function name too.
        ("from re import search as s\ns('(baz)', x)\n", 2),
    ],
)
def test_check_file_flags_aliased_re_imports(
    tmp_path: Path, source: str, lineno: int
) -> None:
    result = _check_source(tmp_path, source)
    assert len(result) == 1 and result[0][0] == lineno


@pytest.mark.parametrize(
    "source",
    [
        # Same aliases, but NAMED groups — these must stay clean, so the positives
        # above aren't just firing on the alias unconditionally (non-vacuous pair).
        "import re as rex\nrex.search('(?P<n>foo)', s)\n",
        "from re import compile\ncompile('(?P<n>bar)')\n",
        # a name that happens to match an re func but is NOT imported from re
        "from os import compile\ncompile('(bar)')\n",
        # a bare `compile('(x)')` with no `from re import` binding it
        "compile('(x)')\n",
    ],
)
def test_check_file_alias_resolution_is_not_overbroad(
    tmp_path: Path, source: str
) -> None:
    assert _check_source(tmp_path, source) == []


def test_check_file_unreadable_path_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope.py"
    assert mod.check_file(missing) == []
    assert "cannot read file" in capsys.readouterr().err


def test_check_file_syntax_error_returns_empty(tmp_path: Path) -> None:
    assert _check_source(tmp_path, "def (:\n") == []


def test_main_returns_one_on_violation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import re\nre.match('(x)', s)\n", encoding="utf-8")
    monkeypatch.setattr(mod.sys, "argv", ["check_unnamed_regex_groups.py", str(bad)])
    assert mod.main() == 1
    assert "unnamed capture group" in capsys.readouterr().out


def test_main_returns_zero_when_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = tmp_path / "good.py"
    good.write_text("import re\nre.match('(?P<x>y)', s)\n", encoding="utf-8")
    monkeypatch.setattr(mod.sys, "argv", ["check_unnamed_regex_groups.py", str(good)])
    assert mod.main() == 0

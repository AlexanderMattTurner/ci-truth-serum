"""Tests for ci_truth_serum/check_counted_repo_root.py — the lint that flags a path
found by counting the parents of `__file__`.

Drives `violations()` for the parsing rules and `main()` for the argv/exit-code
contract, so each test binds to the verdict and not to the check's own text.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import HOOKS_DIR, load_hook

mod = load_hook("check_counted_repo_root.py", "check_counted_repo_root")

_HEAD = "from pathlib import Path\n\n"


@pytest.mark.parametrize(
    "name, body",
    [
        ("resolved, then counted", "ROOT = Path(__file__).resolve().parents[3]\n"),
        ("counted without resolve", "ROOT = Path(__file__).parents[2]\n"),
        ("absolute, then counted", "ROOT = Path(__file__).absolute().parents[1]\n"),
        ("a computed depth", "ROOT = Path(__file__).parents[DEPTH]\n"),
        ("os.path spelling", "ROOT = Path(os.path.abspath(__file__)).parents[2]\n"),
        ("inside a call", "sys.path.insert(0, str(Path(__file__).parents[1]))\n"),
    ],
)
def test_a_root_counted_off_dunder_file_is_flagged(name: str, body: str) -> None:
    assert mod.violations(_HEAD + body) == [3], name


@pytest.mark.parametrize(
    "name, body",
    [
        ("a path from elsewhere", "def f(found):\n    return found.parents[1]\n"),
        (
            "a parent chain",
            "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n",
        ),
        ("a marker-based helper", "ROOT = repo_root(Path(__file__))\n"),
        ("the cwd", "ROOT = Path.cwd()\n"),
        ("the whole parents sequence", "for p in Path(__file__).parents:\n    pass\n"),
        ("a string that spells it", 'DOC = "Path(__file__).parents[3]"\n'),
        ("a comment that spells it", "# ROOT = Path(__file__).parents[3]\n"),
    ],
)
def test_a_path_not_counted_off_dunder_file_passes(name: str, body: str) -> None:
    assert mod.violations(_HEAD + body) == [], name


def test_each_counted_root_is_reported_at_its_own_line() -> None:
    src = _HEAD + "A = Path(__file__).parents[1]\nB = Path(__file__).parents[2]\n"
    assert mod.violations(src) == [3, 4]


def test_a_deliberate_count_opts_out_with_a_reason() -> None:
    src = _HEAD + "ROOT = Path(__file__).parents[1]  # allow-counted-root: a sibling\n"
    assert mod.violations(src) == []


def test_the_opt_out_works_on_the_line_above() -> None:
    src = _HEAD + "# allow-counted-root: a sibling\nROOT = Path(__file__).parents[1]\n"
    assert mod.violations(src) == []


def test_a_marker_on_any_line_the_subscript_spans_counts() -> None:
    src = _HEAD + (
        "ROOT = Path(\n    __file__  # allow-counted-root: deliberate\n).parents[3]\n"
    )
    assert mod.violations(src) == []


def test_an_opt_out_with_no_reason_does_not_suppress() -> None:
    src = _HEAD + "ROOT = Path(__file__).parents[1]  # allow-counted-root\n"
    assert mod.violations(src) == [3]


def test_a_file_that_does_not_parse_is_still_read_line_by_line() -> None:
    src = _HEAD + "def f(:\nROOT = Path(__file__).parents[2]\n"
    assert mod.violations(src) == [4]


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_counted_repo_root.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_and_line_and_names_the_remedy(tmp_path: Path) -> None:
    script = tmp_path / "x.py"
    script.write_text(_HEAD + "ROOT = Path(__file__).parents[3]\n", encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert f"{script}:3:" in result.stderr
    assert ".git" in result.stderr
    assert "allow-counted-root" in result.stderr


def test_main_exits_zero_on_a_clean_file(tmp_path: Path) -> None:
    script = tmp_path / "x.py"
    script.write_text(_HEAD + "ROOT = Path.cwd()\n", encoding="utf-8")
    assert _run(script).returncode == 0


def test_main_refuses_a_file_that_does_not_parse(tmp_path: Path) -> None:
    """An unparseable file is never a pass: the tree the rule reads was not built."""
    script = tmp_path / "x.py"
    script.write_text("def f(:\n", encoding="utf-8")
    result = _run(script)
    assert result.returncode == 1
    assert "cannot parse" in result.stderr


@given(st.text(max_size=300))
def test_fuzz_violations_returns_line_findings_or_a_known_error(text) -> None:
    """Fuzz: any text yields findings on real lines, or one of the declared errors."""
    try:
        found = mod.violations(text)
    except (SyntaxError, ValueError):
        return
    lines = [item[0] if isinstance(item, tuple) else item for item in found]
    assert all(1 <= line <= text.count("\n") + 1 for line in lines)

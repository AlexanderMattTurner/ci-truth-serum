"""The driver refuses a Python file this interpreter cannot parse.

A hook environment older than the tree is the case that bites: a PEP 701
f-string is a syntax error before Python 3.12, and a scope-dependent lint
falling back to a per-line parse then reads a function-local statement as a
module-level one. That is a confident wrong answer, so the driver reports the
file once and never runs the detector over it.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO_ROOT / "ci_truth_serum"))

import _cts_linecheck as linecheck  # noqa: E402

from ci_truth_serum import check_duplicate_module_constant as dmc  # noqa: E402

UNPARSEABLE = "def f(:\n    pass\n"


def test_an_unparseable_python_file_is_reported_not_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = tmp_path / "x.py"
    module.write_text(UNPARSEABLE, encoding="utf-8")
    status = linecheck.run_source_checks(
        [str(module)], lambda text, _path: dmc.violations(text), "msg"
    )
    assert status == 1
    err = capsys.readouterr().err
    assert "cannot parse this file" in err
    assert "default_language_version" in err


def test_the_detector_never_runs_over_line_fragments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every line of an unparseable file parses in isolation, so a per-line
    fallback would report each function-local re-binding as a module-level
    shadow. One refusal replaces that whole class of wrong line numbers."""
    module = tmp_path / "y.py"
    module.write_text(
        "def a(v):\n"
        "    tag, rest = v[0], v[1:]\n"
        "    return tag, rest\n"
        "def b(v):\n"
        "    tag, rest = v[0], v[1:]\n"
        "    return tag, rest\n"
        "def broken(:\n",
        encoding="utf-8",
    )
    assert (
        linecheck.run_source_checks(
            [str(module)], lambda text, _path: dmc.violations(text), "msg"
        )
        == 1
    )
    err = capsys.readouterr().err
    assert ":5: msg" not in err
    assert err.count("\n") == 1


def test_a_parseable_python_file_still_gets_its_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must not change a verdict the parser could already reach."""
    module = tmp_path / "z.py"
    module.write_text("X = 1\nX = 2\n", encoding="utf-8")
    assert (
        linecheck.run_source_checks(
            [str(module)], lambda text, _path: dmc.violations(text), "msg"
        )
        == 1
    )
    assert str(module) + ":2: msg" in capsys.readouterr().err


def test_a_non_python_path_is_not_parsed_as_python(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`is_python_source` reads the suffix, so a `.txt` holding broken Python is
    text this check has no opinion on."""
    note = tmp_path / "note.txt"
    note.write_text(UNPARSEABLE, encoding="utf-8")
    assert linecheck.unparseable_python_reason(str(note), UNPARSEABLE) is None

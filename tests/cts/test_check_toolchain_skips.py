"""Tests for hooks/check_toolchain_skips.py — the lint that flags pytest skips
gated on binary discovery (shutil.which / which( / find_executable) with no CI
env guard, so a runner missing the tool fails loud instead of silently zeroing
the guarded coverage.

Drives ``violations()`` / ``is_test_path()`` and ``main()`` for the argv/exit-
code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_toolchain_skips.py", "check_toolchain_skips")


# ── flagged: which-gated skip with no CI guard ───────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="no jq")\n',
        '@pytest.mark.skipif(which("node") is None, reason="no node")\ndef test_x():\n    pass\n',
        'pytest.mark.skipif(find_executable("docker") is None, reason="x")\n',
        # condition split over lines — the call text is scanned as a whole
        "pytest.mark.skipif(\n"
        '    shutil.which("node") is None,\n'
        '    reason="node missing",\n'
        ")\n",
    ],
)
def test_unguarded_which_skip_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── passes: CI-guarded, non-which, or non-skip code ──────────────────────
@pytest.mark.parametrize(
    "src",
    [
        # the prescribed fix: fail (not skip) in CI
        "pytest.mark.skipif(\n"
        '    shutil.which("node") is None and not os.environ.get("CI"),\n'
        '    reason="node missing locally",\n'
        ")\n",
        'pytest.mark.skipif(shutil.which("x") is None and not os.getenv("CI"), reason="r")\n',
        'pytest.mark.skipif(shutil.which("x") is None and not IN_CI, reason="r")\n'.replace(
            "IN_CI", "CI"
        ),
        # skip conditions unrelated to binary discovery are out of scope
        'pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n',
        'pytest.importorskip("yaml")\n',
        # which() outside any skip call
        'path = shutil.which("git")\n',
        # a mention inside an ordinary string/comment, not a call
        '# pytest.mark.skipif is documented here\nX = "shutil.which"\n',
    ],
)
def test_guarded_or_out_of_scope_code_passes(src: str) -> None:
    assert mod.violations(src) == []


def test_opt_out_on_call_line_and_line_above() -> None:
    same = 'pytest.mark.skipif(shutil.which("jq") is None, reason="r")  # toolchain-skip-ok: local-only helper\n'
    above = (
        "# toolchain-skip-ok: exercised in a dedicated job\n"
        'pytest.mark.skipif(shutil.which("jq") is None, reason="r")\n'
    )
    assert mod.violations(same) == []
    assert mod.violations(above) == []


def test_line_numbers_for_multiple_hits() -> None:
    src = (
        'a = pytest.mark.skipif(shutil.which("a") is None, reason="r")\n'
        "b = 1\n"
        'c = pytest.mark.skipif(shutil.which("c") is None, reason="r")\n'
    )
    assert mod.violations(src) == [1, 3]


# ── is_test_path ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "path, expected",
    [
        ("tests/test_foo.py", True),
        ("pkg/foo_test.py", True),
        ("tests/helpers.py", True),  # under a tests/ dir
        ("hooks/check_x.py", False),
        ("scripts/tool.py", False),
    ],
)
def test_is_test_path(path: str, expected: bool) -> None:
    assert mod.is_test_path(path) is expected


# ── main ─────────────────────────────────────────────────────────────────
def test_main_flags_test_file_and_skips_non_test_file(tmp_path, capsys) -> None:
    bad = 'pytest.mark.skipif(shutil.which("jq") is None, reason="r")\n'
    test_file = tmp_path / "test_a.py"
    test_file.write_text(bad)
    src_file = tmp_path / "module.py"
    src_file.write_text(bad)
    assert mod.main([str(test_file), str(src_file)]) == 1
    err = capsys.readouterr().err
    assert "test_a.py:1:" in err
    assert "module.py" not in err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "test_a.py"
    p.write_text("def test_x():\n    assert True\n")
    assert mod.main([str(p)]) == 0

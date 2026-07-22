"""Tests for hooks/check_lockstep_pins.py — the config-driven lint that
extracts one pinned value from each of two files and fails unless they agree
(the enforced version of a "keep these in lockstep" comment).

Drives ``check_pair()`` for the extraction/compare rules and ``main()`` for the
argparse contract (repeatable --pair, no-config hard error, exit codes).
"""

from tests._helpers import load_hook

mod = load_hook("check_lockstep_pins.py", "check_lockstep_pins")

REV = r"rev:\s*(\S+)"
AT = r"ci-truth-serum@(\S+)"


def _pair(t1: str, t2: str, r1: str = REV, r2: str = AT) -> list[str]:
    return mod.check_pair("a.yaml", t1, r1, "b.yaml", t2, r2)


# ── check_pair ───────────────────────────────────────────────────────────
def test_equal_captures_pass() -> None:
    assert _pair("rev: v1.2.3\n", "pip install ci-truth-serum@v1.2.3\n") == []


def test_mismatch_fails_with_both_values() -> None:
    msgs = _pair("rev: v1.2.3\n", "pip install ci-truth-serum@v1.2.4\n")
    assert len(msgs) == 1
    assert "v1.2.3" in msgs[0] and "v1.2.4" in msgs[0] and "mismatch" in msgs[0]


def test_zero_matches_is_a_hard_error_not_a_pass() -> None:
    msgs = _pair("nothing here\n", "ci-truth-serum@v1\n")
    assert len(msgs) == 1 and "matched 0 times" in msgs[0]


def test_multiple_matches_is_a_hard_error() -> None:
    msgs = _pair("rev: v1\nrev: v2\n", "ci-truth-serum@v1\n")
    assert len(msgs) == 1 and "matched 2 times" in msgs[0]


def test_wrong_group_count_is_a_hard_error() -> None:
    msgs = _pair("rev: v1\n", "ci-truth-serum@v1\n", r1=r"rev:\s*\S+")
    assert len(msgs) == 1 and "0 capture groups" in msgs[0]
    msgs = _pair("rev: v1\n", "ci-truth-serum@v1\n", r1=r"(rev):\s*(\S+)")
    assert len(msgs) == 1 and "2 capture groups" in msgs[0]


def test_uncompilable_regex_is_a_hard_error() -> None:
    msgs = _pair("rev: v1\n", "ci-truth-serum@v1\n", r1="(unclosed")
    assert len(msgs) == 1 and "does not compile" in msgs[0]


def test_errors_on_both_sides_are_both_reported() -> None:
    msgs = _pair("x\n", "y\n")
    assert len(msgs) == 2


# ── main ─────────────────────────────────────────────────────────────────
def _files(tmp_path, t1: str, t2: str) -> tuple[str, str]:
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(t1)
    b.write_text(t2)
    return str(a), str(b)


def test_main_no_pairs_is_a_config_error(capsys) -> None:
    assert mod.main([]) == 2
    assert "no --pair configured" in capsys.readouterr().err


def test_main_matching_pair_passes(tmp_path) -> None:
    a, b = _files(tmp_path, "rev: v1\n", "ci-truth-serum@v1\n")
    assert mod.main(["--pair", a, REV, b, AT]) == 0


def test_main_mismatch_fails(tmp_path, capsys) -> None:
    a, b = _files(tmp_path, "rev: v1\n", "ci-truth-serum@v2\n")
    assert mod.main(["--pair", a, REV, b, AT]) == 1
    assert "mismatch" in capsys.readouterr().out


def test_main_missing_file_fails(tmp_path, capsys) -> None:
    a, _ = _files(tmp_path, "rev: v1\n", "unused\n")
    assert mod.main(["--pair", a, REV, str(tmp_path / "gone.yaml"), AT]) == 1
    assert "does not exist" in capsys.readouterr().out


def test_main_repeatable_pairs_aggregate(tmp_path, capsys) -> None:
    a, b = _files(tmp_path, "rev: v1\n", "ci-truth-serum@v1\n")
    c = tmp_path / "c.txt"
    c.write_text("pin=9\n")
    d = tmp_path / "d.txt"
    d.write_text("pin=8\n")
    rc = mod.main(
        ["--pair", a, REV, b, AT, "--pair", str(c), r"pin=(\d+)", str(d), r"pin=(\d+)"]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "`9`" in out and "`8`" in out

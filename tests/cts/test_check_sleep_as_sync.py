"""Tests for ci_truth_serum/check_sleep_as_sync.py — the lint that flags a
fixed `sleep(...)` statement immediately before an assertion in the same
block, since a sleep long enough to hide a race still passes silently.

Drives ``violations()`` for the detection rules and ``main()`` for the argv/
exit-code contract.
"""

import subprocess
import sys

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_sleep_as_sync.py", "check_sleep_as_sync")


def _py(*lines: str) -> str:
    """A module body from LINES, so each case reads as the source it really is."""
    return "".join(f"{line}\n" for line in lines)


# ── flagged: a fixed sleep an assertion follows ──────────────────────────
def test_flags_a_sleep_an_assertion_follows() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    start()",
        "    time.sleep(0.2)",
        "    assert done()",
    )
    assert mod.violations(body) == [6]


def test_a_pytest_raises_block_counts_as_an_assertion() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    time.sleep(0.2)",
        "    with pytest.raises(RuntimeError):",
        "        read()",
    )
    assert mod.violations(body) == [5]


def test_a_sleep_before_an_assert_helper_is_flagged() -> None:
    # assert_stays is this rule's own remedy shape, so a fixed sleep in front
    # of it is the shape the check most needs to see.
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    time.sleep(0.3)",
        "    assert_stays(lambda: quiet(), grace=1)",
    )
    assert mod.violations(body) == [5]


def test_a_module_constant_interval_is_still_a_fixed_bet() -> None:
    body = _py(
        "import time",
        "",
        "_SETTLE = 0.6",
        "",
        "",
        "def test_x():",
        "    time.sleep(_SETTLE)",
        "    assert quiet()",
    )
    assert mod.violations(body) == [7]


# ── not flagged: pacing, a bounded loop, a computed interval ─────────────
def test_a_sleep_with_no_assertion_after_it_is_pacing() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    assert done()",
        "    time.sleep(0.2)",
        "    cleanup()",
    )
    assert mod.violations(body) == []


def test_a_sleep_inside_a_poll_loop_is_not_flagged() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    while not done():",
        "        time.sleep(0.05)",
        "    assert done()",
    )
    assert mod.violations(body) == []


def test_a_computed_interval_is_not_flagged() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x(interval):",
        "    time.sleep(scale_timeout(2))",
        "    time.sleep(interval)",
        "    assert quiet()",
    )
    assert mod.violations(body) == []


def test_another_call_before_an_assertion_is_not_a_sleep() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    settle(0.2)",
        "    assert done()",
    )
    assert mod.violations(body) == []


def test_a_non_numeric_module_constant_does_not_make_a_sleep_fixed() -> None:
    # Only a NUMBER reads as a fixed interval; `_MODE` names something else,
    # and a tuple target binds no single name at all.
    body = _py(
        "import time",
        "",
        '_MODE = "fast"',
        "_A, _B = 1, 2",
        "",
        "",
        "def test_x():",
        "    time.sleep(_MODE)",
        "    assert done()",
    )
    assert mod.violations(body) == []


# ── the `# allow-sleep:` opt-out ──────────────────────────────────────────
def test_the_annotation_exempts_the_sleep() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    # allow-sleep: the subject IS the one-second naming granularity",
        "    time.sleep(1.1)",
        "    assert two_incidents()",
    )
    assert mod.violations(body) == []


def test_an_annotation_without_a_reason_does_not_exempt() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    # allow-sleep:",
        "    time.sleep(1.1)",
        "    assert two_incidents()",
    )
    assert mod.violations(body) == [6]


def test_an_annotation_on_the_sleep_line_itself_exempts_it() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    time.sleep(0.2)  # allow-sleep: the timeout IS the subject",
        "    assert done()",
    )
    assert mod.violations(body) == []


def test_an_annotation_inside_a_multiline_sleep_call_exempts_it() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    time.sleep(",
        "        1.1,  # allow-sleep: the timeout IS the subject",
        "    )",
        "    assert done()",
    )
    assert mod.violations(body) == []


# ── non-vacuity: the same shape, unannotated, is still caught ────────────
def test_unannotated_call_is_still_flagged() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def test_x():",
        "    time.sleep(0.2)",
        "    assert done()",
    )
    assert mod.violations(body) != []


# ── main() — the enforcement contract ────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "test_x.py"
    p.write_text(
        _py(
            "import time",
            "",
            "",
            "def test_x():",
            "    time.sleep(0.2)",
            "    assert done()",
        )
    )
    assert mod.main([str(p)]) == 1
    assert f"{p}:5:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "test_x.py"
    p.write_text(
        _py(
            "import time",
            "",
            "",
            "def test_x():",
            "    assert done()",
            "    time.sleep(0.2)",
        )
    )
    assert mod.main([str(p)]) == 0


def test_main_with_no_files_exits_two_via_run_file_cli() -> None:
    # The empty-argv refusal is `run_file_cli`'s job, wired at `__main__` — so
    # it is only observable by running the module, not by calling `mod.main`
    # directly (that skips the wrapper and would report a vacuous pass, 0).
    done = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_sleep_as_sync"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 2


def test_the_message_names_both_helpers_and_the_opt_out(tmp_path, capsys) -> None:
    # The remedy is the least-executed text in the repo and reaches a reader
    # with none of this context, so it must name the two replacements by name.
    offender = tmp_path / "test_offender.py"
    offender.write_text(
        _py(
            "import time",
            "",
            "",
            "def test_x():",
            "    time.sleep(0.2)",
            "    assert done()",
        ),
        encoding="utf-8",
    )
    mod.main([str(offender)])
    err = capsys.readouterr().err
    assert f"{offender}:5" in err
    assert "wait_until" in err and "assert_stays" in err and "allow-sleep" in err

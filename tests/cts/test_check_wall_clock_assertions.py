"""Tests for ci_truth_serum/check_wall_clock_assertions.py — the check that bans a
test assertion comparing a wall-clock duration against a numeric literal, in
Python (stdlib ``ast``) and JavaScript/TypeScript (``_js_ast``, tree-sitter).
"""

import subprocess
import sys

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_wall_clock_assertions.py", "check_wall_clock_assertions")


def _py(*lines: str) -> str:
    return "".join(f"{line}\n" for line in lines)


def _py_violations(text: str, scalers: frozenset[str] = frozenset()) -> list[int]:
    return mod.violations(text, "tests/test_x.py", scalers)


_CLEAN_POLL = _py(
    "import time",
    "",
    "",
    "def test_polls_to_a_deadline():",
    "    deadline = time.monotonic() + 10",
    "    while time.monotonic() < deadline:",
    "        if done():",
    "            return",
    '    raise AssertionError("never finished")',
)


# ── Python: flagged shapes ──────────────────────────────────────────────────
def test_flags_an_inline_upper_bound() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    assert time.monotonic() - s < 2",
    )
    assert _py_violations(body) == [6]


def test_flags_a_lower_bound_too() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    e = time.monotonic() - s",
        "    assert e >= 1.0",
    )
    assert _py_violations(body) == [7]


def test_flags_a_duration_through_a_numeric_conversion() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    e = time.monotonic() - s",
        "    assert round(e, 2) < 3",
    )
    assert _py_violations(body) == [7]


def test_a_duration_survives_being_scaled() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    elapsed = time.monotonic() - s",
        "    assert elapsed * 1000 < 250",
    )
    assert _py_violations(body) == [7]


def test_a_name_bound_to_a_duration_is_itself_a_duration() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    elapsed = time.monotonic() - s",
        "    total = elapsed",
        "    assert total < 5",
    )
    assert _py_violations(body) == [8]


def test_a_negated_literal_is_still_a_number() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    drift = time.monotonic() - s",
        "    assert drift > -1",
    )
    assert _py_violations(body) == [7]


def test_a_duration_returned_from_a_helper_is_followed() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def _probe():",
        "    start = time.monotonic()",
        "    r = run()",
        "    return r.returncode, time.monotonic() - start",
        "",
        "",
        "def t():",
        "    rc, elapsed = _probe()",
        "    assert rc == 1",
        "    assert elapsed >= 1.0",
    )
    assert _py_violations(body) == [13]


def test_a_helper_call_asserted_in_place_is_a_duration() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def _seconds():",
        "    started = time.monotonic()",
        "    run()",
        "    return time.monotonic() - started",
        "",
        "",
        "def t():",
        "    assert _seconds() < 10",
    )
    assert _py_violations(body) == [11]


# ── Python: not flagged ──────────────────────────────────────────────────────
def test_a_deadline_poll_is_not_flagged() -> None:
    assert _py_violations(_CLEAN_POLL) == []


def test_a_comparison_against_a_non_literal_is_not_flagged() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t(budget):",
        "    s = time.monotonic()",
        "    e = time.monotonic() - s",
        "    assert e < budget",
    )
    assert _py_violations(body) == []


def test_a_name_reused_in_another_function_is_not_a_duration() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t_one():",
        "    s = time.monotonic()",
        "    r = time.monotonic() - s",
        "    assert r < 9",
        "",
        "",
        "def t_two():",
        "    r = [1, 2, 3]",
        "    assert len(r) == 3",
    )
    assert _py_violations(body) == [7]


def test_a_helper_returning_no_duration_binds_nothing() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def _probe():",
        "    start = time.monotonic()",
        "    r = run()",
        "    return r.returncode, time.monotonic() - start",
        "",
        "",
        "def t():",
        "    rc, elapsed = _probe()",
        "    assert rc < 2",
    )
    assert _py_violations(body) == []


def test_a_nested_defs_return_does_not_escape_to_its_owner() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def _fixture():",
        "    def _fake():",
        "        start = time.monotonic()",
        "        return time.monotonic() - start",
        "",
        "    return _fake",
        "",
        "",
        "def t():",
        "    n = _fixture()",
        "    assert n < 3",
    )
    assert _py_violations(body) == []


# ── Python: the --scaler flag (repo-specific, off by default) ──────────────
def test_an_unnamed_scaler_call_is_not_a_literal() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    elapsed = time.monotonic() - s",
        "    assert elapsed < scale_timeout(20)",
    )
    assert _py_violations(body) == []


def test_a_named_scaler_over_a_literal_is_a_bound() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    elapsed = time.monotonic() - s",
        "    assert elapsed < scale_timeout(20)",
    )
    assert _py_violations(body, frozenset({"scale_timeout"})) == [7]


def test_a_named_scaler_over_a_non_literal_stays_legal() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t(budget):",
        "    s = time.monotonic()",
        "    elapsed = time.monotonic() - s",
        "    assert elapsed < scale_timeout(budget)",
    )
    assert _py_violations(body, frozenset({"scale_timeout"})) == []


# ── Python: opt-out ──────────────────────────────────────────────────────────
def test_the_annotation_exempts_the_assert() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    # allow-wall-clock: the subject IS the clock",
        "    assert time.monotonic() - s < 2",
    )
    assert _py_violations(body) == []


def test_an_annotation_without_a_reason_does_not_exempt() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    s = time.monotonic()",
        "    # allow-wall-clock:",
        "    assert time.monotonic() - s < 2",
    )
    assert _py_violations(body) == [7]


# ── JavaScript / TypeScript ──────────────────────────────────────────────────
def _js_violations(text: str, rel: str = "tests/x.test.mjs") -> list[int]:
    return mod.violations(text, rel)


def test_js_flags_a_bound_inside_an_assert() -> None:
    body = _py(
        "const started = Date.now();",
        "await run();",
        'assert.ok(Date.now() - started < 1000, "too slow");',
    )
    assert _js_violations(body) == [3]


def test_js_flags_performance_now_too() -> None:
    body = _py(
        "const started = performance.now();",
        'assert.ok(performance.now() - started >= 1000, "too fast");',
    )
    assert _js_violations(body) == [2]


def test_js_ignores_a_poll_condition() -> None:
    body = _py(
        "const start = Date.now();",
        "while (true) {",
        "  if (Date.now() - start > 5000) throw new Error('timeout');",
        "}",
    )
    assert _js_violations(body) == []


def test_js_a_name_bound_to_a_duration_is_itself_a_duration() -> None:
    body = _py(
        "const started = Date.now();",
        "const elapsed = Date.now() - started;",
        "assert.ok(elapsed < 1000);",
    )
    assert _js_violations(body) == [3]


def test_js_a_duration_name_does_not_leak_across_functions() -> None:
    """The measured false positive: one function's `elapsed` (a real duration)
    must not make an unrelated function's same-named bare local a duration
    too — a global name set would make BOTH asserts below fire."""
    body = _py(
        "function timedStep() {",
        "  const started = Date.now();",
        "  const elapsed = Date.now() - started;",
        "  assert.ok(elapsed < 1000);",
        "}",
        "function countItems() {",
        "  const elapsed = items.filter((x) => x.done).length;",
        "  assert.ok(elapsed > 2);",
        "}",
    )
    assert _js_violations(body) == [4]


def test_js_a_relative_looking_clock_mention_in_a_string_is_not_a_reading() -> None:
    # The whole reason this half reads the grammar instead of a regex.
    body = _py(
        'const msg = "Date.now() - started < 1000";',
        "assert.ok(true, msg);",
    )
    assert _js_violations(body) == []


def test_js_a_comparison_against_a_non_literal_is_not_flagged() -> None:
    body = _py(
        "const started = Date.now();",
        "const elapsed = Date.now() - started;",
        "assert.ok(elapsed < budget);",
    )
    assert _js_violations(body) == []


def test_js_skips_a_declaration_with_no_initialiser() -> None:
    body = _py(
        "let pending;",
        "const plain = 1;",
        "const started = Date.now();",
        'assert.ok(Date.now() - started < 1000, "too slow");',
    )
    assert _js_violations(body) == [4]


def test_js_annotation_exempts() -> None:
    body = _py(
        "const started = Date.now();",
        "// allow-wall-clock: the subject IS the timeout",
        'assert.ok(Date.now() - started < 1000, "too slow");',
    )
    assert _js_violations(body) == []


def test_js_annotation_without_a_reason_does_not_exempt() -> None:
    body = _py(
        "const started = Date.now();",
        "// allow-wall-clock:",
        'assert.ok(Date.now() - started < 1000, "too slow");',
    )
    assert _js_violations(body) == [3]


def test_a_non_language_path_has_no_violations() -> None:
    assert mod.violations("assert Date.now() < 1", "tests/notes.txt") == []


# ── file-class scoping (main) ───────────────────────────────────────────────
def test_main_only_scans_test_paths(tmp_path) -> None:
    non_test = tmp_path / "lib.py"
    non_test.write_text(
        _py(
            "import time",
            "",
            "",
            "def t():",
            "    s = time.monotonic()",
            "    assert time.monotonic() - s < 2",
        ),
        encoding="utf-8",
    )
    assert mod.main([str(non_test)]) == 0


def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    test_file = tmp_path / "test_x.py"
    test_file.write_text(
        _py(
            "import time",
            "",
            "",
            "def t():",
            "    s = time.monotonic()",
            "    assert time.monotonic() - s < 2",
        ),
        encoding="utf-8",
    )
    assert mod.main([str(test_file)]) == 1
    assert f"{test_file}:6:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    test_file = tmp_path / "test_x.py"
    test_file.write_text(_CLEAN_POLL, encoding="utf-8")
    assert mod.main([str(test_file)]) == 0


def test_main_wires_the_scaler_flag(tmp_path, capsys) -> None:
    test_file = tmp_path / "test_x.py"
    test_file.write_text(
        _py(
            "import time",
            "",
            "",
            "def t():",
            "    s = time.monotonic()",
            "    elapsed = time.monotonic() - s",
            "    assert elapsed < scale_timeout(20)",
        ),
        encoding="utf-8",
    )
    assert mod.main([str(test_file)]) == 0
    assert mod.main(["--scaler", "scale_timeout", str(test_file)]) == 1
    assert f"{test_file}:7:" in capsys.readouterr().err


def test_empty_argv_exits_2_via_run_file_cli() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_wall_clock_assertions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr

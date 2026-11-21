"""Tests for ci_truth_serum/check_wall_clock_assertions.py — the check that bans a
test assertion comparing a wall-clock duration against a numeric literal or a
second measured duration, in
Python (stdlib ``ast``) and JavaScript/TypeScript (``_cts_js_ast``, tree-sitter).
"""

import subprocess
import sys

import pytest

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


# ── Python: two durations compared with each other ──────────────────────────
_TWO_SERIES = (
    "import statistics",
    "import time",
    "",
    "",
    "def t():",
    "    fast, slow = [], []",
    "    for _ in range(5):",
    "        t0 = time.perf_counter()",
    "        run(cached=True)",
    "        fast.append(time.perf_counter() - t0)",
    "        t0 = time.perf_counter()",
    "        run(cached=False)",
    "        slow.append(time.perf_counter() - t0)",
)

_TWO_HELPERS = (
    "import time",
    "",
    "",
    "def _seconds(job):",
    "    started = time.monotonic()",
    "    job()",
    "    return time.monotonic() - started",
    "",
    "",
    "def t():",
)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            _py(*_TWO_SERIES, "    assert min(fast) < 1.6 * min(slow)"),
            id="min-of-two-appended-lists",
        ),
        pytest.param(
            _py(
                *_TWO_SERIES,
                "    assert statistics.median(fast) < statistics.median(slow)",
            ),
            id="statistics-median",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert sorted(fast)[0] <= sorted(slow)[0]"),
            id="subscript-of-sorted",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert sum(fast[1:]) < sum(slow[1:])"),
            id="sum-of-a-slice",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    best = min(fast)", "    assert best < min(slow)"),
            id="reduction-bound-to-a-name",
        ),
        pytest.param(
            _py(*_TWO_HELPERS, "    assert _seconds(warm) < _seconds(cold) / 2"),
            id="two-helper-durations",
        ),
        pytest.param(
            _py(
                *_TWO_HELPERS,
                "    runs = [_seconds(warm) for _ in range(3)]",
                "    assert max(runs) < _seconds(cold)",
            ),
            id="list-comprehension-of-durations",
        ),
        pytest.param(
            _py(
                *_TWO_HELPERS,
                "    assert min(_seconds(warm) for _ in range(3)) < _seconds(cold)",
            ),
            id="generator-of-durations",
        ),
        pytest.param(
            _py(
                *_TWO_HELPERS,
                "    assert min([_seconds(warm), _seconds(warm)]) < _seconds(cold)",
            ),
            id="list-literal-of-durations",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert tuple(fast)[0] < list(slow)[0]"),
            id="subscript-of-tuple-and-list",
        ),
        pytest.param(
            _py(*_TWO_HELPERS, "    assert max(_seconds(warm), 0.0) < _seconds(cold)"),
            id="multi-arg-reduction",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert sum(fast, 0.0) < sum(slow, 0.0)"),
            id="sum-with-a-start-value",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert max(fast) < 0.5"),
            id="an-appended-list-against-a-literal",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                "    taken: dict[int, list[float]] = {0: [], 1: []}",
                "    for _ in range(5):",
                "        for which in (0, 1):",
                "            started = time.perf_counter()",
                "            run(which)",
                "            taken[which].append(time.perf_counter() - started)",
                "    assert min(taken[1]) < 1.6 * min(taken[0])",
            ),
            id="lists-held-in-a-dict",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                '    m = {"warm": [], "cold": []}',
                "    started = time.perf_counter()",
                '    m["warm"].append(time.perf_counter() - started)',
                '    m["cold"].append(time.perf_counter() - started)',
                '    assert min(m["warm"]) < min(m["cold"])',
            ),
            id="lists-under-constant-keys",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                "    warm = cold = 0.0",
                "    for _ in range(3):",
                "        t0 = time.monotonic()",
                "        run(cached=True)",
                "        warm += time.monotonic() - t0",
                "        t0 = time.monotonic()",
                "        run(cached=False)",
                "        cold += time.monotonic() - t0",
                "    assert warm < cold",
            ),
            id="two-totals-that-plus-equals-built",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t(floor):",
                "    s = time.monotonic()",
                "    a = time.monotonic() - s",
                "    s2 = time.monotonic()",
                "    b = time.monotonic() - s2",
                "    assert floor < a < b",
            ),
            id="two-neighbours-inside-a-chain",
        ),
    ],
)
def test_flags_two_durations_compared(body: str) -> None:
    assert _py_violations(body) == [len(body.splitlines())]


def test_minus_equals_on_a_reading_makes_a_duration() -> None:
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        "    start = time.monotonic()",
        "    run()",
        "    elapsed = time.monotonic()",
        "    elapsed -= start",
        "    assert elapsed < 5",
    )
    assert _py_violations(body) == [9]


@pytest.mark.parametrize(
    "value",
    [
        "time.monotonic() - s if s else 0.0",
        "math.ceil(time.monotonic() - s)",
        "to_ms(time.monotonic() - s)",
        "(time.monotonic() - s) or 0.0",
        "ready and time.monotonic() - s",
        "-(s - time.monotonic())",
    ],
)
def test_a_wrapped_duration_is_still_a_duration(value: str) -> None:
    body = _py(
        "import math",
        "import time",
        "",
        "",
        "def t(s):",
        f"    elapsed = {value}",
        "    assert elapsed < 2",
    )
    assert _py_violations(body) == [7]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                "    first_seen = time.monotonic()",
                "    fire()",
                "    second_seen = time.monotonic()",
                "    assert first_seen < second_seen",
            ),
            id="two-readings-order-two-events",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                "    start = time.monotonic()",
                "    assert start > 0",
            ),
            id="a-reading-against-a-literal",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert len(fast) == len(slow)"),
            id="the-lengths-of-two-series",
        ),
        pytest.param(
            _py(
                "def t():",
                "    codes, sizes = [], []",
                "    codes.append(run().returncode)",
                "    sizes.append(len(out()))",
                "    assert max(codes) < max(sizes)",
            ),
            id="lists-that-append-no-duration",
        ),
        pytest.param(
            _py(*_TWO_SERIES, "    assert min(fast) < budget"),
            id="a-series-against-a-budget",
        ),
        pytest.param(
            _py(
                "def t():",
                "    sizes: dict[int, list[int]] = {0: [], 1: []}",
                "    sizes[0].append(len(out()))",
                "    sizes[1].append(len(err()))",
                "    assert min(sizes[1]) < 1.6 * min(sizes[0])",
            ),
            id="lists-in-a-dict-that-append-no-duration",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t():",
                '    m = {"elapsed": [], "sizes": []}',
                "    started = time.perf_counter()",
                '    m["elapsed"].append(time.perf_counter() - started)',
                '    m["sizes"].append(len(out()))',
                '    assert min(m["sizes"]) == 10',
            ),
            id="a-sibling-key-that-holds-no-duration",
        ),
        pytest.param(
            _py(
                "import time",
                "",
                "",
                "def t(budget):",
                "    s = time.monotonic()",
                "    a = time.monotonic() - s",
                "    b = time.monotonic() - s",
                "    assert a < budget > b",
            ),
            id="two-durations-that-a-chain-never-compares",
        ),
    ],
)
def test_two_non_durations_are_not_flagged(body: str) -> None:
    assert _py_violations(body) == []


def test_a_series_name_does_not_leak_across_functions() -> None:
    body = _py(
        *_TWO_SERIES,
        "    assert min(fast) < 1.6 * min(slow)",
        "",
        "",
        "def t_other():",
        "    fast, slow = [3], [4]",
        "    assert min(fast) < min(slow)",
    )
    assert _py_violations(body) == [14]


def test_the_annotation_exempts_two_durations() -> None:
    body = _py(
        *_TWO_SERIES,
        "    # allow-wall-clock: the warm path skips the whole download",
        "    assert min(fast) < 1.6 * min(slow)",
    )
    assert _py_violations(body) == []


def test_a_long_expression_with_no_clock_does_not_recurse() -> None:
    # 1100 operands nest deeper than the default recursion limit of 1000.
    concat = " + ".join(f"'l{i}'" for i in range(1100))
    body = _py(
        "import time",
        "",
        "",
        "def t():",
        f"    expected = {concat}",
        "    s = time.monotonic()",
        "    assert time.monotonic() - s < 2",
    )
    assert _py_violations(body) == [7]


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

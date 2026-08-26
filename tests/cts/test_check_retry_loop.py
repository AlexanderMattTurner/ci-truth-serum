"""Tests for ci_truth_serum/check_retry_loop.py — the lint that refuses a new
hand-rolled attempt-and-sleep loop.

Drives ``violations()`` over real bash source, asserting the line numbers it
reports. The clean cases carry most of the weight: this check is a parser,
not a grep, because a text scan for "a sleep in a counted loop" also matches
live loops that are correctly not retries.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_retry_loop.py", "check_retry_loop")

_RETRY = "for ((i = 0; i < 5; i++)); do\n  fetch && return 0\n  sleep 1\ndone\n"


@pytest.mark.parametrize(
    "text",
    [
        # the c-style counted loop, the commonest spelling
        _RETRY,
        # the budget written out as a literal list, with no variable to step
        "for attempt in 1 2 3; do\n  fetch && return 0\n  sleep 2\ndone\n",
        # a `while` testing a counter its body steps with the `n=$((n + 1))` spelling
        (
            'while [[ "$attempt" -le "$max" ]]; do\n'
            "  fetch && return 0\n"
            "  sleep 1\n"
            "  attempt=$((attempt + 1))\n"
            "done\n"
        ),
        # a decrementing counter, stepped inside the condition itself
        "while ((tries-- > 0)); do\n  fetch && break\n  sleep 1\ndone\n",
        # `while true` with the budget enforced INSIDE the body
        (
            "while true; do\n"
            "  fetch && return 0\n"
            "  ((attempt >= attempts)) && return 1\n"
            "  attempt=$((attempt + 1))\n"
            "  sleep 1\n"
            "done\n"
        ),
        # `+=` steps too, and the sleep may precede the attempt
        "until ((n >= 4)); do\n  sleep 1\n  fetch && return 0\n  ((n += 1))\ndone\n",
        # `((i = i + 1))` steps too
        "while ((i < max)); do\n  fetch && return 0\n  sleep 1\n  ((i = i + 1))\ndone\n",
        # A real retry that TIMES itself still mentions no clock it TESTS
        (
            "for ((i = 0; i < 5; i++)); do\n"
            "  fetch && return 0\n"
            "  ((waited = SECONDS - start))\n"
            "  sleep 1\n"
            "done\n"
        ),
        # so does `-=`, counting a budget down
        "while ((left > 0)); do\n  fetch && return 0\n  ((left -= 1))\n  sleep 1\ndone\n",
        # the sleep behind a transparent prefix is still this loop's sleep
        (
            "for ((i = 0; i < 3; i++)); do\n"
            "  fetch && return 0\n"
            "  command sleep 1\n"
            "done\n"
        ),
    ],
)
def test_fires_on_hand_rolled_retry(text: str) -> None:
    assert mod.violations(text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # A DEADLINE loop waits however many attempts a slow machine needs
        (
            "deadline=$((SECONDS + 30))\n"
            "while ! probe; do\n"
            "  ((SECONDS >= deadline)) && return 1\n"
            "  sleep 0.5\n"
            "done\n"
        ),
        # the same wait, counted against a deadline in the header
        "while ((SECONDS < deadline)); do\n  probe && return 0\n  sleep 1\ndone\n",
        # A loop carrying BOTH an attempt counter and a deadline is still a
        # deadline loop, and these pin the other clocks bash offers.
        (
            "while ((n < 5)); do\n"
            "  probe && return 0\n"
            "  ((EPOCHSECONDS >= end)) && return 1\n"
            "  ((n += 1))\n"
            "  sleep 1\n"
            "done\n"
        ),
        (
            "while ((n < 5)); do\n"
            "  probe && return 0\n"
            '  [[ "$EPOCHREALTIME" > "$end" ]] && return 1\n'
            "  ((n += 1))\n"
            "  sleep 1\n"
            "done\n"
        ),
        # A loop that repeats until the WORLD changes has no attempt budget
        'while [[ ! -e "$dir/stop" ]]; do\n  do_a_pass\n  sleep 2\ndone\n',
        'while kill -0 "$pid" 2>/dev/null; do\n  paint_frame\n  sleep 0.05\ndone\n',
        "until port_ready; do\n  liveness || return 1\n  sleep 0.2\ndone\n",
        # A stepped variable nobody COMPARES bounds nothing
        ('while kill -0 "$pid"; do\n  frame=$(((frame + 1) % 4))\n  sleep 0.1\ndone\n'),
        # An accumulator is not a counter: `total` reads two OTHER names.
        "for f in a b; do\n  total=$((seen + extra))\n  sleep 1\ndone\n",
        # A `for` over names, not numbers, has no budget either.
        "for host in a b c; do\n  probe && return 0\n  sleep 1\ndone\n",
        # The sleep belongs to the INNER loop, which is the one with no budget.
        (
            "for ((i = 0; i < 3; i++)); do\n"
            "  attempt_once\n"
            '  while [[ ! -e "$stop" ]]; do sleep 1; done\n'
            "done\n"
        ),
        # A sleep inside a function DEFINED in the body runs when called
        (
            "for ((i = 0; i < 3; i++)); do\n"
            "  pause() { sleep 1; }\n"
            "  attempt_once\n"
            "done\n"
        ),
        # A word in a comment, and one in an inert heredoc body, are not commands
        "for ((i = 0; i < 3; i++)); do\n  attempt_once  # sleep 1 between tries\ndone\n",
        (
            "for ((i = 0; i < 3; i++)); do\n"
            "  cat <<'EOF' >/tmp/help\n"
            "sleep 1 between attempts\n"
            "EOF\n"
            "done\n"
        ),
        # a counted loop that does not sleep at all is not this defect
        "for ((i = 0; i < 5; i++)); do\n  fetch && return 0\ndone\n",
        # same-line annotation
        (
            "for ((i = 0; i < 5; i++)); do  # retry-loop-ok: reclaims a stale lock\n"
            "  fetch && return 0\n"
            "  sleep 1\n"
            "done\n"
        ),
    ],
)
def test_clean_loops_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_a_clock_the_loop_only_prints_does_not_exempt_it() -> None:
    # Reading `$SECONDS` into a progress line decides nothing about when the
    # loop ends, so it must not buy the deadline-loop exemption.
    text = (
        "for ((i = 0; i < 5; i++)); do\n"
        "  fetch && return 0\n"
        '  gb_info "still trying after ${SECONDS}s"\n'
        "  sleep 1\n"
        "done\n"
    )
    assert mod.violations(text) == [1]


def test_annotation_may_sit_anywhere_in_the_comment_block_above() -> None:
    ok = "# retry-loop-ok: it heals the sign-in\n# between attempts.\n" + _RETRY
    assert mod.violations(ok) == []
    stale = "# retry-loop-ok: about a different loop\nlocal max=5\n" + _RETRY
    assert mod.violations(stale) == [3]
    trailing = "# retry-loop-ok: about a different loop\nlocal max=5  # tune\n" + _RETRY
    assert mod.violations(trailing) == [3]


def test_annotation_inside_the_body_does_not_exempt() -> None:
    text = (
        "for ((i = 0; i < 5; i++)); do\n"
        "  fetch && return 0\n"
        "  # retry-loop-ok: not where an opt-out counts\n"
        "  sleep 1\n"
        "done\n"
    )
    assert mod.violations(text) == [1]


def test_bare_annotation_without_a_reason_does_not_exempt() -> None:
    assert mod.violations("# retry-loop-ok\n" + _RETRY) == [2]


def test_nested_retry_loops_each_report() -> None:
    text = (
        "for ((i = 0; i < 3; i++)); do\n"
        "  fetch && return 0\n"
        "  for ((j = 0; j < 2; j++)); do\n"
        "    inner && break\n"
        "    sleep 1\n"
        "  done\n"
        "  sleep 5\n"
        "done\n"
    )
    assert mod.violations(text) == [1, 3]


def test_two_downloads_report_once_per_loop() -> None:
    assert mod.violations(_RETRY) == [1]


# ── the --wrapper delegation flag ────────────────────────────────────────────
def test_an_unconfigured_wrapper_call_does_not_satisfy_the_rule() -> None:
    text = (
        "for ((i = 0; i < 5; i++)); do\n  gb_retry fetch && return 0\n  sleep 1\ndone\n"
    )
    assert mod.violations(text) == [1]


def test_a_configured_wrapper_call_in_the_body_satisfies_the_rule() -> None:
    text = (
        "for ((i = 0; i < 5; i++)); do\n  gb_retry fetch && return 0\n  sleep 1\ndone\n"
    )
    assert mod.violations(text, frozenset({"gb_retry"})) == []


def test_non_vacuous_default_flag_config() -> None:
    """A default run (no --wrapper) still flags a real hand-rolled retry —
    guards against the delegation flag silently swallowing every case."""
    assert mod.violations(_RETRY, frozenset()) == [1]


# ── the two structural probes shell-lint-parsing.md requires ────────────────
def test_probe_message_string_does_not_fire() -> None:
    text = 'gb_warn "retry with: for ((i=0;i<5;i++)); do fetch; sleep 1; done"\n'
    assert mod.violations(text) == []


def test_probe_heredoc_body_does_not_fire() -> None:
    text = "cat <<'EOF' >/tmp/x\n" + _RETRY + "EOF\n"
    assert mod.violations(text) == []


# ── the refusal names both ways out ─────────────────────────────────────────
def test_the_refusal_names_both_ways_out(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bash"
    bad.write_text(_RETRY, encoding="utf-8")
    result = mod.main(["--retry-helper", "bin/lib/retry.bash", str(bad)])
    assert result == 1


def test_main_names_the_configured_retry_helper(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.bash"
    bad.write_text(_RETRY, encoding="utf-8")
    mod.main(["--retry-helper", "bin/lib/retry.bash", str(bad)])
    err = capsys.readouterr().err
    assert "bin/lib/retry.bash" in err
    assert mod.OPT_OUT in err


def test_main_falls_back_to_generic_wording_with_no_helper_configured(
    tmp_path, capsys
) -> None:
    bad = tmp_path / "bad.bash"
    bad.write_text(_RETRY, encoding="utf-8")
    mod.main([str(bad)])
    assert "your retry helper" in capsys.readouterr().err


# ── main() argv/exit-code contract ───────────────────────────────────────────
def test_main_with_no_files_exits_2(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_with_only_flags_and_no_files_exits_2() -> None:
    assert mod.main(["--retry-helper", "bin/lib/retry.bash"]) == 2


def test_main_reports_a_hit_and_exits_1(tmp_path, capsys) -> None:
    path = tmp_path / "s.bash"
    path.write_text(_RETRY, encoding="utf-8")
    assert mod.main([str(path)]) == 1
    assert f"{path}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_0(tmp_path) -> None:
    path = tmp_path / "s.bash"
    path.write_text(
        "for ((i = 0; i < 5; i++)); do\n  fetch && return 0\ndone\n", encoding="utf-8"
    )
    assert mod.main([str(path)]) == 0

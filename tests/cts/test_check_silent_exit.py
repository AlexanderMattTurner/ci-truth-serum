"""Tests for ci_truth_serum/check_silent_exit.py — the lint that flags a literal
`exit N` a failed file or string probe reaches with nothing printed first.

Drives ``violations()`` for the rules and ``main()`` for the argv and exit-code
contract. The cases pin the structural verdicts: the idiom inside a message string
or a heredoc body is text, an exit inside a subshell ends only that subshell, and a
comparison of a counter reads a verdict the script reached earlier.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_silent_exit.py", "check_silent_exit")


@pytest.mark.parametrize(
    "src",
    [
        '[[ -f "$cfg" ]] || exit 2\n',
        '[ -n "$a" ] || exit 1\n',
        'test -d "$dir" || exit 4\n',
        '[ ! -L "$3" ] || exit 1\n',
        '[[ -f "$a" && -r "$a" ]] || exit 3\n',
        '[[ -f "$a" ]] && [[ -r "$a" ]] || exit 9\n',
        '[[ -s "$f" ]] || { rc=1; exit 2; }\n',
        'if ! [[ -e "$z" ]]; then\n  local q=1\n  exit 5\nfi\n',
        'if [[ -d "$a" ]]; then\n  :\nelse\n  exit 7\nfi\n',
        'f() {\n  [[ -n "$1" ]] || exit 2\n}\n',
        'if [[ -f "$a" ]]; then\n  :\nelif [[ -n "$b" ]]; then\n  exit 2\nfi\n',
        'case "$1" in\n  x) [[ -f "$a" ]] || exit 6 ;;\nesac\n',
    ],
)
def test_a_probe_that_reaches_a_silent_exit_is_flagged(src: str) -> None:
    exit_line = next(n for n, line in enumerate(src.splitlines(), 1) if "exit" in line)
    assert mod.violations(src) == [exit_line]


@pytest.mark.parametrize(
    "src",
    [
        # The branch prints the reason, or calls a command that may print it.
        '[[ -f "$cfg" ]] || { echo "no $cfg" >&2; exit 2; }\n',
        'if [[ -f "$a" ]]; then\n  gb_error "found $a"\n  exit 1\nfi\n',
        # A status from elsewhere, or success.
        '[[ -f "$a" ]] || exit\n',
        '[[ -f "$a" ]] || exit "$rc"\n',
        '[[ -f "$a" ]] || exit 0\n',
        # A command, not a probe, decides: it may have printed its own error.
        'grep -q x "$f" || exit 1\n',
        'if grep -q x "$f"; then\n  :\nelse\n  exit 1\nfi\n',
        'if grep -q x "$f"; then\n  :\nelif [[ -n "$b" ]]; then\n  exit 2\nfi\n',
        # A comparison reads a verdict the script reached, and reported, earlier.
        '[[ "$failed" -eq 0 ]] || exit 1\n',
        '[[ ! ( "$n" -gt 0 ) ]] || exit 1\n',
        # A `$( … )` runs a command that may print the cause.
        '[[ -n "$(grep x f)" ]] || exit 1\n',
        '[[ -f "$a" ]] || { m=$(probe); exit 1; }\n',
        # bash keeps the low 8 bits, so this exits 0.
        '[[ -f "$a" ]] || exit 256\n',
        # An exit in a subshell ends only the subshell; the caller sees the status.
        'f() (\n  [[ -f "$x" ]] || exit 1\n)\n',
        'v=$([[ -f "$x" ]] || exit 1)\n',
        'ls | while read -r l; do [[ -f "$l" ]] || exit 1; done\n',
        "{ [[ -f a ]] || exit 2; } | tee log\n",
        '[[ -f "$cfg" ]] || exit 2 &\nwait\n',
        '[[ ! "$failed" -eq 0 ]] || exit 1\n',
        '[[ -f "$cfg" ]] || { reason=$(echo missing >&2); exit 2; }\n',
        'diff <([[ -f "$x" ]] || exit 1) f\n',
        # A return hands the status to a caller who can print the cause.
        'f() { [[ -n "$1" ]] || return 2; }\n',
        # Text, not code.
        'gb_warn "[[ -f x ]] || exit 1"\n',
        "cat <<'EOF' >doc.txt\n[[ -f x ]] || exit 1\nEOF\n",
    ],
)
def test_a_reported_or_propagated_exit_passes(src: str) -> None:
    assert mod.violations(src) == []


@pytest.mark.parametrize(
    "src",
    [
        '# allow-silent-exit: a predicate script, its status is the answer\n[[ -f "$a" ]] || exit 1\n',
        '[[ -f "$a" ]] || exit 1  # allow-silent-exit: the status is the answer\n',
        'if ! [[ -f "$a" ]]; then  # allow-silent-exit: the status is the answer\n  exit 1\nfi\n',
    ],
)
def test_an_opt_out_with_a_reason_clears_the_finding(src: str) -> None:
    assert mod.violations(src) == []


def test_an_opt_out_with_no_reason_does_not_clear_it() -> None:
    assert mod.violations('[[ -f "$a" ]] || exit 1  # allow-silent-exit:\n') == [1]


def test_main_reports_each_finding_and_fails(tmp_path, capsys) -> None:
    script = tmp_path / "s.sh"
    script.write_text(
        '#!/usr/bin/env bash\n[[ -f "$cfg" ]] || exit 2\n', encoding="utf-8"
    )
    assert mod.main([str(script)]) == 1
    assert f"{script}:2:" in capsys.readouterr().err


def test_main_passes_a_clean_file(tmp_path) -> None:
    script = tmp_path / "s.sh"
    script.write_text(
        '#!/usr/bin/env bash\n[[ -f "$cfg" ]] || { echo "no $cfg" >&2; exit 2; }\n',
        encoding="utf-8",
    )
    assert mod.main([str(script)]) == 0

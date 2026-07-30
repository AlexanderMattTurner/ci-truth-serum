"""Tests for ci_truth_serum/check_stderr_merge_parse.py — the lint that flags `2>&1`-
merged output being PARSED (piped into head/tail/grep/… inside a substitution,
or a merged capture later piped/compared), while leaving diagnostic captures
(`out=$(cmd 2>&1)` + echo) alone.

Drives ``violations()`` for both rules and ``main()`` for the shell-vs-workflow
argv routing and the exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_stderr_merge_parse.py", "check_stderr_merge_parse")


def _lines(src: str) -> list[int]:
    return [line for line, _ in mod.violations(src)]


# ── rule (a): merged and parsed inside one substitution ──────────────────
@pytest.mark.parametrize(
    "line",
    [
        "v=$(npm view pkg version 2>&1 | tail -1)",
        "v=$(cmd 2>&1 | head -n1)",
        "n=$(make build 2>&1 | grep -c error)",
        'x=$(tool 2>&1 | awk "{print $1}")',
        "y=$(tool 2>&1 | sed -e s/x/y/)",
        "z=$(tool 2>&1 | jq -r .version)",
        "w=$(tool 2>&1 | sort | head -1)",
        "c=$(tool 2>&1 | wc -l)",
        "f=$(tool 2>&1 | cut -d: -f2)",
        'echo "$(tool 2>&1 | grep ok)"',
    ],
)
def test_merge_piped_to_parser_inside_substitution_is_flagged(line: str) -> None:
    assert _lines(line + "\n") == [1]


def test_multiline_substitution_is_joined_and_flagged() -> None:
    src = 'status=$(curl -s -o /dev/null \\\n  -w "%{http_code}" 2>&1 |\n  tail -1)\n'
    assert _lines(src) == [1]


# ── rule (b): merged capture parsed/compared later ───────────────────────
def test_capture_then_pipe_to_parser_is_flagged_at_the_use() -> None:
    src = 'out=$(npm view pkg version 2>&1)\nv=$(echo "$out" | tail -1)\n'
    assert _lines(src) == [2]


def test_capture_then_bracket_comparison_is_flagged() -> None:
    src = 'ver=$(tool --version 2>&1)\nif [[ "$ver" == "1.2.3" ]]; then\n  :\nfi\n'
    assert _lines(src) == [2]


def test_capture_then_arithmetic_is_flagged() -> None:
    src = "count=$(tool 2>&1)\nif ((count > 3)); then\n  :\nfi\n"
    assert _lines(src) == [2]


def test_capture_and_use_on_one_line_is_flagged() -> None:
    """A `;` separates two commands without starting a new line, so the read is
    governed by the capture beside it — the grammar sees both."""
    assert _lines('out=$(tool 2>&1); echo "$out" | grep x\n') == [1]


def test_same_line_reassignment_supersedes_the_merged_capture() -> None:
    src = 'out=$(tool 2>&1); out=$(clean_tool); echo "$out" | grep x\n'
    assert _lines(src) == []


def test_use_beyond_ten_lines_is_not_flagged() -> None:
    src = "out=$(tool 2>&1)\n" + ":\n" * 11 + 'echo "$out" | grep x\n'
    assert _lines(src) == []


def test_reassignment_without_merge_clears_tracking() -> None:
    src = 'out=$(tool 2>&1)\nout=$(clean_tool)\necho "$out" | grep x\n'
    assert _lines(src) == []


# ── legitimate corpus: diagnostics captures must produce ZERO findings ───
@pytest.mark.parametrize(
    "src",
    [
        # capture then echo/printf/log only — the dominant legit use
        'out=$(cmd 2>&1)\necho "$out"\n',
        'err=$(bash -n "$f" 2>&1)\nprintf "%s\\n" "$err" >&2\n',
        # capture then branch on exit code, not content
        'if ! out=$(curl -sf url 2>&1); then\n  echo "$out" >&2\n  exit 1\nfi\n',
        # emptiness test is diagnostics, not comparison
        'out=$(cmd 2>&1)\nif [[ -n "$out" ]]; then\n  echo "$out"\nfi\n',
        # merged stream piped to tee (not a parser)
        "./setup.sh 2>&1 | tee log.txt\n",
        # discard-into-null check, no substitution at all
        "if command -v jq >/dev/null 2>&1; then\n  :\nfi\n",
        # parse WITHOUT a merge — stdout-only parsing is the fix, never flagged
        "v=$(npm view pkg version | tail -1)\n",
        # merge without substitution or parse
        "make build 2>&1\n",
        # comment quoting the idiom
        "# bad: v=$(cmd 2>&1 | tail -1)\n",
    ],
)
def test_diagnostic_corpus_yields_zero_findings(src: str) -> None:
    assert _lines(src) == []


# ── text that only DEPICTS the idiom is not code ─────────────────────────
# The grammar rewrite's reason for existing: each of these carries the banned
# idiom verbatim, and none of them runs it as a value.
@pytest.mark.parametrize(
    "src",
    [
        # interpolated into a message a command prints — the substitution's
        # output is spliced between literal text, not taken as a value
        'gb_warn "v=$(cmd 2>&1 | tail -1) parses noise"\n',
        'echo "version: $(cmd 2>&1 | tail -1)"\n',
        # single quotes: no substitution at all, just characters
        "gb_warn 'v=$(cmd 2>&1 | tail -1)'\n",
        # a quoted-delimiter heredoc body is data written to a file
        "cat <<'EOF' > doc.txt\nv=$(cmd 2>&1 | tail -1)\nEOF\n",
        # `2>&1` as the literal text of an argument, not a redirection
        "grep -F '2>&1 | tail -1' notes.md | head -1\n",
        # capture whose EXIT STATUS is what the script branches on: the merge
        # feeds the failure path, so the success-path read is not the defect
        'if ! h=$(curl -sSf -I url 2>&1); then\n  echo "$h" >&2\n  exit 1\nfi\n'
        'echo "$h" | grep -qi "^x-scopes:"\n',
        'h=$(curl -sSf -I url 2>&1) || exit 1\necho "$h" | grep -qi "^x-scopes:"\n',
    ],
)
def test_depicted_or_guarded_idiom_yields_zero_findings(src: str) -> None:
    assert _lines(src) == []


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # The incident in the module docstring: an npm stderr warning merged into
        # "the version". The positive marker for the whole rewrite.
        ("v=$(npm view pkg version 2>&1 | tail -1)\n", [1]),
        # An UNQUOTED heredoc delimiter really does expand its body, so the same
        # idiom there is executed code — the twin that proves the quoted-heredoc
        # case above is not passing vacuously.
        ("cat <<EOF > doc.txt\nv=$(cmd 2>&1 | tail -1)\nEOF\n", [2]),
        # The twin of the exit-status-tested cases: the same read of the same
        # merged capture, written as a plain statement, still fires.
        ('h=$(curl -sSf -I url 2>&1)\necho "$h" | grep -qi "^x-scopes:"\n', [2]),
    ],
)
def test_executed_idiom_still_fires(src: str, expected: list[int]) -> None:
    assert _lines(src) == expected


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_on_flagged_line() -> None:
    assert _lines("v=$(cmd 2>&1 | tail -1) # stderr-merge-ok: sentinel\n") == []


def test_opt_out_on_line_above() -> None:
    assert (
        _lines("# stderr-merge-ok: merged on purpose\nv=$(cmd 2>&1 | tail -1)\n") == []
    )


def test_opt_out_on_a_later_physical_line_of_the_flagged_command() -> None:
    """The annotation is accepted on ANY physical line of the command it
    excuses — the grammar reports one node for a `\\`-continued command, so the
    line the reader annotates need not be the one the finding names."""
    src = (
        "status=$(curl -s -o /dev/null \\\n"
        '  -w "%{http_code}" 2>&1 |\n'
        "  tail -1) # stderr-merge-ok: the code IS the whole value\n"
    )
    assert _lines(src) == []


def test_opt_out_on_assignment_covers_later_use() -> None:
    src = 'out=$(cmd 2>&1) # stderr-merge-ok: both streams are the value\necho "$out" | grep x\n'
    assert _lines(src) == []


# ── main: argv routing ───────────────────────────────────────────────────
def test_main_flags_shell_file(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text("v=$(cmd 2>&1 | tail -1)\n")
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_scans_workflow_run_blocks(tmp_path, capsys) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    p = wf / "ci.yaml"
    p.write_text(
        "jobs:\n  j:\n    steps:\n      - run: |\n          v=$(cmd 2>&1 | tail -1)\n"
    )
    assert mod.main([str(p)]) == 1
    assert "ci.yaml" in capsys.readouterr().err


def test_main_skips_non_workflow_yaml(tmp_path) -> None:
    p = tmp_path / "data.yaml"
    p.write_text("x: v=$(cmd 2>&1 | tail -1)\n")
    assert mod.main([str(p)]) == 0


def test_main_clean_files_pass(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('out=$(cmd 2>&1)\necho "$out"\n')
    assert mod.main([str(p)]) == 0

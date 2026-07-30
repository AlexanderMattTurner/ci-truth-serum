"""Tests for ci_truth_serum/check_gh_slurp_jq.py — the pre-commit lint banning the
``gh api --slurp`` flag combinations gh rejects at argument validation.

Drives `violations()` directly so each rule is asserted in isolation. Reported
line numbers are the line the offending `command` node STARTS on (the bash
grammar's `start_point`), which is where the offending call begins — a
backslash-continued call is one node, so its flags on later lines are judged
against its own first line.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_gh_slurp_jq.py"
mod = load_hook("check_gh_slurp_jq.py", "check_gh_slurp_jq")


@pytest.mark.parametrize(
    "line",
    [
        # --slurp with --jq, either order
        "gh api \"repos/$R/pulls/$PR/reviews\" --paginate --slurp --jq '.[][].state'",
        "gh api --slurp --jq '.[][].id' --paginate \"repos/$R/labels\"",
        # the short jq spelling, and --template / its short spelling
        "gh api \"repos/$R/pulls\" --paginate --slurp -q '.[][].id'",
        'gh api "repos/$R/pulls" --paginate --slurp --template "{{.id}}"',
        'gh api "repos/$R/pulls" --paginate --slurp -t "{{.id}}"',
        # `--flag=value` spelling
        "gh api \"repos/$R/pulls\" --paginate --slurp --jq='.[][]'",
        # a `|` inside a quoted argument is not a command boundary, so the flags
        # after it are still gh api's — truncating there would lose the --slurp
        "gh api --paginate --jq '.a | .b' --slurp x",
        'gh api "repos/$R/x?q=a|b" --slurp',
        # a bundled short-flag cluster carrying gh api's `-q`/`-t`
        "gh api -iq '.[][]' --paginate --slurp x",
        # an argument's own `$( … )` must not end the segment before the flags
        "gh api \"$(build_url)\" --paginate --slurp --jq '.[][]'",
        # --slurp without --paginate
        'gh api "repos/$R/pulls" --slurp >/tmp/x.json',
        # inside a command substitution, and behind a retry wrapper / env prefix
        "n=$(gh api --paginate --slurp --jq '.[]' \"repos/$R/x\")",
        'out="$(retry_stdout gh api "repos/$R/x" --paginate --slurp --jq ".[]")"',
        'x="$(FOO="$F" retry_stdout gh api "repos/$R/x" --slurp --paginate -q .)"',
    ],
)
def test_fires_on_impossible_combination(line: str) -> None:
    assert mod.violations(line) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # the remedy shape: capture --paginate --slurp, filter in a separate jq
        'all="$(retry_stdout gh api --paginate --slurp "repos/$R/pulls")"',
        # the remedy with a substituted endpoint (negative control for the
        # paren-depth segmentation above)
        'all="$(gh api --paginate --slurp "$(build_url)")"',
        "gh api --paginate \"repos/$R/pulls\" | jq -s 'add // []'",
        "gh api --paginate --slurp \"repos/$R/p\" | jq -r '.[][].state'",
        # a DOWNSTREAM command's own short flags are not gh api's flags: the
        # `column -t` here would collide with gh api's `-t/--template` if the
        # scan were not confined to the gh api pipeline segment.
        "gh api --paginate --slurp \"repos/$R/p\" | jq -r '.[][]' | column -t",
        'gh api --paginate --slurp "repos/$R/p" | jq -r . | sort -t: -k2',
        # a quoted `|` does not open the downstream segment early: the real pipe
        # after it still bounds the scan, so `column -t` stays out of gh's flags
        'gh api "a?q=x|y" --paginate --slurp | jq -r . | column -t',
        # --jq without --slurp is fine
        "gh api \"repos/$R/pulls\" --paginate --jq '.[].number'",
        "labels=$(gh pr view \"$PR\" --json labels --jq '.labels[].name')",
        # prose, not a call
        "# gh api --paginate --slurp --jq '.[]' is rejected at validation",
        "#   `--slurp` alongside `--jq` at argument validation",
        # a printed example is not executed code
        "echo \"gh api --slurp --jq '.' will never run\"",
        # same-line annotation
        "gh api \"repos/$R/x\" --slurp --jq '.' # allow-gh-slurp-jq: pinned old gh",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_fires_across_a_backslash_continuation() -> None:
    # The live shape: `gh api` and its flags span continued lines, so a
    # same-line-only regex would miss the `--jq` entirely.
    text = (
        'reviews="$(retry_stdout gh api --paginate --slurp \\\n'
        '  "repos/$R/issues/$PR/comments" \\\n'
        "  --jq '.[][] | .id')\"\n"
    )
    assert mod.violations(text) == [1]


def test_fires_across_a_multiline_capture_with_no_trailing_operator() -> None:
    # An unclosed `$(` spans lines with no trailing `\`; the call it contains is
    # reported on its OWN line, not on the line that opened the capture.
    text = "out=$(\n  gh api --paginate --slurp --jq '.[][]' \"repos/$R/p\"\n)\n"
    assert mod.violations(text) == [2]


def test_fires_across_a_trailing_pipe_continuation() -> None:
    # The `--jq` sits on the gh api segment; the continuation is what carries it.
    text = "gh api --paginate --slurp --jq '.[][]' |\n  tee /tmp/out\n"
    assert mod.violations(text) == [1]


def test_pipe_continuation_does_not_import_the_next_command_flags() -> None:
    # Negative control for the case above: the remedy shape wrapped across the
    # same trailing-pipe continuation must stay clean.
    text = "gh api --paginate --slurp \"repos/$R/p\" |\n  jq -r '.[][]' | column -t\n"
    assert mod.violations(text) == []


def test_continuation_does_not_leak_into_the_next_command() -> None:
    # A --jq on a LATER, separate command is not part of the gh api call.
    text = (
        'all="$(gh api --paginate --slurp "repos/$R/pulls")"\n'
        "ids=$(gh api --paginate \"repos/$R/x\" --jq '.[].id')\n"
    )
    assert mod.violations(text) == []


def test_a_clean_first_call_does_not_mask_a_second_on_the_same_line() -> None:
    text = 'gh api "$A" --paginate --slurp ; gh api "$B" --slurp --jq .\n'
    assert mod.violations(text) == [1]


def test_annotation_placements() -> None:
    same_line = "gh api \"repos/$R/x\" --slurp --jq '.' # allow-gh-slurp-jq: reason\n"
    assert mod.violations(same_line) == []
    preceding = "# allow-gh-slurp-jq: reason\ngh api \"repos/$R/x\" --slurp --jq '.'\n"
    assert mod.violations(preceding) == []
    stale = (
        "# allow-gh-slurp-jq: reason\ndo_a\ngh api \"repos/$R/x\" --slurp --jq '.'\n"
    )
    assert mod.violations(stale) == [3]


@pytest.mark.parametrize(
    "text",
    [
        # bare annotation, no colon / no reason -> does not suppress
        "gh api x --slurp --jq . # allow-gh-slurp-jq",
        "gh api x --slurp --jq . # allow-gh-slurp-jq:",
        "gh api x --slurp --jq . # allow-gh-slurp-jq:   ",
    ],
)
def test_bare_annotation_without_reason_still_fires(text: str) -> None:
    assert mod.violations(text) == [1]


def test_annotation_on_the_line_before_a_continued_call() -> None:
    text = (
        "# allow-gh-slurp-jq: reason\n"
        'reviews="$(gh api --paginate --slurp \\\n'
        "  --jq '.[][]')\"\n"
    )
    assert mod.violations(text) == []


def test_reports_every_offending_call_in_order() -> None:
    text = (
        "#!/usr/bin/env bash\n"
        'gh api "repos/$R/a" --slurp\n'
        'ok="$(gh api --paginate --slurp "repos/$R/b")"\n'
        "gh api --paginate --slurp --jq '.' \"repos/$R/c\"\n"
    )
    assert mod.violations(text) == [2, 4]


def test_flags_a_call_whose_last_line_is_left_continued() -> None:
    # A file ending mid-continuation (trailing `\` with no following line) leaves a
    # logical line the loop never terminates; it must still be matched.
    text = 'out=$(gh api --paginate --slurp \\\n  --jq ".[][]" "repos/$R/p" \\\n'
    assert mod.violations(text) == [1]


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour (skip-unreadable, exit codes) is covered
    once in test_linecheck.py; here we only pin that main() emits THIS message."""
    bad = tmp_path / "bad.sh"
    bad.write_text('gh api "repos/$R" --slurp\n', encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:1: `gh api --slurp` is rejected" in capsys.readouterr().err
    good = tmp_path / "good.sh"
    good.write_text('j="$(gh api --paginate --slurp "repos/$R")"\n', encoding="utf-8")
    assert mod.main([str(good)]) == 0


def _run_script(*paths: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as pre-commit does (paths on argv)."""
    return subprocess.run(
        [sys.executable, str(_SRC), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_rejects_impossible_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text(
        "x=$(gh api --paginate --slurp --jq '.[][]' \"repos/$R/p\")\n",
        encoding="utf-8",
    )
    proc = _run_script(str(bad))
    assert proc.returncode == 1
    assert f"{bad}:1: `gh api --slurp` is rejected" in proc.stderr


def test_script_accepts_remedy_and_annotated(tmp_path: Path) -> None:
    good = tmp_path / "good.sh"
    good.write_text(
        'all="$(retry_stdout gh api --paginate --slurp "repos/$R/pulls")"\n'
        'state="$(jq -r \'.[][].state\' <<<"$all")"\n'
        "gh api \"repos/$R/x\" --slurp --jq '.' # allow-gh-slurp-jq: pinned old gh\n",
        encoding="utf-8",
    )
    proc = _run_script(str(good))
    assert proc.returncode == 0
    assert proc.stderr == ""


_IDIOM = "gh api --slurp --jq . is rejected at argument validation"


@pytest.mark.parametrize(
    ("name", "text"),
    [
        # A heredoc body is DATA the shell writes out, not a command it runs.
        ("quoted heredoc", f"cat <<'EOF' > doc.txt\n{_IDIOM}\nEOF\n"),
        ("expanding heredoc", f"cat <<EOF > doc.txt\n{_IDIOM}\nEOF\n"),
        # A message string is text its command prints. `gb_warn` is deliberately
        # NOT in the shared printer prefix list, so only the grammar (a `string`
        # argument holds no commands) can keep this clean.
        ("message string", f'gb_warn "{_IDIOM}"\n'),
        ("here-string", f'grep -q slurp <<<"{_IDIOM}"\n'),
        # A `--jq` inside a documentation string passed to a house helper.
        ("multi-arg message", f'log_hint "remedy" "{_IDIOM}"\n'),
        # An UNQUOTED printed example: real command words, but the command that
        # holds them only prints (the shared printer-prefix list).
        ("unquoted echo example", f"echo {_IDIOM}\n"),
    ],
)
def test_the_idiom_as_data_is_not_a_call(name: str, text: str) -> None:
    assert mod.violations(text) == [], name


def test_the_data_cases_are_not_vacuous() -> None:
    """Positive marker for the case above: the very same words, spelled as a real
    command instead of quoted data, still fire. Without this the parametrization
    could be passing because the idiom itself stopped being recognized."""
    assert mod.violations("gh api --slurp --jq . repos/x/y\n") == [1]
    assert mod.violations(f'gb_warn "{_IDIOM}"\ngh api --slurp --jq . repos/x\n') == [2]


def test_a_redirection_is_not_a_flag() -> None:
    # `>` and its target are a file_redirect sibling of the command, never an
    # argument — so the sanctioned capture stays clean even when the redirect
    # target's own text looks flag-shaped.
    assert mod.violations('gh api "$R" --paginate --slurp >"$out"\n') == []
    assert mod.violations('gh api "$R" --paginate --slurp >-q.json\n') == []


def test_a_downstream_command_keeps_its_own_flags() -> None:
    # Each pipeline stage is its own `command` node, so a `jq -t`/`column -t`
    # after the remedy cannot be read as gh api's `--template`.
    text = 'gh api --paginate --slurp "$R" | jq -r . | column -t\n'
    assert mod.violations(text) == []
    # Positive marker: moving the same `-t` onto the gh api call does fire.
    assert mod.violations('gh api --paginate --slurp -t "{{.x}}" "$R"\n') == [1]


def test_annotation_is_read_from_the_widest_span_starting_on_a_line() -> None:
    """Two flagged calls can begin on the same physical line with different
    extents; the annotation lookup must cover the WIDER one, or an opt-out on the
    continued call's own second line would be missed."""
    text = 'gh api "$A" --slurp && gh api "$B" --slurp \\\n  --jq .\n'
    assert mod.violations(text) == [1]
    annotated = text.replace("--jq .", "--jq . # allow-gh-slurp-jq: reason")
    assert mod.violations(annotated) == []


def test_main_routes_workflow_yaml_to_run_blocks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `.github/workflows` YAML path has each inline `run:` block scanned as
    shell and is reported at the STEP's line, not the line inside the block."""
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    bad = workflows / "ci.yaml"
    bad.write_text(
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - run: echo hello\n"
        "      - name: slurp\n"
        "        run: |\n"
        '          gh api "repos/$R" --slurp --jq .\n',
        encoding="utf-8",
    )
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:5: `gh api --slurp` is rejected" in capsys.readouterr().err


def test_main_skips_non_workflow_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A YAML file outside `.github/{workflows,actions}` is not shell, so it is
    not scanned — its `gh api` text is data in some other tool's config."""
    other = tmp_path / "config.yaml"
    other.write_text("note: gh api --slurp --jq . is rejected\n", encoding="utf-8")
    assert mod.main([str(other)]) == 0
    assert capsys.readouterr().err == ""


def test_main_fails_loudly_on_pathological_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An input the grammar refuses to parse safely is a LOUD failure, never a
    silent pass — skipping it would false-green the input an adversary controls."""
    huge = tmp_path / "huge.sh"
    huge.write_text("true " + "| true " * 3000 + "\n", encoding="utf-8")
    assert mod.main([str(huge)]) == 1
    assert "pipe bytes" in capsys.readouterr().err


def test_own_shell_tree_is_clean() -> None:
    """Dogfood gate: no shell file in this repo carries an impossible gh api call.
    Non-vacuous because the file list is asserted non-empty."""
    tracked = subprocess.check_output(
        ["git", "ls-files", "*.sh", "*.bash"], text=True, cwd=REPO_ROOT
    ).split()
    assert tracked
    offenders = []
    for rel in tracked:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        offenders += [f"{rel}:{n}" for n in mod.violations(text)]
    assert offenders == [], f"impossible gh api --slurp calls: {offenders}"

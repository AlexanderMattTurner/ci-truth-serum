"""Tests for ci_truth_serum/check_exit_suppression.py — the pre-commit lint that bans
unjustified exit-status suppression (`|| true` / `|| :`).

Drives `violations()` directly so each rule is asserted in isolation.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_exit_suppression.py"
mod = load_hook("check_exit_suppression.py", "check_exit_suppression")


@pytest.mark.parametrize(
    "line",
    [
        # exit status dropped while the command's output stays on the terminal
        "some_teardown_func || true",
        "ls -la /usr/local/bin || true",
        "wait_for_ready || :",
        "git config --get-all x || true",
        # `|| :` is the same no-op suppressor as `|| true`
        "reap_volumes || :",
        # a suppressor glued to a following metacharacter (no trailing space) must
        # still fire — the old `(?:\s|;|$)` boundary missed `|| true&` / `|| :;`.
        "cleanup || true&",
        "cleanup || true;next_cmd",
        "cleanup || :;next_cmd",
    ],
)
def test_fires_on_output_kept_suppression(line: str) -> None:
    assert mod.violations(line) == [1]


@pytest.mark.parametrize(
    "line",
    [
        # a longer command NAMED true.../: ... is not the `true`/`:` builtin, so the
        # `(?![\w-])` boundary must NOT fire on it (negative control for the glued
        # boundary — pairs with the positives above).
        "run_thing || truelove",
        "run_thing || true-ish",
    ],
)
def test_does_not_fire_on_longer_command_name(line: str) -> None:
    assert mod.violations(line) == []


@pytest.mark.parametrize(
    "text",
    [
        # value capture: the `|| true` is inside $( … ), failure -> empty string
        "out=$(maybe_fails || true)",
        "result=$(docker ps -q || true)",
        # process substitution capture
        "diff <(gen_a || true) <(gen_b)",
        # backtick capture
        "x=`maybe_fails || true`",
        # assignment whose whole RHS is a substitution: var=$(cmd) || true
        "out=$(docker ps -q) || true",
        'name="$(get_name)" || true',
        # output already discarded -> nothing left to surface
        "rm -rf /tmp/x >/dev/null 2>&1 || true",
        "docker rm -f c 2>/dev/null || true",
        "cleanup &>/dev/null || true",
        # whole-line comment, not real code
        "# foo || true is fine",
        # a suppressor quoted inside a printed message is an example, not code
        'echo "run: cmd || true to ignore errors"',
        'warn "use || true sparingly"',
        # same-line opt-out annotation
        "reap || true  # allow-exit-suppress: best-effort GC reaper",
        # no suppression at all
        "docker rm -f c",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_annotation_on_preceding_line() -> None:
    text = (
        "# allow-exit-suppress: best-effort diagnostic before the exit\n"
        "ls -la /usr/local/bin || true\n"
    )
    assert mod.violations(text) == []


def test_annotation_two_lines_above_does_not_count() -> None:
    # The opt-out must be on the same or the immediately-preceding line — a stale
    # annotation further up must not silence an unrelated suppressor.
    text = "# allow-exit-suppress: something else\ndo_a_real_thing\nls -la || true\n"
    assert mod.violations(text) == [3]


@pytest.mark.parametrize(
    "text",
    [
        # bare annotation, no colon / no reason -> does not suppress
        "reap || true  # allow-exit-suppress",
        # colon but an empty reason -> does not suppress
        "reap || true  # allow-exit-suppress:",
        "reap || true  # allow-exit-suppress:   ",
    ],
)
def test_bare_annotation_without_reason_still_fires(text: str) -> None:
    assert mod.violations(text) == [1]


def test_bare_annotation_on_preceding_line_does_not_suppress() -> None:
    # A reasonless opt-out on the line above is treated as absent, so the suppressor
    # on the next line is still flagged (at its own physical line).
    text = "# allow-exit-suppress\nls -la || true\n"
    assert mod.violations(text) == [2]


def test_multiline_pipe_continuation_is_joined() -> None:
    # A command whose `$( … )` capture spans a trailing-pipe continuation must be
    # analyzed whole: the `|| true` is inside the capture, so it must not fire.
    text = "out=$(gen_thing |\n  filter_thing) || true\n"
    assert mod.violations(text) == []


def test_multiline_backslash_continuation_is_joined() -> None:
    text = "out=$(make_thing \\\n  --flag) || true\n"
    assert mod.violations(text) == []


def test_multiline_open_paren_capture_is_not_flagged() -> None:
    # A value capture whose `$( … )` body spans lines with NO trailing-operator
    # continuation (the line just ends in an open `$(`). The `|| true` inside is part
    # of the capture, exactly like the single-line `var=$(cmd || true)` form, so it
    # must not fire. This is the case the trailing-`|`/`\` join alone missed.
    text = "result=$(\n  some_command || true\n)\n"
    assert mod.violations(text) == []


def test_multiline_process_substitution_capture_is_not_flagged() -> None:
    text = "diff <(\n  gen_a || true\n) <(gen_b)\n"
    assert mod.violations(text) == []


def test_suppression_after_a_closed_multiline_capture_still_fires() -> None:
    # The substitution-aware join must END when the capture closes: a real `|| true`
    # on a later line is still flagged, at its own physical line — the join must not
    # swallow everything after an opening `$(`.
    text = "out=$(\n  gen\n)\nreal_cmd || true\n"
    assert mod.violations(text) == [4]


def test_escaped_backtick_does_not_mask_a_real_suppression() -> None:
    # An escaped backtick (`\``) is a literal, not a substitution delimiter, so it must
    # not flip the backtick-parity check and hide the real `|| true` after it. Under
    # the old `count("`") % 2` logic this suppression was silently missed.
    text = "val=foo\\`bar ; cleanup || true\n"
    assert mod.violations(text) == [1]


def test_dangling_final_continuation_is_still_scanned() -> None:
    # A file ending mid-continuation (last line trails in `|`, no resolving line)
    # must still be analyzed — the suppressor on it is not silently dropped.
    assert mod.violations("ls -la || true |") == [1]


def _is_shell(path: Path) -> bool:
    """Match the pre-commit hook's `types: [shell]` selection: a .bash/.sh file,
    or an extensionless script whose shebang names a shell — so the test scans the
    same set the hook does."""
    if path.suffix in (".bash", ".sh"):
        return True
    if path.suffix:
        return False
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except (OSError, IndexError):
        return False
    return bool(first) and first[0].startswith("#!") and "sh" in first[0]


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour (skip-unreadable, exit codes) is covered
    once in test_linecheck.py; here we only pin that main() emits THIS message."""
    bad = tmp_path / "bad.sh"
    bad.write_text("teardown || true\n", encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:1: exit status suppressed" in capsys.readouterr().err


def _run_script(*paths: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as pre-commit does (paths on argv)."""
    return subprocess.run(
        [sys.executable, str(_SRC), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "line",
    [
        "teardown_func || true\n",  # `|| true`
        "wait_for_ready || :\n",  # `|| :` is the same no-op suppressor
    ],
)
def test_script_rejects_suppression(tmp_path: Path, line: str) -> None:
    """The real script exits non-zero and names the offending file:line for both
    suppressor spellings."""
    bad = tmp_path / "bad.sh"
    bad.write_text(line, encoding="utf-8")
    proc = _run_script(str(bad))
    assert proc.returncode == 1
    assert f"{bad}:1: exit status suppressed" in proc.stderr


def test_script_accepts_annotated_and_captured(tmp_path: Path) -> None:
    """Negative control: an annotated suppressor, a value capture, and a
    discarded-output suppressor are all accepted (exit 0)."""
    good = tmp_path / "good.sh"
    good.write_text(
        "reap || true  # allow-exit-suppress: best-effort GC reaper\n"
        "out=$(docker ps -q || true)\n"
        "rm -rf /tmp/x >/dev/null 2>&1 || true\n",
        encoding="utf-8",
    )
    proc = _run_script(str(good))
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_own_shell_tree_is_clean() -> None:
    """ci-truth-serum's own shell hooks must pass — a new unannotated `|| true`
    there turns this red, proving the check is wired to real sources, not just unit
    cases. Scoped to hooks/ (the package's own scripts)."""
    tracked = subprocess.check_output(
        ["git", "ls-files", "hooks/"], text=True, cwd=REPO_ROOT
    ).split()
    offenders = []
    for rel in tracked:
        path = REPO_ROOT / rel
        if not _is_shell(path):
            continue
        hits = mod.violations(path.read_text(encoding="utf-8", errors="replace"))
        offenders += [f"{rel}:{n}" for n in hits]
    assert offenders == [], (
        f"unannotated exit-status suppression in hooks/: {offenders}"
    )

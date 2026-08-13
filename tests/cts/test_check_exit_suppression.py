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
        # still fire: the `&` / `;` terminates the list, so `true` is the whole
        # right operand.
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
        # a longer command NAMED true... is not the `true`/`:` builtin, so it must
        # NOT fire (negative control for the glued-metacharacter positives above).
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
    # The capture ENDS at its closing paren: a real `|| true` on a later line is
    # still flagged, at its own physical line — an opening `$(` must not swallow
    # everything after it.
    text = "out=$(\n  gen\n)\nreal_cmd || true\n"
    assert mod.violations(text) == [4]


def test_escaped_backtick_does_not_mask_a_real_suppression() -> None:
    # An escaped backtick (`\``) is a literal, not a substitution delimiter, so the
    # `|| true` after it is at statement level, not inside a capture — a
    # backtick-parity count read it as captured and missed the suppression.
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
        ["git", "ls-files", "ci_truth_serum/"], text=True, cwd=REPO_ROOT
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


# ── The mutating-git exception to the output-discard allowance ────────────────
#
# `>/dev/null || true` is normally accepted: nothing is left to surface. For a git
# command that changes the worktree/index/history the reasoning inverts — the exit
# status is the only evidence the mutation ran, and the next command reads the tree
# it was supposed to produce, so the failure becomes a wrong answer rather than a
# silence.
@pytest.mark.parametrize(
    "line",
    [
        'git -C "$raw" merge --no-commit --no-ff "$base" >/dev/null || true',
        "git add -A -- packaging >/dev/null || true",
        'git commit -m "x" >/dev/null 2>&1 || true',
        'git reset --hard "$sha" &>/dev/null || true',
        "git cherry-pick --abort >/dev/null 2>&1 || :",
        "git switch -c tmp >/dev/null || true",
        "git update-ref -d refs/x >/dev/null || true",
        "git rebase origin/main >/dev/null || true",
        'git_as_bot -C "$r" merge --no-ff "$b" >/dev/null 2>&1 || true',
        'git -c user.name=b -c user.email=b@b commit -m "x" >/dev/null || true',
    ],
)
def test_a_discarded_output_does_not_excuse_a_mutating_git_command(line: str) -> None:
    assert mod.violations(line + "\n") == [1]


@pytest.mark.parametrize(
    "line",
    [
        'git grep -nE "$RE" -- . >/dev/null || true',
        'git log --oneline -1 "$sha" >/dev/null || true',
        'git rev-parse "$ref" >/dev/null 2>&1 || true',
        'git fetch origin "$branch" >/dev/null || true',
        "git ls-files -s -z >/dev/null || true",
        "git status --porcelain >/dev/null 2>&1 || true",
    ],
)
def test_a_read_only_git_command_keeps_the_output_discard_allowance(line: str) -> None:
    assert mod.violations(line + "\n") == []


def test_a_mutating_git_command_is_still_excusable_by_annotation() -> None:
    text = (
        'git merge "$b" >/dev/null || true  # allow-exit-suppress: caller re-derives\n'
    )
    assert mod.violations(text) == []


def test_a_bare_annotation_does_not_excuse_a_mutating_git_command() -> None:
    assert mod.violations(
        'git merge "$b" >/dev/null || true  # allow-exit-suppress\n'
    ) == [1]


def test_a_mutating_git_command_inside_a_capture_stays_allowed() -> None:
    """A value capture is empty-on-failure and the caller already handles that —
    the mutation exception narrows the OUTPUT-DISCARD allowance, not this one."""
    assert mod.violations('v=$(git merge "$b" >/dev/null || true)\n') == []


def test_a_non_git_command_keeps_the_output_discard_allowance() -> None:
    assert mod.violations("docker rm -f box >/dev/null 2>&1 || true\n") == []


def test_every_declared_mutating_verb_is_actually_refused() -> None:
    """Iterate the lint's own verb list rather than restating it: a verb added to
    the set and then misspelled would otherwise pass unnoticed."""
    verbs = sorted(mod._GIT_MUTATION_VERBS)
    assert len(verbs) >= 10
    for verb in verbs:
        line = f"git {verb} --flag >/dev/null || true"
        assert mod.violations(line + "\n") == [1], verb


# ── The grammar decides what is code: strings and heredoc bodies are not ──────
#
# The suppressor is a `||` list node whose right operand is the `true`/`:` builtin,
# so text that merely SPELLS one is never a finding. This is what retired the
# lint's enumerated "commands that only print" excuse list — a project's own logger
# names are unenumerable, and a string argument is never code whatever prints it.
@pytest.mark.parametrize(
    "text",
    [
        # an arbitrary project logger, in neither this lint's nor _linecheck's list
        'gb_warn "do not write cmd || true here"',
        "gb_warn 'do not write cmd || true here'",
        'cg_note "cmd || true is banned"',
        'some_house_specific_logger "cmd || :"',
        # the suppressor is an argument of a command that does something else
        'grep -n "|| true" -- "$file"',
        'printf "%s\\n" "cmd || true"',
        # a `;` / `|` / `&&` inside a string separates nothing, so the surrounding
        # command is unchanged — here one whose output IS discarded
        'foo >/dev/null "a; b" || true',
        'foo >/dev/null "a | b" || true',
        'foo >/dev/null "a && b" || true',
        # a git MUTATION named inside a string is text: the command being suppressed
        # is `printer`, which keeps the output-discard allowance
        'printer >/dev/null "git merge x" || true',
        # heredoc bodies are data, quoted and unquoted alike
        "cat <<'EOF' > doc.txt\nrun cmd || true here\nEOF\n",
        "cat <<EOF > doc.txt\nrun $cmd || true here\nEOF\n",
        # a command substitution nested in an argument is still a value capture
        'foo "$(bar || true)"',
    ],
)
def test_text_that_only_spells_a_suppressor_does_not_fire(text: str) -> None:
    assert mod.violations(text) == []


@pytest.mark.parametrize(
    "text,expected",
    [
        # POSITIVE MARKERS pairing with the negatives above: the same shapes with the
        # suppressor as real code still fire, so those cases prove the grammar
        # distinction and not a detector that stopped firing at all.
        ('gb_warn "do not write cmd || true here"\nreal_cleanup || true\n', [2]),
        (
            "cat <<'EOF' > doc.txt\nrun cmd || true here\nEOF\nreal_cleanup || true\n",
            [4],
        ),
        # a redirect spelled INSIDE a string is not a redirect, so the output is
        # still on the terminal and the allowance does not apply (the text scan this
        # replaced read the quoted `2>/dev/null` as a real discard and missed this)
        ('foo "2>/dev/null" || true', [1]),
        # the capture allowance is decided from the substitution node, not from an
        # unbalanced `$(` to the left: this one is CLOSED before the suppressor
        ('foo "$(bar)" || true', [1]),
        # a discard on the FIRST branch does not excuse the last one, whose status is
        # the one being suppressed
        ("a >/dev/null && b || true", [1]),
        # …nor does one belonging to a command NESTED under the suppressed one: the
        # allowance needs a redirect on the suppressed command itself
        ("diff <(a >/dev/null) <(b) || true", [1]),
        ("{ a >/dev/null; b; } || true", [1]),
    ],
)
def test_real_suppressions_still_fire(text: str, expected: list[int]) -> None:
    assert mod.violations(text) == expected


def test_pathological_input_fails_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An input the grammar refuses to parse (tree-sitter-bash allocates
    quadratically on chained pipelines) is reported and exits 1 — never skipped as
    a silent no-findings pass — and the paths beside it are still checked."""
    pathological = tmp_path / "huge.sh"
    pathological.write_text("cmd " + "| cmd " * 3000 + "\n", encoding="utf-8")
    with pytest.raises(mod.PathologicalInputError):
        mod.violations(pathological.read_text(encoding="utf-8"))
    bad = tmp_path / "bad.sh"
    bad.write_text("teardown || true\n", encoding="utf-8")
    assert mod.main([str(pathological), str(bad)]) == 1
    err = capsys.readouterr().err
    assert "pipe bytes" in err
    assert f"{bad}:1: exit status suppressed" in err


# ── recall: a shell's own `-c` body is a script, not a datum ────────────
@pytest.mark.parametrize(
    "text",
    [
        'bash -c "cleanup || true"',
        "sh -c 'cleanup || true'",
        "dash -c 'cleanup || true'",
        "bash -ec 'cleanup || true'",
    ],
)
def test_suppression_inside_a_shell_c_body_is_flagged(text: str) -> None:
    assert mod.violations(text + "\n") == [1]


def test_multiline_shell_c_body_reports_the_enclosing_file_line() -> None:
    """A finding on the body's third line lands on the file line that carries it,
    which is where the reader writes the annotation."""
    src = 'echo start\nbash -c "\n  setup\n  cleanup || true\n"\n'
    assert mod.violations(src) == [4]


@pytest.mark.parametrize(
    "text",
    [
        # every allowance the outer rules grant holds one quoting layer down
        "bash -c 'cleanup >/dev/null || true'",
        "bash -c 'v=$(cmd) || true'",
        # a capture around the whole call is the outer exemption, and the body
        # must not report through it
        "out=$(bash -c 'cleanup || true')",
        # the annotation is read off the enclosing file's line
        "bash -c 'cleanup || true'  # allow-exit-suppress: best-effort reaper",
        # not a shell, so the argument is a datum this lint cannot read as code
        "docker run img 'cleanup || true'",
        # a body carrying an escape is NOT the script bash runs, so it is skipped
        # rather than guessed at
        'bash -c "printf \\"x\\"; cleanup || true"',
        # `-c` with no quoted body names no script
        "bash -c $script",
        # a `|| true` in an argument that is not the `-c` body is still data
        "bash script.sh 'cleanup || true'",
        # the body is the argument IMMEDIATELY after `-c`; a later one is a
        # positional for the script, not the script
        "bash -c script.sh 'cleanup || true'",
        # the shell must BE the command word: behind a wrapper the same tokens
        # can equally be a printer's arguments (`echo bash -c "x || true"`), and
        # the grammar cannot tell those apart
        'timeout 5 bash -c "cleanup || true"',
    ],
)
def test_shell_c_body_precision(text: str) -> None:
    assert mod.violations(text + "\n") == []


# ── the capture allowance has one exception: a producer that reports its own
# failure through stdout (`gh api`/`gh graphql`, `jq`/`yq`, `curl -f`) ────────
@pytest.mark.parametrize(
    "text",
    [
        'm="$(gh api "repos/o/r/commits/$sha" --jq .commit.message || true)"\n',
        'm="$(gh api repos/o/r/commits/x --jq .commit.message)" || true\n',
        "m=$(gh api repos/o/r --jq .name 2>/dev/null || true)\n",
        'd="$(gh graphql -f query=@q.gql || true)"\n',
        # a global flag whose VALUE would read as the subcommand
        'd="$(gh -R owner/repo api repos/o/r || true)"\n',
        'v="$(jq -r .version pkg.json || true)"\n',
        'v="$(yq -r .version pkg.yaml || true)"\n',
        # jq as the LAST pipeline stage — the pipe kept its status, `||` drops it
        'v="$(cat pkg.json | jq -r .version)" || true\n',
        'b="$(curl -f https://example.test/x || true)"\n',
        'b="$(curl -fsSL https://example.test/x || true)"\n',
        'b="$(curl --fail-with-body https://example.test/x || true)"\n',
        'v="$(jq -r .version pkg.json || :)"\n',
        'v="$(/usr/bin/jq -r .version pkg.json || true)"\n',
        'v="$(command jq -r .version pkg.json || true)"\n',
        # several statements: the substitution reports the LAST one
        'v="$(setup; jq -r .version pkg.json)" || true\n',
        # a trailing statement terminator inside the capture
        'v="$(jq -r .version pkg.json;)" || true\n',
    ],
)
def test_fires_on_a_suppressed_producer_capture(text: str) -> None:
    assert mod.violations(text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # the ordinary idiom the capture allowance keeps: an empty-on-failure
        # capture of a command that is not a producer
        'f="$(grep -E "^x" list || true)"\n',
        'r="$(git rev-parse HEAD 2>/dev/null || true)"\n',
        'd="$(find . -name x || true)"\n',
        # a NON-final pipeline stage: the pipe dropped the status, not `||`
        'v="$(jq -r .version pkg.json | head -1 || true)"\n',
        'h="$(curl -sf https://example.test/x -I | awk "{print $2}" || true)"\n',
        # bare `curl` prints the error page as ordinary output
        'b="$(curl -sS -o /dev/null -w "%{http_code}" https://example.test/x || true)"\n',
        'b="$(curl -sS --max-time 5 https://example.test/x || true)"\n',
        'l="$(gh pr list --limit 1 || true)"\n',
        # a negated capture inverts the status
        'v="$(! jq -r .version pkg.json)" || true\n',
        # same-line opt-out annotation
        'v="$(gh api repos/o/r || true)"  # allow-exit-suppress: probe treats an unreachable API as inconclusive\n',
        # the two-probe audit (shell-lint-parsing.md): a producer capture spelled
        # inside a message string or a heredoc body holds no commands at all
        "gb_warn 'v=\"$(jq -r .version pkg.json || true)\"'\n",
        "cat <<'EOF' > doc.txt\nv=\"$(jq -r .version pkg.json || true)\"\nEOF\n",
    ],
)
def test_clean_producer_captures_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


# ── producer_only: a consumer that wants the captured-producer rule alone,
# over a tree wider than it has ever enforced the uncaptured rule on ──────────
def test_producer_only_still_flags_a_captured_producer() -> None:
    assert mod.violations('v="$(gh api repos/o/r || true)"\n', producer_only=True) == [
        1
    ]


def test_producer_only_skips_the_uncaptured_rule() -> None:
    """The uncaptured rule (bare `|| true` with output kept) is what
    `producer_only` exists to skip — a consumer scoped wider than it has ever
    checked that rule must not suddenly inherit it."""
    assert mod.violations("some_teardown_func || true\n", producer_only=True) == []


def test_producer_only_skips_the_git_mutation_rule() -> None:
    assert (
        mod.violations(
            'git merge --no-commit --no-ff "$base" >/dev/null || true\n',
            producer_only=True,
        )
        == []
    )


def test_main_producer_only_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text('v="$(jq -r .version pkg.json || true)"\nteardown || true\n')
    assert mod.main(["--producer-only", str(bad)]) == 1
    out = capsys.readouterr().err
    assert f"{bad}:1:" in out
    assert f"{bad}:2:" not in out

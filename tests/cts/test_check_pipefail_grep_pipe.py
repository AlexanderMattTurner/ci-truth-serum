"""Tests for ci_truth_serum/check_pipefail_grep_pipe.py — the pre-commit lint that bans
a TESTED pipeline ending in a reader that answers before its input ends. The reader
exits, the still-writing producer dies with SIGPIPE (141), and pipefail surfaces 141 as
the pipeline status, so a MATCH reads as NO-MATCH.

Drives `violations()` directly so each rule is asserted in isolation. Every enumerated
set the module carries is driven from the module's own constant, so dropping a member
turns a test red instead of silently narrowing the check.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_pipefail_grep_pipe.py"
mod = load_hook("check_pipefail_grep_pipe.py", "check_pipefail_grep_pipe")

_PIPEFAIL = "set -euo pipefail\n"

CONSUMER_FIXTURES = Path(__file__).parent / "fixtures" / "consumer"


def _flag(body: str) -> list[int]:
    """Line numbers flagged in a pipefail-enabled file whose first line is the
    pipefail declaration (so BODY's line numbers are offset by 1)."""
    return mod.violations(_PIPEFAIL + body)


def _tested(pipeline: str) -> list[int]:
    """Line numbers flagged when PIPELINE's status is read by an enclosing `if`."""
    return _flag(f"if {pipeline}; then :; fi\n")


# --- non-vacuity against the defect that shipped ---------------------------------
# `sbx-kit/image/lib/create-users.sh` in AlexanderMattTurner/agent-glovebox writes a
# PostToolUse hook out of a `<<'HOOK'` heredoc. Inside that hook, an entire agent tool
# result reached `printf '%s' "$input" | grep -qiE '<network-failure signatures>'` under
# `set -uo pipefail`, so a large result exited 141 and the `!` read it as "no signature".
# The fixture is that file at `d9992573`, byte for byte through the end of the hook it
# writes (line 486). Only the tail beyond that is dropped, so every line number up to
# and including 366 is the real file's.
def test_flags_the_consumer_defect_at_the_shipping_commit() -> None:
    """The bad commit's line 366 is the `printf '%s' "$input" | grep -qiE …` line
    inside the generated hook. Reaching it needs all three gaps closed: the heredoc
    descent to see the line at all, the literal-argument rule to deny `printf` its
    bounded exemption, and the pipeline's `!` to count as reading the status."""
    text = (CONSUMER_FIXTURES / "create-users-d9992573.sh.txt").read_text(
        encoding="utf-8"
    )
    assert mod.violations(text) == [366]
    assert "grep -qiE" in text.splitlines()[365]


def test_consumer_here_string_fix_is_clean() -> None:
    """Commit `d744f9be` replaced that pipe with a here-string, which is the remedy.
    Applying that one edit to the fixture clears the finding, so the check reports the
    defect and not merely the file."""
    text = (CONSUMER_FIXTURES / "create-users-d9992573.sh.txt").read_text(
        encoding="utf-8"
    )
    lines = text.splitlines()
    pipe, pattern = lines[365].split(" | grep -qiE ", maxsplit=1)
    producer = pipe.removeprefix("if ! ")
    assert producer == '''printf '%s' "$input"''', producer
    lines[365] = f'if ! grep -qiE {pattern.removesuffix("; then")} <<<"$input"; then'
    assert mod.violations("\n".join(lines) + "\n") == []


# --- gap 1: the bounded-producer exemption needs LITERAL arguments ---------------
@pytest.mark.parametrize("producer", sorted(mod._BOUNDED_PRODUCERS))
def test_bounded_producer_with_literal_arguments_is_exempt(producer: str) -> None:
    """A builtin writing an already-materialized literal cannot outrun the 64 KiB pipe
    buffer, so it keeps its exemption. Driven from `_BOUNDED_PRODUCERS`."""
    assert _tested(f"{producer} hello | grep -q x") == []


@pytest.mark.parametrize("producer", sorted(mod._BOUNDED_PRODUCERS - {":"}))
def test_bounded_producer_with_an_expanding_argument_is_flagged(producer: str) -> None:
    """An argument the shell expands has no size the source can bound: `printf '%s'
    "$input"` writes as many bytes as `$input` holds. This is the defect that shipped."""
    assert _tested(f'{producer} "$input" | grep -q x') == [2]


@pytest.mark.parametrize(
    "producer",
    [
        'printf "%s" "$input"',  # a double-quoted expansion
        "printf %s $input",  # an unquoted expansion
        "echo ${input}",  # a braced expansion
        'echo "$(cat)"',  # a command substitution
        "echo prefix-$input",  # a concatenation carrying one
        'printf "%s" "${input:-}"',  # an expansion with a default
    ],
)
def test_every_expanding_argument_shape_denies_the_exemption(producer: str) -> None:
    assert _tested(f"{producer} | grep -q x") == [2]


@pytest.mark.parametrize(
    "producer",
    [
        "echo hello",
        "printf '%s\\n' done",
        'printf "%s" literal',
        "echo one two three",
        ":",
    ],
)
def test_every_literal_argument_shape_keeps_the_exemption(producer: str) -> None:
    assert _tested(f"{producer} | grep -q x") == []


# --- gap 2: a heredoc body that holds a shell script is scanned ------------------
def _hook_heredoc(inner: str) -> str:
    """A generator script that writes a hook out of a quoted heredoc."""
    return (
        "#!/bin/bash\n"  # 1
        "set -euo pipefail\n"  # 2
        "tee /hooks/x.sh >/dev/null <<'HOOK'\n"  # 3
        "#!/bin/bash\n"  # 4 — the body's first line
        "set -uo pipefail\n"  # 5
        f"{inner}\n"  # 6
        "HOOK\n"
    )


def test_heredoc_shell_script_is_scanned_at_enclosing_line_numbers() -> None:
    """A `|` in a heredoc body is data to the enclosing shell, but a body whose first
    line is a shell shebang is a script the file writes out and something later runs.
    The hit is reported at the ENCLOSING file's line, which is where a fix goes."""
    text = _hook_heredoc('if printf "%s" "$input" | grep -q pat; then :; fi')
    assert mod.violations(text) == [6]


def test_heredoc_body_without_a_shebang_stays_data() -> None:
    """Without a shell shebang the body is documentation or data, never a script."""
    text = (
        "#!/bin/bash\nset -euo pipefail\ncat <<'EOF' >doc.txt\n"
        'if printf "%s" "$input" | grep -q pat; then :; fi\nEOF\n'
    )
    assert mod.violations(text) == []


@pytest.mark.parametrize("shebang", ["#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash"])
def test_each_shell_shebang_opens_the_descent(shebang: str) -> None:
    text = (
        "#!/bin/bash\nset -euo pipefail\ntee /hooks/x.sh >/dev/null <<'HOOK'\n"
        f"{shebang}\nset -uo pipefail\n"
        'if printf "%s" "$input" | grep -q pat; then :; fi\nHOOK\n'
    )
    assert mod.violations(text) == [6]


def test_heredoc_script_gate_and_annotation_are_the_body_s_own() -> None:
    """The written file is its own file: its `set -o pipefail` arms the check even when
    the generator never sets one, and a `# pipefail-grep-ok:` inside the body suppresses.
    """
    no_pipefail_body = (
        "#!/bin/bash\ntee /hooks/x.sh >/dev/null <<'HOOK'\n#!/bin/bash\n"
        'if printf "%s" "$input" | grep -q pat; then :; fi\nHOOK\n'
    )
    assert mod.violations(no_pipefail_body) == []
    annotated = _hook_heredoc(
        "# pipefail-grep-ok: the payload is one line\n"
        'if printf "%s" "$input" | grep -q pat; then :; fi'
    )
    assert mod.violations(annotated) == []


def test_descent_is_one_level_only() -> None:
    """A generator that writes a generator is not a shape this pack has met, so the
    inner-inner body stays data."""
    text = (
        "#!/bin/bash\nset -euo pipefail\ntee a.sh >/dev/null <<'OUTER'\n"
        "#!/bin/bash\nset -uo pipefail\ntee b.sh >/dev/null <<'INNER'\n"
        "#!/bin/bash\nset -uo pipefail\n"
        'if printf "%s" "$x" | grep -q pat; then :; fi\nINNER\nOUTER\n'
    )
    assert mod.violations(text) == []


# --- gap 3: the reader set --------------------------------------------------------
@pytest.mark.parametrize("name", sorted(mod._GREP_COMMANDS))
def test_every_grep_spelling_is_a_reader(name: str) -> None:
    """Driven from `_GREP_COMMANDS`, so dropping a spelling reds."""
    assert _tested(f"producer | {name} -q pat") == [2]


@pytest.mark.parametrize("letter", sorted(mod._GREP_EARLY_LETTERS))
def test_every_early_grep_letter_is_recognized(letter: str) -> None:
    """Driven from `_GREP_EARLY_LETTERS`: `-q` answers at the first matching line, `-l`
    at the first match, `-m N` at the Nth."""
    assert _tested(f"producer | grep -{letter} 1 pat") == [2]
    assert _tested(f"producer | grep -i{letter} 1 pat") == [2], "inside a cluster"


@pytest.mark.parametrize("option", sorted(mod._GREP_EARLY_LONG))
def test_every_early_grep_long_option_is_recognized(option: str) -> None:
    """Driven from `_GREP_EARLY_LONG`, in both the bare and the `=value` spelling."""
    assert _tested(f"producer | grep {option} 1 pat") == [2]
    assert _tested(f"producer | grep {option}=1 pat") == [2]


@pytest.mark.parametrize("option", sorted(mod._HEAD_COUNT_OPTS))
def test_every_head_count_option_takes_a_negative_count(option: str) -> None:
    """Driven from `_HEAD_COUNT_OPTS`. A positive count makes head answer early; a
    NEGATIVE one ("all but the last five") makes it read to the end, so it is safe."""
    assert _tested(f"producer | head {option} 5") == [2]
    assert _tested(f"producer | head {option} -5") == []
    assert _tested(f"producer | head {option}=5") == [2]
    assert _tested(f"producer | head {option}=-5") == []


def test_bare_head_and_obsolete_count_exit_early() -> None:
    assert _tested("producer | head") == [2]
    assert _tested("producer | head -5") == [2]


# Every sed address form the quit detector accepts. Listed here rather than driven from
# a constant because `_SED_ADDRESS` is one regex, not a set of members.
@pytest.mark.parametrize(
    "script",
    [
        "q",  # no address
        "q5",  # a quit with an exit code
        "Q",  # the GNU immediate-quit spelling
        "1q",  # a line address
        "$q",  # the last-line address
        "/marker/q",  # a regex address
        "/a\\/b/q",  # a regex address with an escaped delimiter
        "2,3q",  # an address range
        "$!q",  # a negated address
        "1,/end/q",  # a mixed range
        "-e",  # placeholder replaced below
    ],
)
def test_each_sed_quit_form_exits_early(script: str) -> None:
    if script == "-e":
        assert _tested("producer | sed -n -e '1d' -e '2q'") == [2]
        return
    assert _tested(f"producer | sed '{script}'") == [2]


@pytest.mark.parametrize(
    "script",
    [
        "s/query/x/",  # a `q` inside a substitution regex
        "s/x/quit/",  # a `q` inside a replacement
        "/query/d",  # a `q` inside an address regex
        "y/q/Q/",  # a `q` inside a transliteration
    ],
)
def test_sed_without_a_quit_command_drains(script: str) -> None:
    assert _tested(f"producer | sed '{script}'") == []


@pytest.mark.parametrize(
    "reader",
    [
        "grep -c pat",  # counts every match, so it must read everything
        "grep -L pat",  # names files WITHOUT a match — knowable only at the end
        "grep -v pat",  # inverts; still streams every line
        "grep -o pat",
        "grep -n pat",
        "grep -5 pat",  # a context count, not a `-5` option cluster
        "grep -c -n",
        "tail -1",
        "wc -l",
        "sort",
        "cat",
    ],
)
def test_readers_that_drain_are_not_flagged(reader: str) -> None:
    assert _tested(f"producer | {reader}") == []


def test_option_terminator_ends_the_grep_option_scan() -> None:
    """After `--` every word is a pattern or a path, so a `-q`-shaped one is not an
    option."""
    assert _tested("producer | grep -c -- -q") == []


# --- the precision lever: the status must actually be READ ------------------------
@pytest.mark.parametrize(
    "body",
    [
        "if producer | grep -q pat; then :; fi",
        "if x; then :; elif producer | grep -q pat; then :; fi",
        "while producer | grep -q pat; do :; done",
        "until producer | grep -q pat; do :; done",
        "producer | grep -q pat && do_thing",
        "producer | grep -q pat || do_thing",
        "! producer | grep -q pat",
        "if ! producer | grep -q pat; then :; fi",
    ],
)
def test_every_status_reading_context_arms_the_check(body: str) -> None:
    hits = mod.violations(_PIPEFAIL + body + "\n")
    assert hits == [2], body


@pytest.mark.parametrize(
    "body",
    [
        # nothing branches on the status; only `set -e` could see it, and whether `-e`
        # is in force here is a run-time fact the text cannot state
        "producer | grep -q pat",
        "producer | grep -q pat >/dev/null",
        # the body of an `if`, not its condition
        "if x; then producer | grep -q pat; fi",
        # the body of a `while`, not its condition
        "while x; do producer | grep -q pat; done",
    ],
)
def test_an_untested_pipeline_is_not_flagged(body: str) -> None:
    assert mod.violations(_PIPEFAIL + body + "\n") == []


@pytest.mark.parametrize("discard", sorted(mod._STATUS_DISCARD_WORDS))
def test_a_discarded_status_is_not_read(discard: str) -> None:
    """`cmd || true` runs the same thing next whatever `cmd` returned, so nothing can
    misread the 141. Driven from `_STATUS_DISCARD_WORDS`."""
    assert _flag(f"producer | grep -q pat || {discard}\n") == []


def test_a_real_fallback_still_counts_as_reading_the_status() -> None:
    """`|| handle` changes what runs next, so the 141 IS read."""
    assert _flag("producer | grep -q pat || handle_missing\n") == [2]


def test_only_the_last_stage_answers() -> None:
    """A reader that is not the last stage does not decide the pipeline's status, so
    its early exit is not what an enclosing `if` reads."""
    assert _tested("producer | grep -q pat | cat") == []
    assert _tested("producer | head -1 | wc -l") == []


# --- what the grammar buys: names, strings, redirects -----------------------------
@pytest.mark.parametrize(
    "line",
    [
        # THE remediation: a here-string has no pipe feeding the reader
        'if grep -q pat <<<"$var"; then :; fi',
        'if grep -qF "$name" <<<"$(container ls 2>/dev/null)"; then :; fi',
        # a reader reading a file / plain args, no pipe
        "if grep -q pat file.txt; then :; fi",
        # `||` is a logical-or, not a pipe into the reader
        "if do_thing || grep -q pat file; then :; fi",
        # a clobber redirect `>|` is not a pipe into the reader
        "producer >| out.txt; grep -q pat file && x",
        # a comment or printed hint that merely cites the banned form
        "# never write producer | grep -q pat",
        'echo "avoid producer | grep -q pat"',
    ],
)
def test_does_not_flag_safe_lines(line: str) -> None:
    assert _flag(line + "\n") == []


def test_a_computed_command_word_names_no_program() -> None:
    """`"$GREP" -q` runs whatever the variable holds; guessing that is worse than the
    stated false negative."""
    assert _tested('producer | "$GREP" -q pat') == []
    assert _tested('producer | "$tools/grep" -q pat') == []


@pytest.mark.parametrize(
    "line",
    [
        # a subshell / group producer is not a bounded builtin
        "if (echo a; producer) | grep -q pat; then :; fi",
        "if { producer; } | grep -q pat; then :; fi",
    ],
)
def test_compound_producer_is_not_bounded(line: str) -> None:
    """A producer stage that is not a simple echo/printf/: command does not earn the
    bounded-builtin exemption."""
    assert _flag(line + "\n") == [2]


@pytest.mark.parametrize(
    "line",
    [
        # the exact class that ships the bug
        "if secret_store ls | grep -q github; then :; fi",
        'if container ls 2>/dev/null | grep -qF "$name"; then :; fi',
        # the `|&` (pipe-stderr-too) form still feeds the reader
        "if producer |& grep -q pat; then :; fi",
        # a streaming multi-stage producer feeding the reader
        "if docker info | grep -q runsc; then :; fi",
        "if id -nG | tr ' ' '\\n' | grep -qx docker; then :; fi",
        # a reader whose own path is absolute
        "if producer | /bin/grep -q pat; then :; fi",
    ],
)
def test_flags_streaming_producer_into_early_reader(line: str) -> None:
    assert _flag(line + "\n") == [2]


# --- regression: evasions the per-physical-line scan allowed ---------------------
@pytest.mark.parametrize(
    "body",
    [
        # trailing-pipe continuation: producer on one line, reader on the next
        "producer arg |\n  grep -q pat && ok",
        # backslash continuation before the pipe
        "producer arg \\\n  | grep -q pat && ok",
        # both stages wrapped
        "producer \\\n  arg |\n  grep \\\n  -q pat && ok",
    ],
)
def test_wrapped_pipeline_is_still_one_pipeline(body: str) -> None:
    """A pipeline split across physical lines (trailing `|` or backslash
    continuations) is one pipeline node in the grammar, so wrapping the line
    cannot evade the check. Reported at the reader stage's line."""
    hits = _flag(body + "\n")
    assert len(hits) == 1
    grep_line = next(
        i for i, ln in enumerate((_PIPEFAIL + body).splitlines(), 1) if "grep" in ln
    )
    assert hits == [grep_line]


def test_pipefail_after_the_pipeline_does_not_clear_it() -> None:
    """`set -o pipefail` AFTER the pipeline does not retroactively protect it —
    and must not clear the file either way: the pipe ran without pipefail (its
    own bug class is out of this lint's scope), but any pipeline AFTER the set
    line is still checked."""
    text = "#!/bin/bash\n! early | grep -q pat\nset -o pipefail\n! late | grep -q pat\n"
    assert mod.violations(text) == [4]


def test_pipeline_in_function_body_is_gated_on_pipefail_anywhere() -> None:
    """A function body defined ABOVE the `set -o pipefail` line still runs under
    pipefail (it executes at call time), so its early-reader pipeline is flagged."""
    text = (
        "#!/bin/bash\n"
        "check() {\n"
        "  producer | grep -q pat && return 0\n"
        "}\n"
        "set -euo pipefail\n"
        "check\n"
    )
    assert mod.violations(text) == [3]


def test_pipe_inside_string_or_plain_heredoc_is_data() -> None:
    """A `|` inside a quoted string or a non-script heredoc body is not a pipeline
    node, so quoting the banned form cannot false-positive."""
    text = (
        "#!/bin/bash\nset -euo pipefail\n"
        'msg="if producer | grep -q pat; then :; fi"\n'
        "cat <<'EOF'\nif producer | grep -q pat; then :; fi\nEOF\n"
    )
    assert mod.violations(text) == []


# --- the pipefail gate: the SAME line flips verdict on pipefail presence ---------
def test_non_pipefail_file_is_not_flagged() -> None:
    """Non-vacuity: identical text is flagged only when pipefail is in effect. Without
    it, a pipeline returns the reader's own status, so no SIGPIPE misread is possible."""
    line = "! producer | grep -qF pat\n"
    assert mod.violations("#!/bin/bash\n" + line) == []
    assert mod.violations("#!/bin/bash\nset -euo pipefail\n" + line) == [3]


@pytest.mark.parametrize(
    "setline",
    ["set -o pipefail", "set -euo pipefail", "set -Eeuo pipefail", "set -eo pipefail"],
)
def test_each_pipefail_spelling_enables_the_gate(setline: str) -> None:
    assert mod.violations(f"#!/bin/bash\n{setline}\n! producer | grep -qF x\n") == [3]


def test_disabling_pipefail_still_flags_when_never_enabled() -> None:
    """A `set +o pipefail` (disable) line does NOT count as enabling pipefail."""
    assert (
        mod.violations("#!/bin/bash\nset +o pipefail\n! producer | grep -qF x\n") == []
    )


# --- sourced bash libraries inherit strict mode: the credential-bug class ---------
def test_sourced_bash_lib_is_pipefail_scoped() -> None:
    """A lib with no shebang that declares `# shellcheck shell=bash` runs under its
    strict-mode callers' pipefail, so its tested early-reader pipeline is flagged even
    with no in-file `set -o pipefail` — this is what catches a sourced lib's teardown
    credential check."""
    lib = "# shellcheck shell=bash\n! secret_store ls | grep -qiE github\n"
    assert mod.violations(lib) == [2]
    # A shebang'd script with the same declaration but no pipefail is NOT scoped.
    script = "#!/usr/bin/env bash\n# shellcheck shell=bash\n! secret_store ls | grep -qiE x\n"
    assert mod.violations(script) == []


# --- the opt-out annotation -------------------------------------------------------
def test_same_line_annotation_suppresses() -> None:
    assert _flag("! producer | grep -qF x  # pipefail-grep-ok: bounded\n") == []


def test_preceding_line_annotation_suppresses() -> None:
    assert _flag("# pipefail-grep-ok: bounded\n! producer | grep -qF x\n") == []


def test_annotation_on_the_pipeline_s_first_line_covers_a_wrapped_pipeline() -> None:
    """The window is the whole pipeline, so a reason written where the pipeline STARTS
    still covers a reader two lines below it."""
    assert (
        _flag("producer |  # pipefail-grep-ok: one line of output\n  grep -q x\n") == []
    )


def test_annotation_two_lines_above_does_not_count() -> None:
    assert _flag("# pipefail-grep-ok: stale\nnoop\n! producer | grep -qF x\n") == [4]


def test_allow_bare_substring_does_not_suppress() -> None:
    # `pipefail-grep-ok` as a bare substring — in a URL path or a quoted arg, not an
    # actual `# pipefail-grep-ok:` comment — must NOT opt out.
    assert _flag("! curl https://cdn/pipefail-grep-ok/x | grep -qF foo\n") == [2]
    assert _flag('! emit "pipefail-grep-ok" | grep -qF foo\n') == [2]
    # A `#` comment without a colon-and-reason states nothing and does not suppress.
    assert _flag("! producer | grep -qF x  # pipefail-grep-ok\n") == [2]
    assert _flag("! producer | grep -qF x  # pipefail-grep-ok:\n") == [2]


def test_reasoned_allow_still_suppresses() -> None:
    # Green control: a genuine reasoned opt-out (same line or preceding line) still
    # suppresses after the bare-substring hole is closed.
    assert _flag("! producer | grep -qF x  # pipefail-grep-ok: bounded output\n") == []
    assert _flag("# pipefail-grep-ok: bounded output\n! producer | grep -qF x\n") == []


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message; here we pin only that main() emits THIS message on a real hit."""
    bad = tmp_path / "bad.bash"
    bad.write_text(
        "set -euo pipefail\nif container ls | grep -qF x; then :; fi\n",
        encoding="utf-8",
    )
    assert mod.main([str(bad)]) == 1
    assert "reader that stops early under `set -o pipefail`" in capsys.readouterr().err


def _run_module(*paths: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_pipefail_grep_pipe", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_module_rejects_and_accepts(tmp_path: Path) -> None:
    bad = tmp_path / "bad.bash"
    bad.write_text(
        "set -euo pipefail\nif container ls | grep -qF x; then :; fi\n",
        encoding="utf-8",
    )
    good = tmp_path / "good.bash"
    good.write_text(
        'set -euo pipefail\nif grep -qF x <<<"$(container ls)"; then :; fi\n',
        encoding="utf-8",
    )
    assert _run_module(str(bad)).returncode == 1
    ok = _run_module(str(good))
    assert ok.returncode == 0
    assert ok.stderr == ""


def _is_shell(path: Path) -> bool:
    """Match the hook's `types: [shell]` selection: a .bash/.sh file, or an
    extensionless script whose shebang names a shell."""
    if path.suffix in (".bash", ".sh"):
        return True
    if path.suffix:
        return False
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except (OSError, IndexError):
        return False
    return bool(first) and first[0].startswith("#!") and "sh" in first[0]


def tracked_shell_paths(root: Path) -> list[Path]:
    """Every tracked file under ROOT that the hook's `types: [shell]` selection picks."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    return [
        root / rel
        for rel in tracked
        if rel and (root / rel).is_file() and _is_shell(root / rel)
    ]


def test_own_shell_surface_is_clean() -> None:
    """Every tracked shell file in this repo must pass — a new tested early-reader
    pipeline under pipefail here turns this red, proving the check is wired to real
    sources."""
    offenders = []
    for path in tracked_shell_paths(REPO_ROOT):
        hits = mod.violations(path.read_text(encoding="utf-8", errors="replace"))
        offenders += [f"{path.relative_to(REPO_ROOT)}:{n}" for n in hits]
    assert offenders == [], f"early-reader pipelines in own shell surface: {offenders}"

"""Tests for ci_truth_serum/check_echo_fallback.py — the lint that bans `|| echo` /
`|| printf` fallbacks which convert a failure into a benign parseable string
(inside command substitutions, and as unaborted bare statements).

Drives ``violations()`` for the rules and ``main()`` for the argv/exit-code
contract. The detector reads the bash grammar, so the cases below pin the
STRUCTURAL verdicts a text scan gets wrong: the idiom quoted inside a message
string, written in a heredoc body, or captured only to be handed to another
command is not executed code that fakes a value.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_echo_fallback.py", "check_echo_fallback")


# ── flagged: fallback inside a substitution ──────────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'v=$(git describe || echo "error")\n',
        'diff=$(git diff "$a" "$b" || echo "Unable to get diff")\n',
        'v=$(cmd || printf "0.0.0")\n',
        "v=`cmd || echo fallback`\n",
        # exit inside a substitution only exits the subshell — still flagged
        'v=$(cmd || echo "x"; exit 1)\n',
        # multi-line substitution joined and flagged at its first line
        'v=$(curl -s url \\\n  || echo "000")\n',
    ],
)
def test_fallback_inside_substitution_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── flagged: bare statement that narrates but does not abort ─────────────
@pytest.mark.parametrize(
    "src",
    [
        'do_deploy || echo "deploy failed"\n',
        'make test || printf "tests failed"\n',
    ],
)
def test_bare_unaborted_fallback_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── legitimate corpus: ZERO findings ─────────────────────────────────────
@pytest.mark.parametrize(
    "src",
    [
        # message to stderr — diagnostics, not a value
        'cmd || echo "cmd failed" >&2\n',
        'v=$(cmd || echo "warn" >&2)\n',
        # narrate AND abort — a real recovery
        'cmd || { echo "failed" >&2; exit 1; }\n',
        'cmd || { echo "failed"; exit 1; }\n',
        'find_config || { echo "no config"; return 1; }\n',
        # no fallback at all
        "v=$(git describe)\n",
        "cmd || exit 1\n",
        "a || b\n",
        # message-printing line quoting the idiom
        'echo "usage: v=$(cmd || echo fallback)"\n',
        # comment quoting the idiom
        '# bad: v=$(cmd || echo "error")\n',
    ],
)
def test_legitimate_corpus_yields_zero_findings(src: str) -> None:
    assert mod.violations(src) == []


# ── opt-out ──────────────────────────────────────────────────────────────
def test_opt_out_same_line() -> None:
    src = 'code=$(curl -w "%{http_code}" url || echo "000") # echo-fallback-ok: 000 is the documented curl-failure sentinel\n'
    assert mod.violations(src) == []


def test_opt_out_line_above() -> None:
    src = '# echo-fallback-ok: sentinel the caller branches on\ncode=$(cmd || echo "000")\n'
    assert mod.violations(src) == []


def test_multiple_violations_report_each_line() -> None:
    src = 'a=$(x || echo "1")\n:\nb || echo "2"\n'
    assert mod.violations(src) == [1, 3]


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_path_line_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text('v=$(cmd || echo "error")\n', encoding="utf-8")
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('cmd || { echo "failed" >&2; exit 1; }\n', encoding="utf-8")
    assert mod.main([str(p)]) == 0


def test_main_reports_pathological_input_loudly(tmp_path, capsys) -> None:
    """An input the grammar refuses to parse fails the check, never passes it: a
    silent skip would false-green exactly the file an adversary controls."""
    p = tmp_path / "s.sh"
    p.write_text("cmd " + "| cat " * 3000 + "\n", encoding="utf-8")
    assert mod.main([str(p)]) == 1
    assert "pipe bytes" in capsys.readouterr().err


# ── a printing command's ARGUMENT is text, whatever the command is called ──
@pytest.mark.parametrize(
    "printer", ["gb_warn", "log_warn", "notice", "my::report", "echo"]
)
def test_idiom_quoted_in_a_message_string_is_not_code(printer: str) -> None:
    """A `string` argument holds no commands, so the printing command needs no
    enumeration — which is the point, since a project's own logger names are
    unenumerable."""
    assert mod.violations(f'{printer} "cmd || echo failed fakes a value"\n') == []


def test_the_same_idiom_outside_the_message_string_still_fires() -> None:
    """Positive marker for the case above: the string is what excuses it, not the
    words inside it."""
    assert mod.violations('gb_warn "msg"\ncmd || echo "failed"\n') == [2]


# ── a heredoc body is data ────────────────────────────────────────────────
@pytest.mark.parametrize("delimiter", ["'EOF'", '"EOF"', "\\EOF"])
def test_idiom_in_a_quoted_heredoc_body_is_not_code(delimiter: str) -> None:
    """A quoted delimiter makes the body literal data — bash expands nothing in
    it, so the text is written to the file as written."""
    src = f'cat <<{delimiter} > doc.txt\nv=$(cmd || echo "error")\nEOF\n'
    assert mod.violations(src) == []


def test_unquoted_heredoc_body_expands_so_the_fallback_still_fires() -> None:
    """Positive marker AND a verdict the grammar earns: with an UNQUOTED
    delimiter bash really runs the substitution and the fallback text lands in
    the file, so the body is code."""
    src = 'cat <<EOF > doc.txt\nv=$(cmd || echo "error")\nEOF\n'
    assert mod.violations(src) == [2]


def test_the_same_idiom_after_the_heredoc_still_fires() -> None:
    """Positive marker: the heredoc excuses only its own body."""
    src = 'cat <<\'EOF\' > doc.txt\nv=$(cmd || echo "error")\nEOF\nv=$(cmd || echo "error")\n'
    assert mod.violations(src) == [4]


# ── a captured value in another command's argv is that command's business ──
@pytest.mark.parametrize(
    "src",
    [
        # argument of a printing command, whatever it is named
        'echo "usage: v=$(cmd || echo fallback)"\n',
        'gb_warn "current: $(cmd || echo unknown)"\n',
        # a process substitution named as an argument, read by another program
        "diff <(cmd || echo x) file\n",
    ],
)
def test_capture_that_becomes_an_argument_is_not_flagged(src: str) -> None:
    assert mod.violations(src) == []


@pytest.mark.parametrize(
    "src",
    [
        # kept by the script
        'v="$(cmd || echo x)"\n',
        "export V=$(cmd || echo x)\n",
        # branched on by the script
        "if [[ $(cmd || echo yes) == yes ]]; then :; fi\n",
        # run as a command name
        "$(cmd || echo x) --flag\n",
        # funnelled as data: the shell's own read captures it, and a here-string
        # is a parser's stdin
        "read -r v < <(cmd || echo x)\n",
        'jq . <<< "$(cmd || echo {})"\n',
        # only the OUTER capture is the script's own value; the inner one is an
        # argument of the echo that prints it
        'v=$(cmd || echo "$(inner || echo deep)")\n',
    ],
)
def test_capture_the_script_itself_takes_is_flagged(src: str) -> None:
    assert mod.violations(src) == [1]


# ── stderr redirects and aborts, read off the grammar ─────────────────────
@pytest.mark.parametrize(
    "src",
    [
        'cmd || echo "x" 1>&2\n',
        'cmd || echo "x" >& 2\n',
        'cmd || echo "x" > /dev/stderr\n',
        'cmd || echo "x" >> /dev/stderr\n',
        # narrated on stderr, then the statement continues
        'cmd || echo "x" >&2 || true\n',
        # the redirect sits on the enclosing block, so it still applies
        '{ cmd || echo "x"; } >&2\n',
        '( cmd || echo "x" ) >&2\n',
    ],
)
def test_stderr_spellings_are_not_flagged(src: str) -> None:
    assert mod.violations(src) == []


@pytest.mark.parametrize(
    "src",
    [
        'cmd || echo "x" && exit 1\n',
        'cmd || echo "x"; exit 1\n',
        "f() { cmd || echo x; return 1; }\n",
    ],
)
def test_bare_form_that_aborts_is_not_flagged(src: str) -> None:
    assert mod.violations(src) == []


def test_abort_on_a_later_line_does_not_excuse_the_fallback() -> None:
    """The abort must belong to the fallback's own statement or line — a bare
    `exit` further down the file recovers nothing at the failure site."""
    assert mod.violations('cmd || echo "x"\nexit 1\n') == [1]

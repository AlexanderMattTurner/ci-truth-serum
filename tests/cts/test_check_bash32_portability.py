"""Tests for ci_truth_serum/check_bash32_portability.py — the lint that flags GNU-only
flags and bash-4+ syntax in scripts that must run on macOS.

Drives `violations()` for the grammar rules and `main()` for the argv contract.
The two probes every shell lint in this pack must survive — the idiom inside a
logger's message string, and inside a heredoc body — have their own cases below.
"""

import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import HOOKS_DIR, load_hook

mod = load_hook("check_bash32_portability.py", "check_bash32_portability")


def _labels(src: str, rules=mod.RULE_SETS) -> list[str]:
    """The construct each finding in SRC names, as the message's first clause."""
    return [
        message.split(" is GNU-only")[0].split(" needs bash 4")[0]
        for _, message in mod.violations(src, rules)
    ]


# ── bsd: each GNU-only row, with every spelling that runs the GNU flag ───
GNU_BAD = {
    "`tail`/`head` -z (--zero-terminated)": [
        "tail -z file",
        "head -zn 5",
        "find . | tail --zero-terminated",
    ],
    "`grep -P` (--perl-regexp)": [
        "grep -P 'x'",
        "grep -oP 'x'",
        "grep --perl-regexp x",
        "'grep' '-P' x",
        "/usr/bin/grep -P x",
        "command grep -P x f",
        "env LC_ALL=C grep -P x f",
        "out=$(grep -P x f)",
    ],
    "`find -printf`": ["find . -printf '%p\\n'"],
    "`date -d` (--date)": ["date -d '2 days ago'", "date --date=@123", "date -d@123"],
    "`sort -V` (--version-sort)": [
        "sort -V",
        "printf '%s\\n' x | sort -rV",
        "sort -Vu",
        "sort --version-sort",
        "command sort -V",
    ],
    "`sha256sum -c` with the list on stdin": [
        'sha256sum --check --status <<<"$want  $file"',
        'sha256sum --check <<<"$want  $file"',
        'printf "%s  %s" "$w" "$f" | sha256sum -c',
    ],
}


@pytest.mark.parametrize(
    "label, src",
    [(label, src) for label, srcs in GNU_BAD.items() for src in srcs],
)
def test_a_gnu_only_flag_is_flagged(label: str, src: str) -> None:
    assert _labels(f"{src}\n") == [label]


@pytest.mark.parametrize(
    "src",
    [
        "cut -dz -f1",  # z is cut's delimiter value
        "grep -E 'a|b'",
        "find . -print0",
        "date +%s",
        "sort -u",
        "sort -rn nums",
        "command -v sort",  # a lookup, so sort never runs
        "command -v grep",
        "env --help",
        'sha256sum "$file"',
        'sha256sum -c "$sums"',  # the list is a file operand
        'printf "%s  %s" "$w" "$f" | sha256sum -c -',  # `-` names stdin
        "docker exec box tail -zn +2 f",  # runs in the container, not on the host
        "gdate -d @0",  # GNU coreutils under its macOS name
        "grep -- -P file",  # after `--` the word is the pattern
        "grep -e -P file",  # the value of -e is the pattern
        "grep -ie -P file",
        "grep -f -P file",
        "grep --regexp -P file",
        "date -- -d",
    ],
)
def test_a_portable_command_passes(src: str) -> None:
    assert mod.violations(f"{src}\n", (mod.BSD,)) == []


@pytest.mark.parametrize(
    "src",
    [
        "grep -P -- pattern file",  # the flag sits before `--`
        "grep -e a -P file",  # the value skip ends after one word
        "grep -f pats -P file",
        "grep -Pe a file",
        "grep --regexp a --perl-regexp file",
    ],
)
def test_a_flag_beside_data_words_is_still_found(src: str) -> None:
    assert len(mod.violations(f"{src}\n", (mod.BSD,))) == 1


# ── bash32: each bash-4+ construct ───────────────────────────────────────
BASH4_BAD = {
    "`declare -A`": [
        "declare -A m",
        "local -gA m=()",
        "typeset -A x",
        "! declare -A m",
    ],
    "`declare -n`": [
        'local -n _out="$1"',
        "declare -n ref=$name",
        'local -gn r="$1"',
        "typeset -n v=x",
    ],
    "`mapfile`": ["mapfile -t a < <(x)"],
    "`readarray`": ["readarray -t a <f"],
    "`${v,,}`/`${v^^}` case conversion": [
        'x="${name,,}"',
        "y=${VAR^^}",
        "z=${arr[1],}",
        "w=${v^}",
    ],
    "`{fd}` descriptor allocation": [
        '{ exec {fd}<"$f"; } 2>/dev/null || return 0',
        'if { exec {_fd}>"$_lock"; } 2>/dev/null; then :; fi',
        "exec {fd}<&-",
    ],
    "`${a[-1]}` negative index": [
        'dest="${args[-1]}"',
        'src="${args[-2]}"',
        'n="${#lines[-1]}"',
        'x="${arr[ -1 ]}"',
    ],
}


@pytest.mark.parametrize(
    "label, src",
    [(label, src) for label, srcs in BASH4_BAD.items() for src in srcs],
)
def test_a_bash4_construct_is_flagged(label: str, src: str) -> None:
    assert _labels(f"{src}\n") == [label]


@pytest.mark.parametrize(
    "src",
    [
        'd="${args[i-1]}"',  # arithmetic inside the subscript
        'e="${args[@]:-1}"',  # a default value, not an index
        'f="${#args[@]}"',
        'g="${*: -1}"',  # substring on the positional parameters
        "local -a arr",
        "grep -A2 foo",  # -A on another command
        "local -i n=1",  # `n` names the variable here
        "local -a names",
        'grep -n "$pat" f',
        'x="${json:+, }"',  # a literal comma in the alternative value
        'd="${e%%:*}"',
        'p="${prefix//?/ }"',
        "a=${x/^/y}",  # `^` is the pattern, not an operator
        "c=${x%,}",
        'read -ra w <<<"$s"',
        'exec >"$log" 2>&1',
        'tr -d "\\r\\n" <&"$fd"',  # reading an fd already held
        "exec ${fd}>&-",  # closing an fd held in a variable
        "for x in {a,b}; do :; done",
        "export -n FOO",  # bash 3.2 has export -n
    ],
)
def test_bash32_safe_syntax_passes(src: str) -> None:
    assert mod.violations(f"{src}\n") == []


# ── the version-guard exemption ──────────────────────────────────────────
@pytest.mark.parametrize(
    "guarded, src",
    [
        (False, "# BASH_VERSINFO is what the guard reads\n"),
        (False, 'die "this needs BASH_VERSINFO 5 or newer"\n'),
        (False, 'ver="$("$candidate" -c \'echo ${BASH_VERSINFO[0]}\')"\n'),
        (True, "[[ ${BASH_VERSINFO[0]} -ge 5 ]] || exit 1\n"),
        (True, 'major="${BASH_VERSINFO[0]}"  # the running major\n'),
        (True, "if ((${_M:-BASH_VERSINFO[0]} < 5)); then :; fi\n"),
        (True, "if ((BASH_VERSINFO[0] >= 4)); then :; fi\n"),
    ],
)
def test_only_a_read_of_the_version_counts_as_a_guard(guarded: bool, src: str) -> None:
    """A comment, a message, or a probe of ANOTHER bash names the variable without
    reading it, and must not exempt the file."""
    assert mod.checks_bash_version(mod.parse(src)) is guarded


def test_a_guarded_file_skips_the_bash32_set_but_not_the_bsd_set() -> None:
    """A newer bash still runs BSD tools, so the guard exempts only the syntax."""
    src = "((BASH_VERSINFO[0] >= 4)) || exit 1\ndeclare -A m\ngrep -P x f\n"
    assert [line for line, _ in mod.violations(src)] == [3]


# ── text that no shell runs ──────────────────────────────────────────────
@pytest.mark.parametrize(
    "name, src",
    [
        # The two probes: text a command prints, and data written to a file.
        (
            "a logger's message string",
            'gb_warn "use grep -P, mapfile or declare -A m here"\n',
        ),
        (
            "a heredoc body",
            "cat <<'EOF' >doc.txt\ngrep -P x f\ndeclare -A m\nmapfile -t a\n"
            "x=${v,,} ${a[-1]}\nexec {fd}<f\nEOF\n",
        ),
        ("a comment", "# grep -P x and declare -A m both fail on macOS\n"),
        ("an echoed instruction", "echo date -d @0 sort -V\n"),
        ("a single-quoted string", "x='${v,,} ${a[-1]}'\n"),
    ],
)
def test_text_no_shell_runs_passes(name: str, src: str) -> None:
    assert mod.violations(src) == [], name


def test_an_expansion_inside_a_double_quoted_message_still_runs() -> None:
    """bash expands `${v,,}` inside double quotes, so 3.2 aborts on the message."""
    assert [line for line, _ in mod.violations('gb_warn "got ${v,,}"\n')] == [1]


def test_an_unquoted_heredoc_runs_its_substitutions() -> None:
    """bash expands an unquoted heredoc body, so `$(…)` in it runs on the host."""
    src = "cat <<EOF >f\n$(grep -P x f)\nEOF\n"
    assert [line for line, _ in mod.violations(src)] == [2]


# ── rule selection and non-vacuity ───────────────────────────────────────
_EVERY_CONSTRUCT = (
    "\n".join(
        [srcs[0] for srcs in GNU_BAD.values()]
        + [srcs[0] for srcs in BASH4_BAD.values()]
    )
    + "\n"
)


def test_every_rule_contributes_a_finding() -> None:
    """Each row fires on its own sample, so no row is dead weight."""
    assert sorted(_labels(_EVERY_CONSTRUCT)) == sorted([*GNU_BAD, *BASH4_BAD])
    assert {label for label, _, _, _ in mod.GNU_ROWS} == set(GNU_BAD)


@pytest.mark.parametrize(
    "only, wanted", [(mod.BSD, set(GNU_BAD)), (mod.BASH32, set(BASH4_BAD))]
)
def test_one_rule_set_reports_only_its_own_rows(only: str, wanted: set) -> None:
    assert set(_labels(_EVERY_CONSTRUCT, (only,))) == wanted


# ── the opt-out ──────────────────────────────────────────────────────────
def test_an_annotated_line_passes() -> None:
    src = "grep -P x f # bash32-portability-ok: runs only in the Linux image\n"
    assert mod.violations(src) == []


def test_the_annotation_works_on_the_line_above() -> None:
    src = "# bash32-portability-ok: runs only in the Linux image\ndeclare -A m\n"
    assert mod.violations(src) == []


def test_an_annotation_with_no_reason_does_not_suppress() -> None:
    assert [
        line for line, _ in mod.violations("grep -P x f # bash32-portability-ok\n")
    ] == [1]


def test_a_continued_command_takes_the_annotation_on_any_of_its_lines() -> None:
    src = "grep -P \\\n  x f # bash32-portability-ok: Linux-only helper\n"
    assert mod.violations(src) == []


# ── the argv/exit-code contract ──────────────────────────────────────────
def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOOKS_DIR / "check_bash32_portability.py"), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_main_reports_the_path_line_and_opt_out(tmp_path: Path) -> None:
    script = tmp_path / "wrapper.sh"
    script.write_text("#!/bin/bash\nmapfile -t a <f\n", encoding="utf-8")
    result = _run(str(script))
    assert result.returncode == 1
    assert f"{script}:2:" in result.stderr
    assert "bash32-portability-ok" in result.stderr


def test_main_with_only_bsd_skips_the_bash32_set(tmp_path: Path) -> None:
    script = tmp_path / "wrapper.sh"
    script.write_text("#!/bin/bash\nmapfile -t a <f\n", encoding="utf-8")
    assert _run("--only", "bsd", str(script)).returncode == 0


def test_main_exits_zero_on_a_clean_file(tmp_path: Path) -> None:
    script = tmp_path / "wrapper.sh"
    script.write_text("#!/bin/bash\ngrep -E x f\n", encoding="utf-8")
    assert _run(str(script)).returncode == 0


def test_main_refuses_an_empty_file_list() -> None:
    result = _run("--only", "bsd")
    assert result.returncode == 2
    assert "no files to scan" in result.stderr


def test_a_file_the_grammar_refuses_fails_loudly(tmp_path: Path) -> None:
    script = tmp_path / "huge.sh"
    script.write_text("cmd |" * 3000, encoding="utf-8")
    result = _run(str(script))
    assert result.returncode == 1
    assert "pipe bytes" in result.stderr


# ── crash resistance ─────────────────────────────────────────────────────
_PIECES = st.sampled_from(
    [
        "grep",
        "-P",
        "declare",
        "-A",
        "${v,,}",
        "${a[-1]}",
        "exec",
        "{fd}<",
        "f",
        "'",
        '"',
        "$(",
        ")",
        "<<'EOF'\n",
        "EOF\n",
        "\n",
        " ",
        "|",
        "#",
        "\\\n",
    ]
)


@given(st.lists(_PIECES, max_size=30).map("".join))
def test_violations_never_crashes(src: str) -> None:
    lines = len(src.split("\n"))
    for line, message in mod.violations(src):
        assert 1 <= line <= lines and message

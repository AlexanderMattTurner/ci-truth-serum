"""Tests for ci_truth_serum/check_argument_exit_swallow.py — the pre-commit lint
that bans a command substitution passed as an ARGUMENT to a locally defined
function (`my_helper "$(risky)"`) in a file that runs under `set -e`.

Drives `violations()` directly so each rule is asserted in isolation, and drives
`main()` over a tmp tree for the parts that need a real path (what a file
sources, and the file:line report).
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_argument_exit_swallow.py"
mod = load_hook("check_argument_exit_swallow.py", "check_argument_exit_swallow")

# The shortest file that satisfies both gates: errexit on, and a function this
# file defines. Every fires/clean case below is written against it.
PRELUDE = "set -e\nmy_helper() { :; }\n"


def _lines(*body: str) -> str:
    return PRELUDE + "\n".join(body) + "\n"


# ── the core shape: a substitution as an argument to a local function ────────
@pytest.mark.parametrize(
    "line",
    [
        # the canonical bug
        'my_helper "$(risky_listing)"',
        # unquoted substitution: the same swallow, one word-split later
        "my_helper $(risky_listing)",
        # the substitution is part of a larger word
        'my_helper "prefix-$(risky_listing)"',
        # inside a parameter expansion's default
        'my_helper "${override:-$(risky_listing)}"',
        # backtick spelling of the same substitution
        "my_helper `risky_listing`",
        # not the first argument
        'my_helper --flag "$(risky_listing)"',
        # the call is a condition, which does not observe the inner status either
        'if my_helper "$(risky_listing)"; then :; fi',
        # the call is a pipeline stage
        'my_helper "$(risky_listing)" | cat',
    ],
)
def test_fires_on_argument_substitution(line: str) -> None:
    assert mod.violations(_lines(line)) == [3]


@pytest.mark.parametrize(
    "definition",
    [
        "my_helper() { :; }",
        "function my_helper { :; }",
        "function my_helper() { :; }",
    ],
)
def test_both_definition_forms_supply_the_callee(definition: str) -> None:
    text = f'set -e\n{definition}\nmy_helper "$(risky)"\n'
    assert mod.violations(text) == [3]


# ── scope: only a function this tree defines ─────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        # the idiomatic bulk of every match: a printer and a matcher, neither of
        # which is a function this file defines
        'echo "$(date)"',
        'grep -q "$(marker)" file',
        'printf "%s\\n" "$(git rev-parse HEAD)"',
        # an external program that happens to share no name with a definition
        'curl -fsSL "$(build_url)"',
    ],
)
def test_non_local_callees_are_clean(line: str) -> None:
    assert mod.violations(_lines(line)) == []


def test_a_computed_callee_name_cannot_match_a_definition() -> None:
    # `"$tool" "$(risky)"` runs whatever $tool holds; no computed name can be
    # matched against a definition, so this is out of scope by construction.
    assert mod.violations(_lines('"$my_helper" "$(risky)"')) == []


def test_a_substitution_in_the_command_name_is_not_an_argument() -> None:
    # `"$(get_tool)" --flag` computes the PROGRAM. That is a different defect
    # (an unchecked program lookup), not a swallowed argument.
    assert mod.violations(_lines('"$(get_tool)" --flag')) == []


# ── SC2155's own shape, and the fix, are both out of scope ───────────────────
@pytest.mark.parametrize(
    "line",
    [
        # SC2155 proper — shellcheck already reports this one
        'local x="$(risky)"',
        'declare -r x="$(risky)"',
        'export X="$(risky)"',
        # the recommended fix: a plain assignment DOES report the substitution's
        # status, so it must never be flagged
        'listing="$(risky)"',
    ],
)
def test_assignments_are_out_of_scope(line: str) -> None:
    assert mod.violations(_lines(line)) == []


def test_the_recommended_fix_is_clean() -> None:
    text = _lines('listing="$(risky_listing)"', 'my_helper "$listing"')
    assert mod.violations(text) == []


# ── the errexit gate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "prologue",
    [
        "set -e",
        "set -eu",
        "set -euo pipefail",
        "set -o errexit",
        "#!/bin/bash -e",
        "#!/usr/bin/env bash -eu",
    ],
)
def test_errexit_spellings_open_the_gate(prologue: str) -> None:
    text = f'{prologue}\nmy_helper() {{ :; }}\nmy_helper "$(risky)"\n'
    assert mod.violations(text) == [3]


@pytest.mark.parametrize(
    "prologue",
    [
        # no errexit at all: the script never promised a failure would stop it
        "#!/usr/bin/env bash",
        # `-u` and `-o pipefail` are other options; neither is errexit
        "set -u",
        "set -o pipefail",
        # `+e` turns errexit OFF
        "set +e",
    ],
)
def test_without_errexit_nothing_fires(prologue: str) -> None:
    text = f'{prologue}\nmy_helper() {{ :; }}\nmy_helper "$(risky)"\n'
    assert mod.violations(text) == []


def test_errexit_set_after_the_call_still_opens_the_gate() -> None:
    # The gate is a property of the FILE, not of a line order: a `set -e` further
    # down still says this script means a failure to stop it.
    text = 'my_helper() { :; }\nmy_helper "$(risky)"\nset -e\n'
    assert mod.violations(text) == [2]


def test_a_later_set_plus_e_does_not_close_the_gate() -> None:
    # Documented, deliberate: errexit spans are not tracked, and the conservative
    # direction for a fail-open detector is to still report.
    text = _lines("set +e", 'my_helper "$(risky)"')
    assert mod.violations(text) == [4]


# ── verdicts only the grammar can reach ──────────────────────────────────────
@pytest.mark.parametrize(
    "name, text, expected",
    [
        (
            "the call quoted inside a printed message is not a command",
            _lines('echo "bad: my_helper \\"$(risky)\\""'),
            [],
        ),
        (
            "a heredoc body is data written to a file, not code",
            _lines("cat <<'EOF' > doc.txt", 'my_helper "$(risky)"', "EOF"),
            [],
        ),
        (
            "a comment documenting the bug is not code",
            _lines('# my_helper "$(risky)" is the bug this bans'),
            [],
        ),
        (
            "a call inside the body of another function still counts",
            _lines("wrapper() {", '  my_helper "$(risky)"', "}"),
            [4],
        ),
        (
            "the inner substitution is reported, the outer is not an argument",
            _lines('x="$(my_helper "$(risky)")"'),
            [3],
        ),
        (
            "a multi-line call anchors on the substitution's own line",
            _lines("my_helper \\", '  "$(risky)"'),
            [4],
        ),
        (
            "two substitutions in one call are one finding per line",
            _lines('my_helper "$(a)" "$(b)"'),
            [3],
        ),
    ],
)
def test_grammar_reachable_verdicts(name: str, text: str, expected: list[int]) -> None:
    assert mod.violations(text) == expected, name


# ── the injected callee set (what a sourced file contributes) ────────────────
def test_injected_functions_supply_the_callee() -> None:
    # The file defines nothing; `lib_helper` comes from the sourced-file set.
    text = 'set -e\nlib_helper "$(risky)"\n'
    assert mod.violations(text) == []
    assert mod.violations(text, {"lib_helper"}) == [2]


# ── source resolution ────────────────────────────────────────────────────────
def test_source_targets_reads_both_spellings() -> None:
    text = 'source lib/a.sh\n. lib/b.sh\nsource "${DIR}/c.sh"\n'
    assert mod.source_targets(text) == ["lib/a.sh", "lib/b.sh", "${DIR}/c.sh"]


def test_resolve_source_prefers_a_path_relative_to_the_sourcing_file() -> None:
    tracked = ["bin/lib.sh", "other/lib.sh"]
    assert mod.resolve_source("lib.sh", "bin/run.sh", tracked) == "bin/lib.sh"


def test_resolve_source_falls_back_to_a_unique_basename() -> None:
    # `source "${SCRIPT_DIR}/lib.sh"` names a path only the shell can build; the
    # trailing literal name still identifies one tracked file.
    tracked = ["bin/lib.sh"]
    assert mod.resolve_source("${SCRIPT_DIR}/lib.sh", "bin/run.sh", tracked) == (
        "bin/lib.sh"
    )


def test_resolve_source_prefers_a_sibling_of_the_sourcing_file(
    tmp_path: Path,
) -> None:
    # `source "${SCRIPT_DIR}/lib.sh"` means the sibling far more often than it
    # means some other tracked `lib.sh`, so the sibling wins.
    (tmp_path / "lib.sh").write_text("f() { :; }\n", encoding="utf-8")
    origin = str(tmp_path / "run.sh")
    assert mod.resolve_source("${SCRIPT_DIR}/lib.sh", origin, ["elsewhere/lib.sh"]) == (
        str(tmp_path / "lib.sh")
    )


def test_resolve_source_refuses_an_ambiguous_basename() -> None:
    tracked = ["a/lib.sh", "b/lib.sh"]
    assert mod.resolve_source("${DIR}/lib.sh", "c/run.sh", tracked) is None


def test_resolve_source_refuses_a_fully_computed_name() -> None:
    assert mod.resolve_source("${LIB}", "bin/run.sh", ["bin/lib.sh"]) is None


def test_resolve_source_refuses_a_literal_path_that_matches_no_file() -> None:
    # A literal path names one file. When it matches none, the name fallback
    # must NOT run: it would attribute `other/lib.sh`'s functions to a script
    # that sources something else.
    assert mod.resolve_source("vendor/lib.sh", "bin/run.sh", ["other/lib.sh"]) is None


def test_sourced_functions_is_transitive_and_terminates_on_a_cycle(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.sh").write_text("source b.sh\nfrom_a() { :; }\n", encoding="utf-8")
    (tmp_path / "b.sh").write_text("source a.sh\nfrom_b() { :; }\n", encoding="utf-8")
    entry = tmp_path / "run.sh"
    entry.write_text("source a.sh\n", encoding="utf-8")
    names = mod.sourced_functions(
        entry.read_text(encoding="utf-8"), str(entry), [str(tmp_path / "a.sh")]
    )
    assert names == {"from_a", "from_b"}


# ── opt-out annotation (reason REQUIRED) ─────────────────────────────────────
def test_same_line_annotation_with_reason_suppresses() -> None:
    line = 'my_helper "$(risky)"  # allow-argument-exit: empty input is the no-op'
    assert mod.violations(_lines(line)) == []


def test_preceding_line_annotation_with_reason_suppresses() -> None:
    text = _lines("# allow-argument-exit: empty input is the no-op", 'my_helper "$(a)"')
    assert mod.violations(text) == []


@pytest.mark.parametrize(
    "annotation",
    [
        "# allow-argument-exit",  # bare, no colon
        "# allow-argument-exit:",  # colon, no reason
        "# allow-argument-exit:   ",  # colon, only whitespace
    ],
)
def test_reasonless_annotation_does_not_suppress(annotation: str) -> None:
    assert mod.violations(_lines(f'my_helper "$(a)"  {annotation}')) == [3]


def test_annotation_two_lines_above_does_not_suppress() -> None:
    text = _lines("# allow-argument-exit: reason", "x=1", 'my_helper "$(a)"')
    assert mod.violations(text) == [5]


# ── main() wiring: exit code, file:line message, sourced callees ─────────────
def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.sh"
    bad.write_text(_lines('my_helper "$(risky)"'), encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:3:" in err
    assert "discards its exit status" in err


def test_main_clean_file_returns_zero(tmp_path: Path) -> None:
    good = tmp_path / "good.sh"
    good.write_text(_lines('out="$(risky)"', 'my_helper "$out"'), encoding="utf-8")
    assert mod.main([str(good)]) == 0


def test_main_reads_the_callee_from_a_sourced_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "lib.sh").write_text("lib_helper() { :; }\n", encoding="utf-8")
    script = tmp_path / "run.sh"
    script.write_text(
        'set -e\nsource "$(dirname "$0")/lib.sh"\nlib_helper "$(risky)"\n',
        encoding="utf-8",
    )
    assert mod.main([str(script)]) == 1
    assert f"{script}:3:" in capsys.readouterr().err


def test_main_skips_an_unreadable_path(tmp_path: Path) -> None:
    assert mod.main([str(tmp_path / "gone.sh")]) == 0


def test_main_reports_pathological_input_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file the grammar refuses must fail LOUDLY, never be skipped as clean."""
    hostile = tmp_path / "hostile.sh"
    hostile.write_text("set -e\n" + "cmd |" * 3000 + "cmd\n", encoding="utf-8")
    assert mod.main([str(hostile)]) == 1
    assert "pipe bytes" in capsys.readouterr().err


def test_main_names_the_sourced_file_the_grammar_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must name the file a reader has to go and look at, and it must
    never be swallowed — a dropped callee set loses findings silently."""
    (tmp_path / "lib.sh").write_text("cmd |" * 3000 + "cmd\n", encoding="utf-8")
    script = tmp_path / "run.sh"
    script.write_text("set -e\nsource lib.sh\n", encoding="utf-8")
    assert mod.main([str(script)]) == 1
    err = capsys.readouterr().err
    assert "sourced file" in err
    assert "lib.sh" in err


# ── the shipped tree is clean under this lint ────────────────────────────────
def test_repo_shell_tree_is_clean() -> None:
    """Dogfood: this repo's own tracked shell files must not violate. A finding
    here is either a real fail-open to fix or a false positive to answer, and
    both must block rather than sit undetected."""
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.sh", "*.bash"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    paths = [path for path in tracked if path]
    assert paths, "no tracked shell files found — the dogfood check would be vacuous"
    result = subprocess.run(
        ["python", "-m", "ci_truth_serum.check_argument_exit_swallow", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_the_module_parses_the_grammar_rather_than_the_text() -> None:
    """Meta-contract (.claude/rules/shell-lint-parsing.md): every structural
    question here is answered by `_bash_ast`, so the module must import the
    parser and must not carry a quote-state scanner or `shlex`."""
    source = _SRC.read_text(encoding="utf-8")
    assert "from _bash_ast import" in source
    assert "shlex" not in source

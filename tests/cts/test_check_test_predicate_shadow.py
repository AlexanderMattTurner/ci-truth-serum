"""Tests for ci_truth_serum/check_test_predicate_shadow.py — the lint that bans a
test-side shell function shadowing a production PURE PREDICATE.

The whole value of the lint is the line it draws: a dependency stub (a logger, a
privilege escalator, a network call) is legitimate and must stay unflagged, while
a side-effect-free predicate copied into a test is the defect. So the purity
classifier is asserted member-by-member on both sides of that line, not sampled.

Drives ``pure_predicates`` / ``function_definitions`` / ``find_shadowed`` for the
classification rules, ``main()`` over a real throwaway git tree for the argv and
exit-code contract, and two Hypothesis properties for crash-resistance.
"""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import commit_all, init_test_repo, load_hook

mod = load_hook("check_test_predicate_shadow.py", "check_test_predicate_shadow")

# The defect that motivated the lint: the shipped predicate rejects a leading
# zero and an overlong digit run; the test's copy accepts both, so the security
# regression it exists to cover cannot fail the test.
SHIPPED_PORT = "valid_host_port() { [[ $1 =~ ^[1-9][0-9]{0,4}$ ]]; }\n"
COPIED_PORT = "valid_host_port() { [[ $1 =~ ^[0-9]+$ ]]; }\n"


# ── pure: bodies that only test and return ───────────────────────────────
@pytest.mark.parametrize(
    "body",
    [
        "[[ $1 =~ ^[1-9][0-9]{0,4}$ ]]",  # the shipped predicate's own shape
        '[ -n "$1" ]',  # the POSIX test builtin
        "[[ $x = y ]]",  # a single `=` here is comparison, not assignment
        "(( $1 > 0 ))",  # arithmetic evaluation with no mutation
        "return",
        "return 1",
        "true",
        "false",
        "! [[ -d $1 ]]",
        "[[ -n $1 ]] && [[ $1 != -- ]]",  # `--` in a test is a string, not a decrement
        "[[ -n $1 ]] || return 1",
        "[[ ${x:-} == a ]]",
    ],
)
def test_pure_predicate_bodies_are_classified_pure(body: str) -> None:
    assert mod.pure_predicates(f"f() {{ {body}; }}\n") == {"f": 1}


@pytest.mark.parametrize(
    "src",
    [
        "f() {\n  [[ -n $1 ]] || return 1\n  [[ $1 == --* ]]\n}\n",  # multi-statement
        "function f() { [[ -e $1 ]]; }\n",  # keyword form with parens
        "function f { [[ -e $1 ]]; }\n",  # keyword form without parens
        "f() {\n  # why this predicate exists\n  [[ -n $1 ]]\n}\n",  # comment-only line
        "f() { [[ -n $1 ]] && \\\n  [[ $1 != x ]]; }\n",  # line continuation
    ],
)
def test_pure_predicate_spellings(src: str) -> None:
    assert "f" in mod.pure_predicates(src)


# ── impure: a dependency stub, a mutation, a shell-out ───────────────────
@pytest.mark.parametrize(
    "body",
    [
        'echo "hi"',  # a logger — the archetypal legitimate stub
        "printf x",
        'helper "$1"',  # delegating to another function
        "[[ $(id -u) == 0 ]]",  # a test wrapping a shell-out
        "[[ $1 == `id` ]]",  # the backtick spelling of the same
        "(( count++ ))",  # a counter bump is a side effect
        "(( n-- ))",
        "(( x = 5 ))",
        "(( x += 1 ))",
        "local x=1; [[ $x ]]",  # a declaration
        "x=1",  # a bare assignment
        "read -r x < /etc/foo",  # a redirection
        "grep -q x /f",  # an arbitrary command
        ":",  # an empty no-op stub: no logic to have been copied
        "if [[ $1 ]]; then return 0; fi",  # control flow is not the flat shape
        "case $1 in a) true;; *) false;; esac",
        "{ [[ $1 ]]; }",  # a nested brace group
        'return "$code"',  # richer than the bare `return [n]` form
    ],
)
def test_impure_bodies_are_not_predicates(body: str) -> None:
    assert mod.pure_predicates(f"f() {{ {body}; }}\n") == {}


def test_empty_body_is_not_a_predicate() -> None:
    assert mod.pure_predicates("f() {\n}\n") == {}


def test_heredoc_body_is_not_a_predicate() -> None:
    assert mod.pure_predicates("f() {\n  cat <<EOF\nhi\nEOF\n}\n") == {}


def test_first_pure_definition_wins() -> None:
    src = "f() { true; }\nf() { [[ -n $1 ]]; }\n"
    assert mod.pure_predicates(src) == {"f": 1}


def test_redefinition_after_an_impure_first_definition_is_still_found() -> None:
    src = "f() { echo hi; }\nf() { [[ -n $1 ]]; }\n"
    assert mod.pure_predicates(src) == {"f": 2}


# ── definitions ──────────────────────────────────────────────────────────
def test_function_definitions_finds_both_forms_in_source_order() -> None:
    src = "function kw { true; }\nfoo() { echo hi; }\n"
    assert mod.function_definitions(src) == [("kw", 1), ("foo", 2)]


def test_a_bare_call_is_not_a_definition() -> None:
    assert mod.function_definitions("foo() { true; }\nfoo\nbar\n") == [("foo", 1)]


# ── find_shadowed ────────────────────────────────────────────────────────
def _write(tmp_path, rel: str, text: str) -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_predicate_redefinition_is_flagged(tmp_path) -> None:
    test_file = _write(tmp_path, "tests/drive.bash", COPIED_PORT)
    hits = mod.find_shadowed([test_file], {"valid_host_port": "lib/ip.bash"})
    assert hits == [
        mod.Shadow(test_file, 1, "valid_host_port", "lib/ip.bash"),
    ]


def test_dependency_stub_of_a_non_predicate_is_not_flagged(tmp_path) -> None:
    """`log` ships as a printer, so it is never in the predicate set and its
    stub — the legitimate case this lint must not touch — passes."""
    test_file = _write(
        tmp_path, "tests/drive.bash", 'log() { :; }\nas_root() { "$@"; }\n'
    )
    assert mod.find_shadowed([test_file], {"valid_host_port": "lib/ip.bash"}) == []


def test_opt_out_on_the_definition_line_suppresses(tmp_path) -> None:
    test_file = _write(
        tmp_path,
        "tests/drive.bash",
        "valid_host_port() { [[ $1 =~ ^[0-9]+$ ]]; }  # predicate-shadow-ok: fixture\n",
    )
    assert mod.find_shadowed([test_file], {"valid_host_port": "lib/ip.bash"}) == []


def test_a_bare_opt_out_marker_does_not_suppress(tmp_path) -> None:
    """The shared annotation matcher requires a reason — a naked marker states
    nothing and must not silence the finding."""
    test_file = _write(
        tmp_path,
        "tests/drive.bash",
        COPIED_PORT.rstrip("\n") + "  # predicate-shadow-ok\n",
    )
    assert len(mod.find_shadowed([test_file], {"valid_host_port": "lib/ip.bash"})) == 1


def test_unreadable_path_is_skipped(tmp_path) -> None:
    missing = str(tmp_path / "tests" / "gone.bash")
    assert mod.find_shadowed([missing], {"valid_host_port": "lib/ip.bash"}) == []


def test_production_predicates_reports_the_first_path_sorted(tmp_path) -> None:
    late = _write(tmp_path, "z.bash", SHIPPED_PORT)
    early = _write(tmp_path, "a.bash", SHIPPED_PORT)
    assert mod.production_predicates([late, early]) == {"valid_host_port": early}


# ── main() over a real git tree ──────────────────────────────────────────
def _tree(tmp_path, production: str, test: str) -> None:
    init_test_repo(tmp_path)
    _write(tmp_path, "lib/ip.bash", production)
    _write(tmp_path, "tests/drive.bash", test)
    commit_all(tmp_path)


def test_main_flags_the_historical_defect(tmp_path, monkeypatch, capsys) -> None:
    """Red on the shape that shipped: the test carries its own weaker copy of
    `valid_host_port` instead of sourcing the file that defines it."""
    _tree(tmp_path, SHIPPED_PORT, COPIED_PORT)
    monkeypatch.chdir(tmp_path)
    assert mod.main(["tests/drive.bash"]) == 1
    err = capsys.readouterr().err
    assert "tests/drive.bash:1:" in err
    assert (
        "valid_host_port() redefines the pure predicate shipped at lib/ip.bash" in err
    )


def test_main_green_once_the_test_sources_the_real_definition(
    tmp_path, monkeypatch
) -> None:
    _tree(tmp_path, SHIPPED_PORT, "source lib/ip.bash\nlog() { :; }\n")
    monkeypatch.chdir(tmp_path)
    assert mod.main(["tests/drive.bash"]) == 0


def test_main_all_scans_every_tracked_test_file(tmp_path, monkeypatch) -> None:
    _tree(tmp_path, SHIPPED_PORT, COPIED_PORT)
    monkeypatch.chdir(tmp_path)
    assert mod.main(["--all"]) == 1


def test_main_ignores_a_production_file_passed_as_argv(tmp_path, monkeypatch) -> None:
    """Only a TEST file can shadow; the production definition itself is the
    thing being protected, so passing it is a no-op."""
    _tree(tmp_path, SHIPPED_PORT, COPIED_PORT)
    monkeypatch.chdir(tmp_path)
    assert mod.main(["lib/ip.bash"]) == 0


def test_main_ignores_an_untracked_production_file(tmp_path, monkeypatch) -> None:
    """The predicate set comes from the tracked tree: an untracked scratch copy
    of a predicate cannot make a committed test file red."""
    init_test_repo(tmp_path)
    _write(tmp_path, "tests/drive.bash", COPIED_PORT)
    commit_all(tmp_path)
    _write(tmp_path, "lib/ip.bash", SHIPPED_PORT)  # never committed
    monkeypatch.chdir(tmp_path)
    assert mod.main(["tests/drive.bash"]) == 0


def test_main_with_no_test_files_returns_zero(tmp_path, monkeypatch) -> None:
    _tree(tmp_path, SHIPPED_PORT, COPIED_PORT)
    monkeypatch.chdir(tmp_path)
    assert mod.main([]) == 0


# ── properties ───────────────────────────────────────────────────────────
# Fragments that reach the classifier's real branches: test commands, arithmetic
# with and without mutation, the pure commands, combinators, substitutions,
# redirections, declarations, and the separators between them.
_FRAGMENTS = st.sampled_from(
    [
        "f() {",
        "function g {",
        "}",
        "[[ -n $1 ]]",
        "[[ $1 =~ ^[0-9]+$ ]]",
        "[ -z $2 ]",
        "(( x > 1 ))",
        "(( x++ ))",
        "(( x = 1 ))",
        "return",
        "return 2",
        "true",
        "false",
        ":",
        "!",
        "&&",
        "||",
        ";",
        "\n",
        "echo hi",
        "$(id -u)",
        "`id`",
        "< /etc/passwd",
        "local x=1",
        "x=1",
        "# predicate-shadow-ok: r",
        "'",
        '"',
        "\\",
    ]
)


@given(st.lists(_FRAGMENTS, max_size=40).map("".join))
def test_pure_predicates_never_raises_and_reports_real_lines(text: str) -> None:
    found = mod.pure_predicates(text)
    line_count = len(text.splitlines())
    assert all(1 <= lineno <= max(line_count, 1) for lineno in found.values())


@given(st.lists(_FRAGMENTS, max_size=40).map("".join))
def test_classification_is_deterministic(text: str) -> None:
    assert mod.pure_predicates(text) == mod.pure_predicates(text)
    # Every pure predicate is also a definition — the two passes agree on the
    # names they see, so a body can never be classified without being found.
    assert set(mod.pure_predicates(text)) <= {
        n for n, _ in mod.function_definitions(text)
    }

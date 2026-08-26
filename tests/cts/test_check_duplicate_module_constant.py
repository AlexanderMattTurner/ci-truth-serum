"""Tests for ci_truth_serum/check_duplicate_module_constant.py — the lint that
flags a module-level name ASSIGNED MORE THAN ONCE at top level (the second
binding silently shadows the first).

Drives `violations()` directly on source snippets so each rule is asserted in
isolation, then pins `main()`'s argv/exit-code contract over a real file.
"""

import ast

from tests._helpers import load_hook

mod = load_hook("check_duplicate_module_constant.py", "check_duplicate_module_constant")


def test_single_definition_is_clean() -> None:
    assert mod.violations("X = 1\nY = 2\n") == []


def test_two_identical_top_level_assigns_flag_the_second() -> None:
    # The canonical shadow: same name bound twice at module scope, second wins.
    assert mod.violations("X = 1\nX = 1\n") == [2]


def test_two_differing_top_level_assigns_flag_the_second() -> None:
    # Different RHS is still a shadow — the second binding discards the first
    # regardless of value.
    assert mod.violations("X = compute_a()\nX = compute_b()\n") == [2]


def test_value_reads_name_false_when_stmt_carries_no_value() -> None:
    # _value_reads_name guards on a missing `.value` (a bare annotation like
    # `x: int` is an AnnAssign with value=None) and returns False rather than
    # crashing — a defensive path `violations()` never reaches (a re-binding
    # always carries a value), so it is asserted directly here.
    stmt = ast.parse("x: int").body[0]
    assert mod._value_reads_name(stmt, "x") is False


def test_third_binding_also_flagged() -> None:
    # Every later binding is a shadow, not just the second.
    assert mod.violations("X = 1\nX = 2\nX = 3\n") == [2, 3]


def test_annotated_reassignment_is_flagged() -> None:
    # An AnnAssign carrying a value is a binding; a second one shadows.
    assert mod.violations("X: int = 1\nX: int = 2\n") == [2]


def test_bare_annotation_then_assignment_is_not_flagged() -> None:
    # `x: int` (no value) is a declaration, not a binding — the single real
    # assignment below is the sole binding.
    assert mod.violations("X: int\nX = 5\n") == []


def test_augmented_assignment_is_not_flagged() -> None:
    # `x += …` reads-then-writes; it can't shadow a prior definition.
    assert mod.violations("X = []\nX += [1]\n") == []


def test_self_referential_rebuild_is_not_flagged() -> None:
    # `x = x + …` reads the prior binding — intentional accumulation, not a copy.
    assert mod.violations("X = [1]\nX = X + [2]\n") == []
    assert mod.violations("__all__ = ['a']\n__all__ = __all__ + ['b']\n") == []
    assert mod.violations("X = (1,)\nX = [*X, 2]\n") == []


def test_conditional_definition_is_not_flagged() -> None:
    # A default at module scope overridden inside an `if` is a deliberate branch,
    # not a flat re-binding — the override lives inside the If, not Module.body.
    assert mod.violations("X = 1\nif cond:\n    X = 2\n") == []


def test_if_else_branches_are_not_flagged() -> None:
    # Both bindings sit on different branches of one conditional (inside the If).
    src = "if TYPE_CHECKING:\n    X = 1\nelse:\n    X = 2\n"
    assert mod.violations(src) == []


def test_try_except_import_fallback_is_not_flagged() -> None:
    # The classic guarded-definition idiom: import X, or a fallback on failure.
    # Both assignments are nested in the Try, never direct Module.body children.
    src = "try:\n    from fast import X\nexcept ImportError:\n    X = None\n"
    assert mod.violations(src) == []


def test_reassignment_inside_a_function_is_not_flagged() -> None:
    # A local shadowing a module constant inside a function body is not a
    # module-level duplicate; only Module.body statements are considered.
    src = "X = 1\ndef f():\n    X = 2\n    return X\n"
    assert mod.violations(src) == []


def test_tuple_unpacking_reuse_is_flagged() -> None:
    # Each Name in a tuple target is a binding; reusing `a` shadows it.
    assert mod.violations("a, b = f()\na, c = g()\n") == [2]


def test_starred_unpacking_binds_its_name() -> None:
    assert mod.violations("head, *rest = xs\nrest = ys\n") == [2]


def test_chained_assignment_targets_each_count() -> None:
    # `a = b = value` binds both a and b; a later `b = …` shadows b.
    assert mod.violations("a = b = 1\nb = 2\n") == [2]


def test_attribute_and_subscript_targets_are_ignored() -> None:
    # `obj.attr = …` / `d[k] = …` mutate an existing object; they are not
    # module-name bindings, so repeating them is not a duplicate constant.
    assert mod.violations("sys.path = a\nsys.path = b\n") == []
    assert mod.violations("D = {}\nD['k'] = 1\nD['k'] = 2\n") == []


def test_allow_annotation_suppresses_same_line() -> None:
    src = "X = 1\nX = 2  # allow-duplicate-constant: two live spellings on purpose\n"
    assert mod.violations(src) == []


def test_allow_annotation_requires_a_reason() -> None:
    # A bare marker with no `: <reason>` states nothing and does not suppress.
    src = "X = 1\nX = 2  # allow-duplicate-constant\n"
    assert mod.violations(src) == [2]


def test_allow_annotation_on_multiline_statement_span() -> None:
    # The marker on the closing line of a multi-line offending assignment counts.
    src = "X = 1\nX = (\n    2\n)  # allow-duplicate-constant: reason\n"
    assert mod.violations(src) == []


def test_allow_on_first_definition_does_not_suppress_a_later_shadow() -> None:
    # The opt-out must sit on the OFFENDING (duplicate) statement, not the first.
    src = "X = 1  # allow-duplicate-constant: reason\nX = 2\n"
    assert mod.violations(src) == [2]


def test_distinct_names_never_collide() -> None:
    assert mod.violations("A = 1\nB = 2\nA = 3\nB = 4\n") == [3, 4]


def test_unparseable_text_returns_no_hits() -> None:
    # A non-Python / syntactically broken file must not crash the scan.
    assert mod.violations("this is (not python\n") == []


# ── main: argv/exit-code contract ─────────────────────────────────────────
def test_main_flags_a_duplicate_and_names_the_annotation(tmp_path, capsys) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("PATTERN = 1\nPATTERN = 2\n", encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert "bad.py:2:" in err
    assert "allow-duplicate-constant" in err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(
        "X = 1\nY = X + 1\nZ = 1\nZ = 2  # allow-duplicate-constant: deliberate\n",
        encoding="utf-8",
    )
    assert mod.main([str(ok)]) == 0


def test_main_skips_unreadable_path(tmp_path) -> None:
    missing = tmp_path / "does-not-exist.py"
    assert mod.main([str(missing)]) == 0


def test_empty_argv_exits_2_via_cli_contract() -> None:
    # run_file_cli(main) refuses an empty argv rather than reporting a clean
    # pass over nothing.
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, mod.__file__], capture_output=True, check=False
    )
    assert result.returncode == 2

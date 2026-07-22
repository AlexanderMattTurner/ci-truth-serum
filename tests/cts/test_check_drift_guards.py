"""Tests for ci_truth_serum/check_drift_guards.py — the pre-commit lint that requires a
justified @pytest.mark.drift_guard marker on any test that reads as a drift
guard.

Drives `violations()` and its helpers directly so every branch — phrase
detection, the marker/justification shape filter, and main()'s exit code — is
asserted in isolation.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, dogfood_extras_exclude, load_hook

_SRC = HOOKS_DIR / "check_drift_guards.py"
mod = load_hook("check_drift_guards.py", "check_drift_guards")


@pytest.mark.parametrize(
    "name, doc, expected",
    [
        # Guard-intent phrasing in the docstring -> detected. Both separator
        # variants of each hyphen/space class are covered so a mutant collapsing
        # `drift[- ]guard` / `anti[- ]?drift` is caught.
        ("test_x", "drift guard: the two lists agree", True),
        ("test_x", "a drift-guard on the config", True),
        ("test_x", "asserted on the source so it can't drift", True),
        ("test_x", "so the two cannot drift", True),
        ("test_x", "the allowlists never drift", True),
        ("test_x", "the host list won't drift from the container's", True),
        ("test_x", "an anti-drift assertion", True),
        ("test_x", "an anti drift assertion", True),
        ("test_x", "must stay in sync with detect_provider", True),
        ("test_x", "must remain in sync with the SSOT", True),
        ("test_x", "the two can't diverge", True),
        ("test_x", "so the values never diverge", True),
        ("test_x", "the two pins move in lockstep", True),
        ("test_x", "kept in sync with the entrypoint", True),
        ("test_x", "the paths are kept in step across both", True),
        # Phrasing in the NAME (underscores read as spaces).
        ("test_configs_must_stay_in_sync", "", True),
        ("test_no_drift_guard_regression", "", True),
        ("test_values_in_lockstep", "", True),
        # Merely mentioning drift, without guard intent -> NOT detected.
        ("test_main_check_mode_detects_drift", "tool reports drift", False),
        ("test_drift_triggers_rewrite", "rewrites on drift", False),
        ("test_plain", "asserts the parsed value", False),
        # "lockstep" WITHOUT "in" often names a runtime mechanism, not a
        # copies-agree guard -> NOT detected (avoids over-matching).
        (
            "test_entrypoint_lockstep_guard_is_inert",
            "the lockstep guard is inert",
            False,
        ),
    ],
)
def test_is_drift_guard(name: str, doc: str, expected: bool) -> None:
    assert mod._is_drift_guard(name, doc) is expected


def _decorator(src: str) -> ast.expr:
    """Parse a single `@<expr>`-style decorator into its expression node."""
    func = ast.parse(f"{src}\ndef f(): ...").body[0]
    return func.decorator_list[0]


@pytest.mark.parametrize(
    "decorator_src, expected",
    [
        ('@pytest.mark.drift_guard("a stated reason")', "a stated reason"),
        ("@pytest.mark.drift_guard()", None),  # no justification arg
        ('@pytest.mark.drift_guard("")', None),  # empty justification
        ('@pytest.mark.drift_guard("   ")', None),  # whitespace-only
        ("@pytest.mark.drift_guard(123)", None),  # non-string justification
        ('@pytest.mark.parametrize("x", [])', None),  # different marker
        ("@some_function()", None),  # Call but func is a Name, not Attribute
        ("@pytest.fixture", None),  # decorator is not a Call at all
    ],
)
def test_justification(decorator_src: str, expected: str | None) -> None:
    assert mod._justification(_decorator(decorator_src)) == expected


@pytest.mark.parametrize(
    "src",
    [
        'def test_a():\n    """drift guard: lists agree"""\n',  # sync
        'async def test_a():\n    """the two cannot drift"""\n',  # async
    ],
)
def test_violations_flags_unmarked_drift_guard(src: str) -> None:
    assert mod.violations(src) == [(1, "test_a")]


def test_violations_passes_justified_drift_guard() -> None:
    src = (
        '@pytest.mark.drift_guard("the two configs live in different languages")\n'
        'def test_a():\n    """drift guard: lists agree"""\n'
    )
    assert mod.violations(src) == []


# ── Structural trigger: a maintained copy pinned against a read source ────────
# The laundering that motivated this trigger: a test worded to dodge the phrase
# lint ("SSOT-coverage contract") that still, structurally, asserts a
# hand-maintained collection equals a file-derived one.

_LAUNDERED_GUARD = (
    "def test_examples_cover_the_config():\n"
    '    """SSOT-coverage contract: examples cover the live set."""\n'
    "    live = json.load(open('detectors.json'))\n"
    "    assert sorted(EXAMPLES.keys()) == sorted(live)\n"
)


def test_structural_trigger_catches_laundered_guard() -> None:
    """A guard that avoids every intent phrase (calls itself an 'SSOT-coverage
    contract') is still caught by the copies-agree structure: it reads a source
    and asserts an UPPER_CASE constant's keys equal it."""
    assert mod.violations(_LAUNDERED_GUARD) == [(1, "test_examples_cover_the_config")]


def test_structural_trigger_cleared_by_marker() -> None:
    marked = (
        '@pytest.mark.drift_guard("JS pre-gate is a distinct ReDoS-safe representation")\n'
        + _LAUNDERED_GUARD
    )
    assert mod.violations(marked) == []


def test_structural_trigger_cleared_by_optout() -> None:
    """A genuine collection-equality unit test clears with an explicit reasoned
    opt-out, so the trigger doesn't force a false drift_guard label."""
    opted = (
        "def test_examples_cover_the_config():\n"
        "    live = json.load(open('detectors.json'))\n"
        "    # not-a-drift-guard: EXAMPLES is the code output, live is the fixture\n"
        "    assert sorted(EXAMPLES.keys()) == sorted(live)\n"
    )
    assert mod.violations(opted) == []


@pytest.mark.parametrize(
    "src",
    [
        # Ordinary output-vs-expected: neither side is a maintained copy, and the
        # left is a call to the code under test. The false-positive shape that a
        # blanket "two collections compared" rule would wrongly flag.
        (
            "def test_collector_returns_expected():\n"
            "    data = json.load(open('x.json'))\n"
            "    assertCountEqual(collect(data), {'a', 'b'})\n"
        ),
        # Maintained-copy equality but NO source read — not pinned against a
        # separate source here, so below the trigger (a known, stated gap).
        (
            "def test_two_local_lists():\n"
            "    assert sorted(EXAMPLES.keys()) == ['a', 'b']\n"
        ),
        # Single-source coverage (sanctioned): reads one config, asserts code
        # handles each entry via membership — no collection equality.
        (
            "def test_code_handles_every_entry():\n"
            "    for e in json.load(open('cfg.json')):\n"
            "        assert handles(e)\n"
        ),
    ],
)
def test_structural_trigger_ignores_non_guards(src: str) -> None:
    assert mod.violations(src) == []


def test_assert_count_equal_needs_a_maintained_copy() -> None:
    """assertCountEqual alone is NOT the tell (it's the common output-vs-expected
    idiom); it fires only when an argument is a maintained copy pinned to a read
    source."""
    non_guard = (
        "def test_x(self):\n"
        "    got = json.load(open('x.json'))\n"
        "    self.assertCountEqual(got, expected)\n"
    )
    guard = (
        "def test_x(self):\n"
        "    got = json.load(open('x.json'))\n"
        "    self.assertCountEqual(got, EXPECTED_SET)\n"
    )
    assert mod.violations(non_guard) == []
    assert mod.violations(guard) == [(1, "test_x")]


@pytest.mark.parametrize(
    "source",
    [
        # Not a function (a module-level mention) -> skipped.
        '"""drift guard for the whole module"""\nx = 1\n',
        # Function whose name doesn't start with test_ -> skipped.
        'def _helper():\n    """so it can\'t drift"""\n',
        # A test that merely mentions drift without guard intent -> not flagged.
        'def test_tool_detects_drift():\n    """the checker reports drift"""\n',
        # No guard-shaped docstring, nothing to flag.
        "def test_ordinary():\n    pass\n",
        # A file that does not parse produces no findings.
        "def (:\n",
    ],
)
def test_violations_ignores_non_guards(source: str) -> None:
    assert mod.violations(source) == []


@pytest.mark.parametrize(
    "line, flagged",
    [
        ("  it('configs must stay in sync', () => {", True),
        ("  // asserted on the source so it can't drift", True),
        ("  it('the two pins move in lockstep', () => {", True),
        ("  # the host and container lists are kept in sync", True),
        # a same-line reason-bearing annotation excuses it
        ("  it('must stay in sync'); // drift-guard-ok: cross-language SSOT", False),
        # a mere mention of drift without guard intent is not flagged
        ("  // the tool reports drift and rewrites", False),
        ("  echo 'building'", False),
    ],
)
def test_text_violations_phrase_pass(line: str, flagged: bool) -> None:
    hits = mod.text_violations(line + "\n")
    assert bool(hits) is flagged
    if flagged:
        assert hits[0][0] == 1


def test_text_violations_annotation_on_preceding_line() -> None:
    text = (
        "// drift-guard-ok: two runtimes, no shared SSOT\n"
        "it('the two configs must stay in sync', () => {});\n"
    )
    assert mod.text_violations(text) == []


def test_text_violations_bare_annotation_without_reason_still_fires() -> None:
    # A reasonless `drift-guard-ok` (no colon/value) does not suppress.
    text = "it('must stay in sync'); // drift-guard-ok\n"
    assert len(mod.text_violations(text)) == 1


def test_main_flags_non_python_guard_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "config.test.mjs"
    bad.write_text("test('configs must stay in sync', () => {});\n", encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:1: drift-guard intent" in err
    assert "drift-guard-ok:" in err


def test_main_accepts_annotated_non_python_guard(tmp_path: Path) -> None:
    good = tmp_path / "check.sh"
    good.write_text(
        "# drift-guard-ok: mirrors an external upstream value, no SSOT\n"
        "assert_equal must stay in sync\n",
        encoding="utf-8",
    )
    assert mod.main([str(good)]) == 0


def test_main_returns_one_on_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text('def test_a():\n    """drift guard: x"""\n', encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:1: drift guard 'test_a' lacks a justification" in err


def test_main_returns_zero_when_clean(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    good.write_text(
        '@pytest.mark.drift_guard("external upstream value, no SSOT")\n'
        'def test_a():\n    """drift guard: x"""\n',
        encoding="utf-8",
    )
    assert mod.main([str(good)]) == 0


def test_main_skips_an_unreadable_path(tmp_path: Path) -> None:
    assert mod.main([str(tmp_path / "absent.py")]) == 0


def _run_script(*paths: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real script as pre-commit does (paths on argv)."""
    return subprocess.run(
        [sys.executable, str(_SRC), *paths],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "src",
    [
        'def test_a():\n    """drift guard: lists agree"""\n',  # drift[- ]guard
        'def test_a():\n    """an anti-drift assertion"""\n',  # anti-drift
        'def test_a():\n    """so the two cannot drift"""\n',  # cannot drift
        'def test_a():\n    """must stay in sync with the SSOT"""\n',  # in sync
        "def test_must_stay_in_sync():\n    pass\n",  # intent in the NAME
        'def test_a():\n    """A DRIFT GUARD here"""\n',  # case-insensitive
    ],
)
def test_script_rejects_unmarked_drift_guard(tmp_path: Path, src: str) -> None:
    """The real script exits non-zero and names the offending file for each
    distinct guard-intent variant (incl. case-insensitive + name-based)."""
    bad = tmp_path / "bad.py"
    bad.write_text(src, encoding="utf-8")
    proc = _run_script(str(bad))
    assert proc.returncode == 1
    assert str(bad) in proc.stderr
    assert "lacks a justification" in proc.stderr


def test_script_accepts_marked_and_non_guard(tmp_path: Path) -> None:
    """Negative control: a justified marker, and a test that merely mentions
    drift without guard intent, are both accepted (exit 0)."""
    good = tmp_path / "good.py"
    good.write_text(
        '@pytest.mark.drift_guard("external upstream value, no SSOT")\n'
        'def test_a():\n    """drift guard: lists agree"""\n'
        'def test_tool_detects_drift():\n    """the checker reports drift"""\n',
        encoding="utf-8",
    )
    proc = _run_script(str(good))
    assert proc.returncode == 0
    assert proc.stderr == ""


def test_own_test_tree_is_clean() -> None:
    """Every guard-shaped test in this repo's own suite carries the marker (or
    lives in a file the dogfood aggregate excludes). A new unmarked guard turns
    this red, proving the check is wired to real sources, not just unit cases."""
    exclude = dogfood_extras_exclude()
    offenders = [
        (rel, lineno, name)
        for p in sorted((REPO_ROOT / "tests").rglob("*.py"))
        for rel in [str(p.relative_to(REPO_ROOT))]
        if not exclude.match(rel)
        for lineno, name in mod.violations(p.read_text(encoding="utf-8"))
    ]
    assert offenders == []

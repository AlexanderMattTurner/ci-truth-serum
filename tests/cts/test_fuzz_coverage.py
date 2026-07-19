"""SSOT obligation gate: every hook that parses untrusted input MUST be exercised
by at least one property/fuzz suite.

This is the same discipline as agent-input-sanitizer's fuzz-coverage gate, ported
to Python: line coverage can be 100% while a passthrough bug ships (a parser runs
the line without violating any *asserted* invariant), so a percentage cannot catch
"this parser has no crash-resistance test". Requiring a named fuzz target per
input-parsing hook can.

Non-vacuity (CLAUDE.md): the gate enumerates the required modules as an explicit
set and asserts (a) the set is non-empty, (b) it exactly matches the live set of
hooks that read external input (so a NEW input-parsing hook fails this test until
fuzzed), and (c) at least one real ``@given``-driven suite exists. A negative
"module X is referenced" assertion is paired with the positive "fuzz suites were
discovered" marker so it can never pass because discovery silently found nothing.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(
    subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
HOOKS_DIR = REPO_ROOT / "hooks"
FUZZ_DIR = REPO_ROOT / "tests" / "cts"

# Every hook that ingests external/untrusted input (file text, parsed YAML, a CLI
# command line) and so owes a property/fuzz target. Keyed by module stem; the
# value is the public symbol a fuzz suite must reference.
#
# Intentionally EXCLUDED (documented so each omission is a choice, not a miss):
#   - run_tier, sync_required_checks: orchestrators. run_tier dispatches argv to
#     other hooks (each fuzzed on its own); sync_required_checks is network/REST
#     plumbing whose only parser, required_check_contexts (in _linecheck), IS
#     fuzzed here.
#   - check_symlinks.sh: a shell hook, not a Python parser.
FUZZ_REQUIRED = {
    "_linecheck": "required_check_contexts",
    "check_exit_suppression": "violations",
    "check_stderr_suppression": "violations",
    "check_pipefail_grep_pipe": "violations",
    "check_pinned_downloads": "violations",
    "check_pinned_base_images": "violations",
    "check_global_stdio_swap": "violations",
    "check_workflow_pipefail": "analyze",
    "check_inline_run_length": "analyze",
    "check_always_reporter": "check_file",
    "check_required_reporter": "check_file",
    "check_concurrency": "check_file",
    "check_static_concurrency": "check_file",
    "check_requires_concurrency": "check_file",
    "check_externalized_markers": "check_file",
    "check_pr_paths": "check_file",
    "check_claude_model": "check_file",
    "check_path_gate_deps": "check_file",
    "check_failure_notifier_coverage": "check_repo",
    "check_unnamed_regex_groups": "check_file",
}

# Hooks that take only argv-of-paths / orchestrate and are deliberately not in the
# required set. Listed so the "exactly covers the live hooks" check below can
# subtract them and prove the required set is exhaustive over real parsers.
_NON_PARSER_HOOKS = {"run_tier", "sync_required_checks", "__init__"}


def _strip_comments(source: str) -> str:
    """Drop import lines and comments so a name only counts when it appears in
    actual test code -- a name in an ``import`` or a comment is NOT evidence that a
    property exercises it (mirrors the JS gate's stripImportsAndComments)."""
    out_lines = []
    for line in source.splitlines():
        if re.match(r"\s*(?:from|import)\b", line):
            continue
        out_lines.append(re.sub(r"#.*$", "", line))
    return "\n".join(out_lines)


def _fuzz_suites() -> list[tuple[str, str]]:
    """(filename, comment-stripped source) for every test file that actually drives
    Hypothesis -- discovered by the ``@given(`` sentinel, not by filename, so a
    renamed suite is still found. This gate file is excluded: it names every
    required symbol as a string literal, so scanning it would pass vacuously."""
    suites = []
    for path in sorted(FUZZ_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        if "@given(" in source:
            suites.append((path.name, _strip_comments(source)))
    return suites


SUITES = _fuzz_suites()


def test_gate_is_not_vacuous() -> None:
    # If discovery found no @given suites, every "is referenced" check below would
    # pass for the wrong reason. Pin both sides so the gate cannot pass empty.
    assert SUITES, "no @given fuzz suites discovered -- the gate would be vacuous"
    assert FUZZ_REQUIRED, "FUZZ_REQUIRED is empty -- nothing would be enforced"


def test_required_set_exactly_covers_live_input_parsing_hooks() -> None:
    # SSOT contract: the enumerated required set must match the live hooks on disk
    # (minus the documented non-parser orchestrators). A new check_*.py that reads
    # input fails here until added to FUZZ_REQUIRED and given a fuzz suite.
    live = {p.stem for p in HOOKS_DIR.glob("*.py") if p.stem not in _NON_PARSER_HOOKS}
    assert set(FUZZ_REQUIRED) == live, (
        "FUZZ_REQUIRED drifted from the live hooks: "
        f"missing={live - set(FUZZ_REQUIRED)}, "
        f"stale={set(FUZZ_REQUIRED) - live}"
    )


def test_every_required_symbol_is_a_real_module_member() -> None:
    # A stale entry (module/symbol renamed away) must fail loudly rather than be
    # silently satisfied by a string-literal match in some unrelated test.
    import importlib.util

    for stem, symbol in FUZZ_REQUIRED.items():
        src = HOOKS_DIR / f"{stem}.py"
        assert src.exists(), f"{stem}: no such hook"
        spec = importlib.util.spec_from_file_location(f"gate_{stem}", src)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert hasattr(mod, symbol), f"{stem}.{symbol} is not a real module member"


def test_every_input_parsing_hook_has_a_fuzz_suite() -> None:
    for stem, symbol in FUZZ_REQUIRED.items():
        # The module must be loaded by a fuzz suite (its name appears) AND its
        # required parser symbol must be referenced there -- both, so importing a
        # module without driving its parser does not satisfy the obligation.
        module_hits = [name for name, code in SUITES if stem in code]
        symbol_re = re.compile(rf"\b{re.escape(symbol)}\b")
        # Intersect: the symbol must be referenced in a suite that ALSO loads this
        # module. Otherwise a symbol shared across parsers (e.g. `check_file`) is
        # satisfied by some *other* module's suite, and this module could ship with
        # its parser undriven while the obligation reads as met.
        symbol_hits = [
            name for name, code in SUITES if stem in code and symbol_re.search(code)
        ]
        assert module_hits, f"{stem} parses input but no fuzz suite references it"
        assert symbol_hits, (
            f"{stem}.{symbol} is never exercised by a fuzz suite "
            "(module imported but its parser not driven)"
        )

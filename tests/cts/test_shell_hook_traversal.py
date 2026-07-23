"""Meta-contract: every shell-content lint scans through a SHARED traversal.

The bug class this pins closed: a lint that iterates ``text.splitlines()`` on
its own sees one physical line at a time, so any construct wrapped across
lines (a trailing ``|``/``\\`` continuation, a multi-line ``$(…)`` capture, a
single-line ``case … esac``) slips past it — each hook re-growing its own
half-correct joiner was how the evasions crept in one hook at a time. The two
sanctioned traversals are the real bash grammar (``_bash_ast``) and the one
shared continuation joiner (``_linecheck.logical_lines``); every shell lint
registered in ``run_tier.TIERS`` must use one of them.

The enumeration is driven from ``run_tier.TIERS`` (the SSOT for which lints
receive shell content), so a newly added shell lint is held to the contract
with no edit here.
"""

import re

from tests._helpers import HOOKS_DIR, load_hook

rt = load_hook("run_tier.py", "rt_for_traversal_contract")

# The run_tier selector kinds under which a lint receives shell file content.
_SHELL_KINDS = frozenset({rt.SHELL, rt.SHELL_OR_DOCKERFILE, rt.SHELL_OR_WORKFLOW_YAML})


def _shell_lints() -> list[str]:
    return sorted(
        {
            module
            for members in rt.TIERS.values()
            for module, kind in members
            if kind in _SHELL_KINDS
        }
    )


def test_shell_lint_enumeration_is_nonvacuous() -> None:
    """Positive marker: the TIERS-driven enumeration actually selects the shell
    lints (an empty or mis-keyed selection would make every assertion below
    pass vacuously)."""
    lints = _shell_lints()
    assert len(lints) >= 5
    assert "check_exit_suppression" in lints and "check_pipefail_grep_pipe" in lints


def test_every_shell_lint_uses_a_shared_traversal() -> None:
    """Each shell lint imports the bash grammar or the shared logical-line
    joiner — never neither."""
    offenders = []
    for module in _shell_lints():
        src = (HOOKS_DIR / f"{module}.py").read_text(encoding="utf-8")
        uses_ast = "from _bash_ast import" in src
        uses_joiner = bool(re.search(r"\blogical_lines\b", src))
        if not (uses_ast or uses_joiner):
            offenders.append(module)
    assert offenders == [], f"shell lints with no shared traversal: {offenders}"


def test_no_shell_lint_scans_physical_lines_directly() -> None:
    """The evasion-prone idiom itself is banned from shell lints: driving the
    scan off ``enumerate(text.splitlines())`` sees one physical line at a time.
    (Indexing ``text.splitlines()`` for annotation/neighbour lookups is fine —
    only the enumerate-scan driver is the hazard.)"""
    banned = re.compile(r"enumerate\(\s*(?:text|physical)\.splitlines\(\)")
    offenders = [
        module
        for module in _shell_lints()
        if banned.search((HOOKS_DIR / f"{module}.py").read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"shell lints scanning physical lines: {offenders}"
    # Positive marker proving the banned-idiom regex still matches the idiom:
    assert banned.search("for lineno, raw in enumerate(text.splitlines(), 1):")


def test_the_one_joiner_lives_in_linecheck_and_nowhere_else() -> None:
    """The shared joiner exists exactly once; a lint growing a private
    ``def _logical_lines`` copy is the duplication this contract eliminates."""
    assert "def logical_lines(" in (HOOKS_DIR / "_linecheck.py").read_text(
        encoding="utf-8"
    )
    copies = [
        path.name
        for path in HOOKS_DIR.glob("*.py")
        if path.name != "_linecheck.py"
        and re.search(r"def _?logical_lines\(", path.read_text(encoding="utf-8"))
    ]
    assert copies == [], f"private logical-line joiner copies: {copies}"

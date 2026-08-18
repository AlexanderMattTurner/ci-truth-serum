#!/usr/bin/env python3
"""
Run the checks a ``--select`` expression names: a tier, a tag, or one check.

The tier aggregates (``check-tier1`` / ``check-tier2`` / ``check-extras``) group
by how opinionated a check is. This hook groups by what a check is ABOUT, so a
consumer who wants every security and secrets lint, wherever it sits, writes::

    - id: check-select
      args: [--select, "tag:security", --select, "tag:secrets"]

A selector is one of ``all``, ``tier:<1|2|extras>``, ``tag:<name>``, or
``check:<module-or-hook-id>``. ``--select`` unions, then ``--ignore`` subtracts,
so tier 1 without its Docker lint is::

    args: [--select, "tier:1", --ignore, "check:check-pinned-base-images"]

Three failures exit 2 instead of running a smaller set: no ``--select`` at all,
an unknown selector, and a selection that ends up empty. A hook that runs zero
checks and reports success is the false green this pack exists to refuse.

Members run exactly as under ``run_tier``: a workflow lint self-discovers
``.github/{workflows,actions}``, and a content lint receives only the passed
files of its kind. The registry of checks, tiers and tags is
``ci_truth_serum/_registry.py``.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _registry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    CHECKS,
    TAGS,
    TIERS,
    Check,
    by_tag,
)
from run_tier import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    report_unscanned,
    run_members,
)

USAGE = (
    "usage: run_selection --select <selector> [--select <selector>]... "
    "[--ignore <selector>]... [files...]\n"
    "  selector: all | tier:<1|2|extras> | tag:<name> | check:<module-or-hook-id>"
)


class SelectorError(ValueError):
    """A selector that names nothing this pack ships."""


def resolve(selector: str) -> list[Check]:
    """The checks SELECTOR names, in registry order.

    Raises SelectorError when the selector has no known prefix, or has one whose
    value this pack does not ship. A typo must stop the run: silently resolving
    it to nothing turns a misconfigured hook into a passing one.
    """
    if selector == "all":
        return list(CHECKS)
    kind, _, value = selector.partition(":")
    if not value:
        raise SelectorError(f"unknown selector {selector!r}")
    if kind == "tier":
        if value not in TIERS:
            raise SelectorError(
                f"unknown tier {value!r}; valid: {', '.join(sorted(TIERS))}"
            )
        return [c for c in CHECKS if c.tier == value]
    if kind == "tag":
        if value not in TAGS:
            raise SelectorError(
                f"unknown tag {value!r}; valid: {', '.join(sorted(TAGS))}"
            )
        return by_tag(value)
    if kind == "check":
        module = value.replace("-", "_")
        found = [c for c in CHECKS if c.module == module]
        if not found:
            raise SelectorError(f"unknown check {value!r}")
        return found
    raise SelectorError(f"unknown selector {selector!r}")


def resolve_all(selects: list[str], ignores: list[str]) -> list[Check]:
    """The union of SELECTS minus the union of IGNORES, in registry order."""
    selected = {c.module for s in selects for c in resolve(s)}
    dropped = {c.module for s in ignores for c in resolve(s)}
    keep = selected - dropped
    return [c for c in CHECKS if c.module in keep]


def parse_args(argv: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split ARGV into (selects, ignores, files).

    Raises SelectorError when a flag has no value after it.
    """
    selects: list[str] = []
    ignores: list[str] = []
    files: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--select", "--ignore"):
            if i + 1 >= len(argv):
                raise SelectorError(f"{arg} requires an argument")
            (selects if arg == "--select" else ignores).append(argv[i + 1])
            i += 2
        elif arg.startswith("--"):
            # A misspelled flag must not become a filename. `--selec tag:tests`
            # would otherwise drop that selection and run the rest, which is the
            # narrower green this hook refuses.
            raise SelectorError(f"unknown option {arg!r}")
        else:
            files.append(arg)
            i += 1
    return selects, ignores, files


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        selects, ignores, files = parse_args(argv)
        if not selects:
            print(USAGE, file=sys.stderr)
            return 2
        chosen = resolve_all(selects, ignores)
    except SelectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(USAGE, file=sys.stderr)
        return 2

    if not chosen:
        print(
            "error: the selection matched no checks; a hook that runs nothing "
            "reports a green it did not earn",
            file=sys.stderr,
        )
        return 2

    rc, unscanned = run_members([(c.module, c.kind) for c in chosen], files)
    flags = " ".join(
        [f"--select {s}" for s in selects] + [f"--ignore {s}" for s in ignores]
    )
    report_unscanned(
        unscanned,
        files,
        "selected checks",
        f"python -m ci_truth_serum.run_selection {flags}",
    )
    return rc


if __name__ == "__main__":
    sys.exit(main())

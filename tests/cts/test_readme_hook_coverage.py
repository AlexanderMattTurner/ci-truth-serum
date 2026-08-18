"""Every exported hook must have a README row, and every row a real hook.

The bug class this pins closed: `check-frozen-head-sha` and
`check-cancellable-required-check` both shipped in `.pre-commit-hooks.yaml`
and appeared in no README table at all. A consumer reading the README to
decide what to enable could not learn they existed, and the omission was
silent for as long as nobody hand-audited the two lists. Adding a hook and
forgetting its row is the natural way to write that bug, because the manifest
entry is what makes the hook work and the row is what makes it known.

The reverse direction is the same defect mirrored: a row naming a hook the
manifest no longer exports sends a consumer to add an `- id:` that fails at
`pre-commit install-hooks`.

The tier aggregates (`check-tier1`, `check-tier2`, `check-extras`) are
excluded. They are not individual checks, and the README documents them in the
"Enable a whole tier with one id" section rather than as table rows.
"""

import re

import pytest
import yaml

from tests._helpers import REPO_ROOT

# A hook id in the first column of a README table row: `| `check-foo` | … |`.
_README_ROW = re.compile(r"^\|\s*`(?P<hook>check-[\w-]+)`", re.MULTILINE)
# The aggregates: one id per tier, plus the tag/tier selector. Each runs other
# checks rather than being one, so each is documented in prose, not as a row.
AGGREGATES = frozenset({"check-tier1", "check-tier2", "check-extras", "check-select"})


def _exported_hooks() -> set[str]:
    manifest = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-hooks.yaml").read_text(encoding="utf-8")
    )
    return {hook["id"] for hook in manifest} - AGGREGATES


def _documented_hooks() -> set[str]:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return set(_README_ROW.findall(readme))


@pytest.mark.drift_guard(
    "the README is prose a human reads to choose hooks; it cannot enumerate "
    "the manifest at render time, so the two lists are compared here instead"
)
def test_every_exported_hook_has_a_readme_row() -> None:
    exported, documented = _exported_hooks(), _documented_hooks()
    # Non-vacuity: both sides must be populated, or an empty regex match (a
    # changed table format) would satisfy the subset test for the wrong reason.
    assert len(exported) > 20, f"only {len(exported)} exported hooks — parse broke?"
    assert len(documented) > 20, f"only {len(documented)} README rows — parse broke?"
    assert exported - documented == set(), (
        f"these hooks ship in .pre-commit-hooks.yaml with no README table row: "
        f"{sorted(exported - documented)} — a consumer reading the README cannot "
        "learn they exist. Add a row to the table for the hook's tier."
    )


def test_every_readme_row_names_an_exported_hook() -> None:
    exported, documented = _exported_hooks(), _documented_hooks()
    assert documented - exported == set(), (
        f"these README rows name hooks .pre-commit-hooks.yaml does not export: "
        f"{sorted(documented - exported)} — a consumer copying the id gets a "
        "`pre-commit install-hooks` failure. Drop the row, or restore the hook."
    )


def test_the_row_matcher_reads_a_real_table_row() -> None:
    """Non-vacuity for the matcher itself: it must accept the shipped row shape
    and reject a hook id merely mentioned in prose or in the usage YAML."""
    assert _README_ROW.findall("| `check-job-timeout` | Catches a job … |") == [
        "check-job-timeout"
    ]
    assert _README_ROW.findall("      # - id: check-job-timeout  # a comment") == []
    assert _README_ROW.findall("Run `check-job-timeout` on every job.") == []

#!/usr/bin/env python3
"""Aggregate the sharded mutation reports and enforce the coverage contract.

Each shard runs cosmic-ray over one module and uploads a
``reports/mutation/<id>.json`` (``{id, rate}``). This step collects them and
gates on the honesty property the sharding could otherwise erode: the set of
shard reports must be EXACTLY the set the expander produced from the same
checkout — no missing slice (a crashed shard that uploaded nothing), no
duplicate, no stray id. A missing report would let the run score a subset of the
codebase as if it were the whole, so a mismatch fails loud.

Survivors are not a build break here (matching the unsharded run-mutation.sh):
the per-shard survival rates are printed as diagnostics, not thresholded. The
gate is coverage completeness, not the score.

With SURVIVORS-OUT, it also writes the surviving mutants as Markdown there, or
an empty file when every shard killed every mutant. The daily ledger job turns
that file into the persistent mutation issue.

Usage: python aggregate-mutation.py <reports-dir> [<survivors-out>]
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _repo_root import find_repo_root  # noqa: E402
from mutation_shards import expand_shards  # noqa: E402


def _load_reports(reports_dir: Path) -> list[dict]:
    """Every shard report under REPORTS_DIR (recursively — the download unpacks
    one artifact per subdir), parsed."""
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(reports_dir.rglob("*.json"))
    ]


def aggregate(repo_root: Path, reports: list[dict]) -> list[str]:
    """Return the diagnostic summary lines, or raise if the report set does not
    match the shard set exactly (one report per shard, ids identical)."""
    expected = {s["id"] for s in expand_shards(repo_root)}
    found = [r["id"] for r in reports]
    found_set = set(found)

    if len(found) != len(found_set):
        dupes = sorted({i for i in found if found.count(i) > 1})
        raise SystemExit(f"duplicate shard report id(s): {dupes}")
    if found_set != expected:
        missing = sorted(expected - found_set)
        extra = sorted(found_set - expected)
        raise SystemExit(
            "shard reports do not match the shard set; refusing to gate on a "
            f"partial result. missing={missing} unexpected={extra}"
        )

    lines = [f"Aggregated {len(reports)} shard report(s) (one per shard):"]
    for report in sorted(reports, key=lambda r: r["id"]):
        lines.append(f"- {report['id']}: survival rate {report.get('rate', 'unknown')}")
    return lines


def survivors_markdown(reports: list[dict]) -> str:
    """The surviving mutants in REPORTS, one section per module, or "" when
    there are none."""
    sections = []
    for report in sorted(reports, key=lambda r: r["id"]):
        found = report["survivors"]
        if not found:
            continue
        lines = [f"### `{report['id']}` (survival rate {report['rate']})", ""]
        for mutant in found:
            where = f" in `{mutant['definition']}`" if mutant["definition"] else ""
            lines.append(
                f"- `{mutant['path']}:{mutant['line']}:{mutant['column']}`"
                f" `{mutant['operator']}`{where}"
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def main(argv: list[str]) -> None:
    if len(argv) not in (1, 2):
        raise SystemExit("usage: aggregate-mutation.py <reports-dir> [<survivors-out>]")
    repo_root = find_repo_root(Path(__file__).resolve().parent)
    reports_dir = Path(argv[0])
    if not reports_dir.is_dir():
        raise SystemExit(f"reports dir {reports_dir} does not exist")

    reports = _load_reports(reports_dir)
    lines = aggregate(repo_root, reports)
    if len(argv) == 2:
        Path(argv[1]).write_text(survivors_markdown(reports), encoding="utf-8")
    summary = "\n".join(lines)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Mutation testing\n\n{summary}\n")


if __name__ == "__main__":
    main(sys.argv[1:])

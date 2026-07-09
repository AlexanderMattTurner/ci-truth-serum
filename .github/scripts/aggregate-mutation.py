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

Usage: python aggregate-mutation.py <reports-dir>
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
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


def main(argv: list[str]) -> None:
    if len(argv) != 1:
        raise SystemExit("usage: aggregate-mutation.py <reports-dir>")
    repo_root = Path(__file__).resolve().parents[2]
    reports_dir = Path(argv[0])
    if not reports_dir.is_dir():
        raise SystemExit(f"reports dir {reports_dir} does not exist")

    lines = aggregate(repo_root, _load_reports(reports_dir))
    summary = "\n".join(lines)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Mutation testing\n\n{summary}\n")


if __name__ == "__main__":
    main(sys.argv[1:])

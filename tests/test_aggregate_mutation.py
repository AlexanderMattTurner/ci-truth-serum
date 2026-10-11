"""Tests for .github/scripts/aggregate-mutation.py — the shard coverage gate.

The aggregator's whole job is the anti-vacuous check: the set of shard reports
must be EXACTLY the shard set the expander produces from the same checkout, so a
crashed/missing slice can't let the run score a subset as the whole. These pin
that contract and the diagnostic summary; survivors are never a build break.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "aggregate-mutation.py"
_spec = importlib.util.spec_from_file_location("aggregate_mutation", _SRC)
agg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agg)

_MS = importlib.util.spec_from_file_location(
    "mutation_shards", REPO_ROOT / ".github" / "scripts" / "mutation_shards.py"
)
ms = importlib.util.module_from_spec(_MS)
_MS.loader.exec_module(ms)

_SHARD_IDS = [s["id"] for s in ms.expand_shards(REPO_ROOT)]


def _reports(ids: list[str], rate: str = "0.10") -> list[dict]:
    return [{"id": i, "rate": rate, "survivors": []} for i in ids]


def test_complete_set_passes_and_summarizes() -> None:
    lines = agg.aggregate(REPO_ROOT, _reports(_SHARD_IDS))
    assert lines[0] == f"Aggregated {len(_SHARD_IDS)} shard report(s) (one per shard):"
    # one diagnostic line per shard, carrying its rate
    body = lines[1:]
    assert len(body) == len(_SHARD_IDS)
    assert all(" survival rate 0.10" in ln for ln in body)
    # every shard id is represented
    assert {ln.split(":")[0].removeprefix("- ") for ln in body} == set(_SHARD_IDS)


def test_missing_shard_report_fails_loud() -> None:
    with pytest.raises(SystemExit, match="missing="):
        agg.aggregate(REPO_ROOT, _reports(_SHARD_IDS[:-1]))


def test_extra_unexpected_report_fails_loud() -> None:
    with pytest.raises(SystemExit, match="unexpected="):
        agg.aggregate(REPO_ROOT, _reports(_SHARD_IDS + ["not_a_real_shard"]))


def test_duplicate_report_fails_loud() -> None:
    with pytest.raises(SystemExit, match="duplicate shard report"):
        agg.aggregate(REPO_ROOT, _reports(_SHARD_IDS + _SHARD_IDS[:1]))


def test_rate_passthrough_including_unknown() -> None:
    reports = _reports(_SHARD_IDS)
    reports[0]["rate"] = "unknown"
    lines = agg.aggregate(REPO_ROOT, reports)
    assert any(ln == f"- {_SHARD_IDS[0]}: survival rate unknown" for ln in lines)


# ── loader: reports are read recursively (download unpacks one dir per artifact)
def test_load_reports_reads_nested_dirs(tmp_path: Path) -> None:
    for i, sid in enumerate(_SHARD_IDS[:3]):
        d = tmp_path / f"mutation-report-{sid}"
        d.mkdir()
        (d / f"{sid}.json").write_text(
            json.dumps({"id": sid, "rate": "0.0"}), encoding="utf-8"
        )
    loaded = agg._load_reports(tmp_path)
    assert {r["id"] for r in loaded} == set(_SHARD_IDS[:3])


# ── survivors: the Markdown the daily ledger turns into the mutation issue
def test_survivors_markdown_lists_each_survivor_under_its_module() -> None:
    reports = _reports(["check_b", "check_a"])
    reports[0]["survivors"] = [
        {
            "path": "ci_truth_serum/check_b.py",
            "line": 7,
            "operator": "core/NumberReplacer",
            "definition": "f",
        },
        {
            "path": "ci_truth_serum/check_b.py",
            "line": 9,
            "operator": "core/AddNot",
            "definition": None,
        },
    ]
    assert agg.survivors_markdown(reports) == (
        "### `check_b` (survival rate 0.10)\n\n"
        "- `ci_truth_serum/check_b.py:7` `core/NumberReplacer` in `f`\n"
        "- `ci_truth_serum/check_b.py:9` `core/AddNot`"
    )


def test_survivors_markdown_is_empty_when_every_mutant_died() -> None:
    assert agg.survivors_markdown(_reports(_SHARD_IDS)) == ""

"""Tests for .github/scripts/write-shard-report.py — a shard's survivor list.

It reads `cosmic-ray dump` lines, so these feed it dump lines of the shape
cosmic-ray 8 prints and check which mutants it reports.
"""

import importlib.util
import json

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "write-shard-report.py"
_spec = importlib.util.spec_from_file_location("write_shard_report", _SRC)
wsr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsr)


def _line(outcome: str | None, line: int, definition: str | None = "f") -> str:
    item = {
        "job_id": str(line),
        "mutations": [
            {
                "module_path": "ci_truth_serum/check_a.py",
                "operator_name": "core/AddNot",
                "occurrence": 0,
                "start_pos": [line, 4],
                "end_pos": [line, 9],
                "operator_args": {},
                "definition_name": definition,
            }
        ],
    }
    result = (
        None
        if outcome is None
        else {
            "worker_outcome": "normal",
            "output": "",
            "test_outcome": outcome,
            "diff": "",
        }
    )
    return json.dumps([item, result])


def test_only_survived_mutants_are_reported_in_line_order() -> None:
    lines = [
        _line("killed", 3),
        _line("survived", 9, None),
        _line(None, 5),
        _line("incompetent", 6),
        _line("survived", 2),
    ]
    assert wsr.survivors(lines + [""]) == [
        {
            "path": "ci_truth_serum/check_a.py",
            "line": 2,
            "operator": "core/AddNot",
            "definition": "f",
        },
        {
            "path": "ci_truth_serum/check_a.py",
            "line": 9,
            "operator": "core/AddNot",
            "definition": None,
        },
    ]


def test_a_clean_dump_reports_no_survivors() -> None:
    assert wsr.survivors([_line("killed", 3)]) == []

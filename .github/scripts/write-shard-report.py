#!/usr/bin/env python3
"""Write one mutation shard's report: its id, survival rate and survivors.

Reads `cosmic-ray dump` on stdin, one JSON `[work_item, result]` pair per line,
and writes `{id, rate, survivors}` to OUT. Each survivor names the file, line,
operator and function of a mutant the module's own suite did not kill.

Usage: cosmic-ray dump SESSION | write-shard-report.py ID RATE OUT
"""

import json
import sys
from collections.abc import Iterable
from pathlib import Path


def survivors(dump_lines: Iterable[str]) -> list[dict]:
    """The mutants in DUMP_LINES whose test run SURVIVED, sorted by place."""
    found = []
    for line in dump_lines:
        if not line.strip():
            continue
        item, result = json.loads(line)
        if result is None or result["test_outcome"] != "survived":
            continue
        for mutation in item["mutations"]:
            found.append(
                {
                    "path": mutation["module_path"],
                    "line": mutation["start_pos"][0],
                    "operator": mutation["operator_name"],
                    "definition": mutation.get("definition_name"),
                }
            )
    return sorted(found, key=lambda s: (s["path"], s["line"], s["operator"]))


def main(argv: list[str]) -> None:
    shard_id, rate, out = argv
    report = {"id": shard_id, "rate": rate, "survivors": survivors(sys.stdin)}
    Path(out).write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])

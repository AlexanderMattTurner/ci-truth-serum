#!/usr/bin/env python3
"""Expand the cosmic-ray mutation run into one parallel shard per hook module.

A single cosmic-ray pass over the whole ``hooks/`` package reruns the offline
suite once per mutant, serially — minutes of wall-clock. This slices that work
the way ``agent-input-sanitizer`` slices its Stryker run: derive the shard set
from the tree at CI time (no hand-maintained tiling that can drift), fan the
slices across parallel runners, and let a separate aggregate step demand one
report per shard so a vanished slice can never score a subset as the whole.

Each shard mutates exactly ONE module (``hooks/check_x.py``) and runs only that
module's own example suite (``tests/cts/test_check_x.py``) as the oracle. Scoping
the per-mutant test command to the module's own suite is both the speed lever
(one small test file per mutant instead of the entire ``tests/cts`` tree) and the
right granularity for the question mutation testing actually asks — *does this
module's own tests assert its behaviour* — so cross-module / fuzz-only kills that
a whole-suite run would credit are intentionally out of a shard's score.

The mutated set and the exclusions are read from ``cosmic-ray.toml`` (the SSOT):
whatever that file mutates, this shards; whatever it excludes (the IO-only
orchestrators with no offline oracle), this skips. A newly added hook module
automatically gets its own shard — and, because every shard's oracle is
``test_<module>.py``, a module without that suite fails expansion loudly rather
than shipping an unmutated-or-untested slice.

Usage:
    python .github/scripts/mutation_shards.py              # print shard matrix JSON
    python .github/scripts/mutation_shards.py --write-config <id>  # write cosmic-ray.shard.toml
"""

import json
import math
import sys
from pathlib import Path

import tomllib

CONFIG = "cosmic-ray.toml"
SHARD_CONFIG = "cosmic-ray.shard.toml"
# Every shard's per-mutant oracle is the module's own example suite under this
# dir; the base test-command in cosmic-ray.toml targets the whole tree, and a
# shard narrows it to one file (see _scoped_test_command).
TEST_DIR = "tests/cts"

# A module larger than this many source lines is split into ceil(lines / this)
# sub-shards that each mutate the whole module but run only a disjoint slice of
# its mutants (see run-mutation-shard.sh's work_items partition). cosmic-ray
# emits roughly one mutant per source line and each mutant costs ~1.2 s, so this
# caps a shard at ~150 mutants ≈ ~3 min of exec + setup — comfortably under the
# job's timeout. Line count is a cheap, drift-proof proxy computed at plan time,
# exactly as agent-input-sanitizer's `splitEvery` slices its big files. Below the
# cap a module is a single shard whose id is the bare module stem.
SPLIT_EVERY_LINES = 150


def _base_config(repo_root: Path) -> dict:
    return tomllib.loads((repo_root / CONFIG).read_text(encoding="utf-8"))


def _test_file(stem: str) -> str:
    """The example suite that is a module's mutation oracle. `hooks/check_x.py`
    -> `tests/cts/test_check_x.py`; the shared `hooks/_linecheck.py` ->
    `tests/cts/test_linecheck.py` (the leading underscore is dropped, matching
    the committed test filename)."""
    return f"{TEST_DIR}/test_{stem.lstrip('_')}.py"


def expand_shards(repo_root: Path) -> list[dict]:
    """The mutation shard matrix, id-sorted.

    Reads ``cosmic-ray.toml`` for the mutated package (``module-path``) and the
    modules it excludes, then emits shards for every remaining ``hooks/*.py``.
    A module up to ``SPLIT_EVERY_LINES`` lines is one shard ``{id=stem, index=0,
    total=1}``; a larger module is split into ``ceil(lines / SPLIT_EVERY_LINES)``
    sub-shards ``{id=f"{stem}-{k+1}", index=k, total=N}`` that each mutate the
    whole module but run a disjoint ``rowid % N == index`` slice of its mutants.
    Each shard also carries the ``tests`` oracle it runs. Raises if a module's
    ``test_<module>.py`` oracle is missing — a new hook must bring the suite its
    shard will run, or expansion fails loud rather than gate on an empty slice.
    """
    cfg = _base_config(repo_root)["cosmic-ray"]
    package = cfg["module-path"]
    excluded = set(cfg.get("excluded-modules", []))

    shards = []
    for path in sorted((repo_root / package).glob("*.py")):
        module = f"{package}/{path.name}"
        if module in excluded:
            continue
        tests = _test_file(path.stem)
        if not (repo_root / tests).is_file():
            raise FileNotFoundError(
                f"{module} has no mutation oracle at {tests}: every mutated hook "
                f"needs its own example suite (add it, or exclude the module in {CONFIG})."
            )
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        total = max(1, math.ceil(line_count / SPLIT_EVERY_LINES))
        for index in range(total):
            shard_id = path.stem if total == 1 else f"{path.stem}-{index + 1}"
            shards.append(
                {
                    "id": shard_id,
                    "module": module,
                    "tests": tests,
                    "index": index,
                    "total": total,
                }
            )
    if not shards:
        raise ValueError(f"no mutable modules found under {package}/ in {CONFIG}")
    return sorted(shards, key=lambda s: s["id"])


def _scoped_test_command(base: str, tests: str) -> str:
    """The base test-command with its whole-tree target narrowed to one suite.

    The base (SSOT for the pytest flags) targets ``tests/cts``; a shard swaps
    that trailing target for its own ``test_<module>.py`` so a mutant reruns one
    small file, not the tree. A base that does not end in the tree dir is a
    config drift and fails loud."""
    suffix = f" {TEST_DIR}"
    if not base.endswith(suffix):
        raise ValueError(
            f"{CONFIG} test-command must end in {suffix!r} so a shard can scope it, got {base!r}"
        )
    return f"{base[: -len(suffix)]} {tests}"


def _toml_str_array(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v) for v in values) + "]"


def shard_config_toml(repo_root: Path, shard: dict) -> str:
    """A single-module cosmic-ray config for SHARD, derived from the base config.

    Inherits the base ``timeout`` and operator filter (SSOT); overrides
    ``module-path`` to the one module, empties ``excluded-modules`` (nothing to
    exclude in a single file), and scopes ``test-command`` to the module's suite.
    """
    cfg = _base_config(repo_root)["cosmic-ray"]
    timeout = cfg["timeout"]
    test_command = _scoped_test_command(cfg["test-command"], shard["tests"])
    exclude_operators = (
        cfg.get("filters", {}).get("operators-filter", {}).get("exclude-operators", [])
    )
    return (
        "# Generated per-shard config — do not edit; see mutation_shards.py.\n"
        "[cosmic-ray]\n"
        f"module-path = {json.dumps(shard['module'])}\n"
        "excluded-modules = []\n"
        f"timeout = {timeout!r}\n"
        f"test-command = {json.dumps(test_command)}\n"
        "\n"
        "[cosmic-ray.distributor]\n"
        'name = "local"\n'
        "\n"
        "[cosmic-ray.filters.operators-filter]\n"
        f"exclude-operators = {_toml_str_array(exclude_operators)}\n"
    )


def _write_config(repo_root: Path, shard_id: str) -> Path:
    shard = next((s for s in expand_shards(repo_root) if s["id"] == shard_id), None)
    if shard is None:
        raise SystemExit(f"unknown shard id {shard_id!r}")
    dest = repo_root / SHARD_CONFIG
    dest.write_text(shard_config_toml(repo_root, shard), encoding="utf-8")
    return dest


def main(argv: list[str]) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    if argv[:1] == ["--write-config"]:
        if len(argv) != 2:
            raise SystemExit("usage: mutation_shards.py --write-config <id>")
        print(_write_config(repo_root, argv[1]))
        return
    if argv:
        raise SystemExit("usage: mutation_shards.py [--write-config <id>]")
    print(json.dumps(expand_shards(repo_root)))


if __name__ == "__main__":
    main(sys.argv[1:])

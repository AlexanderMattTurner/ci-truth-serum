#!/usr/bin/env python3
"""Expand the cosmic-ray mutation run into one parallel shard per hook module.

A single cosmic-ray pass over the whole ``ci_truth_serum/`` package reruns the offline
suite once per mutant, serially — minutes of wall-clock. This slices that work
the way ``agent-input-sanitizer`` slices its Stryker run: derive the shard set
from the tree at CI time (no hand-maintained tiling that can drift), fan the
slices across parallel runners, and let a separate aggregate step demand one
report per shard so a vanished slice can never score a subset as the whole.

Each shard mutates exactly ONE module (``ci_truth_serum/check_x.py``) and runs only that
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

Each shard also carries a ``hash`` of its own inputs, which the workflow uses as
the exact cache key for the shard's cosmic-ray session (see ``shard_inputs``).
That is what makes the run incremental: a push re-runs the shards for the
modules it touched and restores the verdicts of the rest.

Usage:
    python .github/scripts/mutation_shards.py              # print shard matrix JSON
    python .github/scripts/mutation_shards.py --write-config <id>  # write cosmic-ray.shard.toml
"""

import ast
import hashlib
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
# caps a shard at ~200 mutants ≈ ~4 min of exec + setup — comfortably under the
# job's timeout. Line count is a cheap, drift-proof proxy computed at plan time,
# exactly as agent-input-sanitizer's `splitEvery` slices its big files. Below the
# cap a module is a single shard whose id is the bare module stem.
#
# The value also trades against MATRIX_JOB_LIMIT below: a smaller slice buys
# wall-clock and spends matrix slots. At 150 the pack expanded to 257 shards and
# crossed that limit, which produced no shard jobs at all.
SPLIT_EVERY_LINES = 200

# GitHub runs at most 256 jobs from one matrix. Over that it starts NONE of them,
# and the shard job is skipped rather than failed, so the aggregate step then
# demands reports for a matrix that never ran and dies on a missing directory —
# a failure that names neither the limit nor the cause. Fail here instead, where
# the number and the remedy are both in hand.
MATRIX_JOB_LIMIT = 256

# Files every shard's oracle loads whichever module it mutates: the pytest
# conftest, the shared test helpers, and the fixture tree the example suites
# read. A change to any of them can change a mutant's verdict, so they are
# inputs to every shard's hash.
SHARED_TEST_INPUTS = (
    "tests/__init__.py",
    "tests/conftest.py",
    "tests/_helpers.py",
    f"{TEST_DIR}/__init__.py",
)
SHARED_TEST_DIRS = (f"{TEST_DIR}/fixtures",)
# The harness a shard runs through: the config every shard config derives from,
# the interpreter and dependency pins the oracle runs under, and the two scripts
# that plan and execute a shard. A change to any of them can change what a
# mutant does, so it must invalidate every session.
HARNESS_INPUTS = (
    CONFIG,
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    ".github/scripts/mutation_shards.py",
    ".github/scripts/run-mutation-shard.sh",
)


def _base_config(repo_root: Path) -> dict:
    return tomllib.loads((repo_root / CONFIG).read_text(encoding="utf-8"))


def _test_file(stem: str) -> str:
    """The example suite that is a module's mutation oracle. `ci_truth_serum/check_x.py`
    -> `tests/cts/test_check_x.py`; the shared `ci_truth_serum/_cts_linecheck.py` ->
    `tests/cts/test_cts_linecheck.py` (the leading underscore is dropped, matching
    the committed test filename)."""
    return f"{TEST_DIR}/test_{stem.lstrip('_')}.py"


def _direct_dependencies(repo_root: Path, module: str) -> set[str]:
    """The package's own modules that MODULE imports.

    A check module reaches its shared helpers as ``from _cts_linecheck import …``
    after putting its own directory on ``sys.path``, so an imported top-level
    name is a local dependency exactly when a sibling ``.py`` of that name
    exists. Every other imported name is a third-party or stdlib package, whose
    version the dependency pins in ``HARNESS_INPUTS`` already cover.
    """
    package = Path(module).parent
    tree = ast.parse((repo_root / module).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
    return {
        str(package / f"{name}.py")
        for name in names
        if (repo_root / package / f"{name}.py").is_file()
    }


def _dependency_closure(repo_root: Path, module: str) -> set[str]:
    """MODULE and every package module it transitively imports.

    A mutant's verdict depends on the code its oracle actually executes, so the
    shared helpers a check imports are inputs to that check's shard as much as
    the check itself. Taking the closure rather than the whole package is what
    keeps a change to one check from invalidating the other 80 shards.
    """
    seen = {module}
    stack = [module]
    while stack:
        for dependency in sorted(_direct_dependencies(repo_root, stack.pop())):
            if dependency not in seen:
                seen.add(dependency)
                stack.append(dependency)
    return seen


def shard_inputs(repo_root: Path, shard: dict) -> list[str]:
    """Every file whose content can change one of SHARD's mutant verdicts.

    This is the safety property behind the session cache: a cosmic-ray session
    records a verdict per mutant and cannot re-validate one, so reusing a
    session is sound only when nothing the run depended on has moved. The list
    is therefore deliberately over-inclusive at the edges — the whole fixture
    tree, the dependency lock — because a missing input scores a stale verdict
    as current, while a needless one only costs a re-run.
    """
    paths = set(_dependency_closure(repo_root, shard["module"]))
    paths.add(shard["tests"])
    paths.update(SHARED_TEST_INPUTS)
    paths.update(HARNESS_INPUTS)
    for directory in SHARED_TEST_DIRS:
        paths.update(
            str(path.relative_to(repo_root))
            for path in (repo_root / directory).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return sorted(path for path in paths if (repo_root / path).is_file())


def shard_hash(
    repo_root: Path, shard: dict, digests: dict[str, bytes] | None = None
) -> str:
    """A content digest of SHARD's inputs, used as its session cache key.

    Two runs share a session only when they would compute the same verdicts.
    DIGESTS memoizes per-file hashes across the shards of one expansion; pass
    None for a fresh read of the tree.
    """
    if digests is None:
        digests = {}
    digest = hashlib.sha256()
    # The generated config carries the mutated module, the scoped test command,
    # the timeout and the operator filter; index/total carry the mutant slice.
    digest.update(shard_config_toml(repo_root, shard).encode("utf-8"))
    digest.update(f"\0slice {shard['index']}/{shard['total']}\0".encode("utf-8"))
    for path in shard_inputs(repo_root, shard):
        if path not in digests:
            digests[path] = hashlib.sha256((repo_root / path).read_bytes()).digest()
        digest.update(path.encode("utf-8"))
        digest.update(digests[path])
    return digest.hexdigest()[:16]


def expand_shards(repo_root: Path) -> list[dict]:
    """The mutation shard matrix, id-sorted.

    Reads ``cosmic-ray.toml`` for the mutated package (``module-path``) and the
    modules it excludes, then emits shards for every remaining ``ci_truth_serum/*.py``.
    A module up to ``SPLIT_EVERY_LINES`` lines is one shard ``{id=stem, index=0,
    total=1}``; a larger module is split into ``ceil(lines / SPLIT_EVERY_LINES)``
    sub-shards ``{id=f"{stem}-{k+1}", index=k, total=N}`` that each mutate the
    whole module but run a disjoint ``rowid % N == index`` slice of its mutants.
    Each shard also carries the ``tests`` oracle it runs and the ``hash`` of its
    inputs, which the workflow uses as its session cache key. Raises if a module's
    ``test_<module>.py`` oracle is missing — a new hook must bring the suite its
    shard will run, or expansion fails loud rather than gate on an empty slice.
    Raises too when the set outgrows ``MATRIX_JOB_LIMIT``, because a matrix over
    that size starts no jobs at all.
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
    if len(shards) > MATRIX_JOB_LIMIT:
        raise ValueError(
            f"{len(shards)} shards exceeds GitHub's {MATRIX_JOB_LIMIT}-job matrix limit. "
            f"Raise SPLIT_EVERY_LINES (now {SPLIT_EVERY_LINES}) so fewer modules split, "
            f"or exclude a module in {CONFIG}."
        )
    digests: dict[str, bytes] = {}
    for shard in shards:
        shard["hash"] = shard_hash(repo_root, shard, digests)
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

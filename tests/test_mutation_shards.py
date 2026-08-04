"""Tests for .github/scripts/mutation_shards.py — the cosmic-ray shard expander.

Drives the pure functions directly (no cosmic-ray) and pins the SSOT contract:
the shards cover exactly the mutated ``ci_truth_serum/*.py`` (per cosmic-ray.toml) minus
its exclusions, a large module splits into a complete indexed mutant-partition,
and every shard's generated config is valid TOML that mutates one module and
scopes the suite to that module.
"""

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
import tomllib

from tests._helpers import REPO_ROOT

_SRC = REPO_ROOT / ".github" / "scripts" / "mutation_shards.py"
_spec = importlib.util.spec_from_file_location("mutation_shards", _SRC)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_COSMIC = tomllib.loads((REPO_ROOT / "cosmic-ray.toml").read_text())["cosmic-ray"]


def _mutated_modules() -> set[str]:
    """The `ci_truth_serum/*.py` cosmic-ray.toml mutates: the package minus its exclusions.
    Computed independently of the code under test so the contract is a real check."""
    package = _COSMIC["module-path"]
    excluded = set(_COSMIC["excluded-modules"])
    return {
        f"{package}/{p.name}"
        for p in (REPO_ROOT / package).glob("*.py")
        if f"{package}/{p.name}" not in excluded
    }


# ── the SSOT contract: shards COVER exactly the mutated modules ────────────
def test_shards_cover_exactly_the_mutated_modules() -> None:
    shards = mod.expand_shards(REPO_ROOT)
    assert {s["module"] for s in shards} == _mutated_modules()
    # ids are unique across all shards (sub-shards included)
    assert len({s["id"] for s in shards}) == len(shards)


def test_excluded_modules_get_no_shard() -> None:
    modules = {s["module"] for s in mod.expand_shards(REPO_ROOT)}
    for excluded in _COSMIC["excluded-modules"]:
        assert excluded not in modules, f"{excluded} is excluded but got a shard"


def test_every_shard_oracle_exists_and_is_id_sorted() -> None:
    shards = mod.expand_shards(REPO_ROOT)
    assert shards == sorted(shards, key=lambda s: s["id"])
    for shard in shards:
        assert (REPO_ROOT / shard["tests"]).is_file()
        assert shard["module"].startswith(f"{_COSMIC['module-path']}/")
        # the oracle is the module's own suite regardless of sub-shard suffix
        assert shard["tests"] == mod._test_file(Path(shard["module"]).stem)


def test_expand_is_deterministic() -> None:
    assert mod.expand_shards(REPO_ROOT) == mod.expand_shards(REPO_ROOT)


# ── sub-sharding: a module's slices partition its mutants disjointly ────────
def test_sub_shards_form_a_complete_indexed_partition() -> None:
    by_module: dict[str, list[dict]] = {}
    for s in mod.expand_shards(REPO_ROOT):
        by_module.setdefault(s["module"], []).append(s)
    for module, subs in by_module.items():
        total = subs[0]["total"]
        assert all(s["total"] == total for s in subs)
        # every residue class 0..total-1 appears exactly once — disjoint + complete
        assert sorted(s["index"] for s in subs) == list(range(total))
        stem = Path(module).stem
        if total == 1:
            assert [s["id"] for s in subs] == [stem]
        else:
            assert {s["id"] for s in subs} == {f"{stem}-{k + 1}" for k in range(total)}


def test_total_matches_line_count_ceiling() -> None:
    for s in mod.expand_shards(REPO_ROOT):
        lines = len((REPO_ROOT / s["module"]).read_text(encoding="utf-8").splitlines())
        assert s["total"] == max(1, math.ceil(lines / mod.SPLIT_EVERY_LINES))


# ── the oracle-file convention ────────────────────────────────────
@pytest.mark.parametrize(
    "stem,expected",
    [
        ("check_pipefail_grep_pipe", "tests/cts/test_check_pipefail_grep_pipe.py"),
        ("check_pr_paths", "tests/cts/test_check_pr_paths.py"),
        # the shared lib drops its leading underscore to match the committed file
        ("_linecheck", "tests/cts/test_linecheck.py"),
    ],
)
def test_test_file_convention(stem: str, expected: str) -> None:
    assert mod._test_file(stem) == expected


# ── fail-loud on a hostile / incomplete tree ──────────────────────────
def _write_min_repo(root: Path, *, modules: list[str], tests: list[str]) -> None:
    (root / "ci_truth_serum").mkdir()
    (root / "tests" / "cts").mkdir(parents=True)
    for m in modules:
        (root / "ci_truth_serum" / m).write_text("x = 1\n")
    for t in tests:
        (root / "tests" / "cts" / t).write_text("def test_x(): pass\n")
    (root / "cosmic-ray.toml").write_text(
        "[cosmic-ray]\n"
        'module-path = "ci_truth_serum"\n'
        'excluded-modules = ["ci_truth_serum/__init__.py"]\n'
        "timeout = 60.0\n"
        'test-command = "python -m pytest -x -q -p no:cacheprovider tests/cts"\n'
        '\n[cosmic-ray.distributor]\nname = "local"\n'
        "\n[cosmic-ray.filters.operators-filter]\nexclude-operators = []\n"
    )


def test_module_without_oracle_raises(tmp_path: Path) -> None:
    _write_min_repo(
        tmp_path,
        modules=["__init__.py", "check_a.py", "check_b.py"],
        tests=["test_check_a.py"],  # check_b has no oracle
    )
    with pytest.raises(FileNotFoundError, match=r"check_b\.py has no mutation oracle"):
        mod.expand_shards(tmp_path)


def test_empty_package_raises(tmp_path: Path) -> None:
    _write_min_repo(tmp_path, modules=["__init__.py"], tests=[])
    with pytest.raises(ValueError, match="no mutable modules"):
        mod.expand_shards(tmp_path)


def test_large_module_splits_into_line_capped_sub_shards(tmp_path: Path) -> None:
    # A module just over 2x the line cap must split into 3 sub-shards indexed 0..2.
    lines = mod.SPLIT_EVERY_LINES * 2 + 1
    _write_min_repo(tmp_path, modules=["__init__.py", "check_big.py"], tests=[])
    (tmp_path / "ci_truth_serum" / "check_big.py").write_text("x = 1\n" * lines)
    (tmp_path / "tests" / "cts" / "test_check_big.py").write_text("def test(): pass\n")
    shards = mod.expand_shards(tmp_path)
    assert [s["id"] for s in shards] == ["check_big-1", "check_big-2", "check_big-3"]
    assert all(s["total"] == 3 for s in shards)
    assert [s["index"] for s in shards] == [0, 1, 2]
    # a module at exactly the cap stays a single bare-stem shard
    (tmp_path / "ci_truth_serum" / "check_small.py").write_text(
        "x = 1\n" * mod.SPLIT_EVERY_LINES
    )
    (tmp_path / "tests" / "cts" / "test_check_small.py").write_text(
        "def test(): pass\n"
    )
    small = [
        s for s in mod.expand_shards(tmp_path) if s["module"].endswith("check_small.py")
    ]
    assert len(small) == 1
    assert {k: v for k, v in small[0].items() if k != "hash"} == {
        "id": "check_small",
        "module": "ci_truth_serum/check_small.py",
        "tests": "tests/cts/test_check_small.py",
        "index": 0,
        "total": 1,
    }


# ── the shard hash: the safety property behind the session cache ───────────
#
# The workflow reuses a shard's cosmic-ray session whenever the hash is
# unchanged, and cosmic-ray cannot re-validate a recorded verdict. So the hash
# must move for every file that can change a verdict — that is the property
# these drive — while staying still for the files that cannot, which is the only
# reason the cache saves anything.
def _write_hashable_repo(root: Path) -> None:
    """A minimal tree carrying the shared and harness files a shard hash reads.

    ``check_a`` imports ``shared``; ``check_b`` imports nothing. That split is
    what lets a test tell a per-module hash apart from a whole-package one.
    """
    _write_min_repo(
        root,
        modules=["check_a.py", "check_b.py", "shared.py"],
        tests=["test_check_a.py", "test_check_b.py", "test_shared.py"],
    )
    (root / "ci_truth_serum" / "check_a.py").write_text("from shared import helper\n")
    (root / "ci_truth_serum" / "shared.py").write_text("helper = 1\n")
    (root / ".github" / "scripts").mkdir(parents=True)
    (root / "tests" / "cts" / "fixtures" / "consumer").mkdir(parents=True)
    for path, text in {
        "tests/__init__.py": "",
        "tests/conftest.py": "# conftest\n",
        "tests/_helpers.py": "REPO_ROOT = 1\n",
        "tests/cts/__init__.py": "",
        "tests/cts/fixtures/consumer/sample.yaml": "on: push\n",
        "pyproject.toml": "[project]\nname = 'x'\n",
        "uv.lock": "version = 1\n",
        ".python-version": "3.12\n",
        ".github/scripts/mutation_shards.py": "# planner\n",
        ".github/scripts/run-mutation-shard.sh": "# runner\n",
    }.items():
        (root / path).write_text(text)


def _hashes(root: Path) -> dict[str, str]:
    return {s["id"]: s["hash"] for s in mod.expand_shards(root)}


def _append(root: Path, path: str) -> None:
    """Change PATH's bytes without changing what the tree means."""
    target = root / path
    target.write_bytes(target.read_bytes() + b"\n# edited\n")


def test_hash_is_stable_when_the_tree_does_not_move(tmp_path: Path) -> None:
    _write_hashable_repo(tmp_path)
    assert _hashes(tmp_path) == _hashes(tmp_path)


@pytest.mark.parametrize(
    "edited,expected",
    [
        # a check module and its oracle reach exactly one shard — the common
        # pull request, and the whole reason the cache pays off
        ("ci_truth_serum/check_a.py", {"check_a"}),
        ("tests/cts/test_check_a.py", {"check_a"}),
        ("ci_truth_serum/check_b.py", {"check_b"}),
        # an imported helper reaches its own shard and its importers, and NOT
        # the module that does not import it
        ("ci_truth_serum/shared.py", {"shared", "check_a"}),
    ],
)
def test_an_edit_invalidates_exactly_the_shards_that_read_it(
    tmp_path: Path, edited: str, expected: set[str]
) -> None:
    _write_hashable_repo(tmp_path)
    before = _hashes(tmp_path)
    _append(tmp_path, edited)
    after = _hashes(tmp_path)
    assert {k for k in before if before[k] != after[k]} == expected


@pytest.mark.parametrize(
    "edited",
    [
        "tests/conftest.py",
        "tests/_helpers.py",
        "tests/cts/fixtures/consumer/sample.yaml",
        "cosmic-ray.toml",
        "uv.lock",
        "pyproject.toml",
        ".python-version",
        ".github/scripts/mutation_shards.py",
        ".github/scripts/run-mutation-shard.sh",
    ],
)
def test_a_shared_or_harness_edit_invalidates_every_shard(
    tmp_path: Path, edited: str
) -> None:
    _write_hashable_repo(tmp_path)
    before = _hashes(tmp_path)
    _append(tmp_path, edited)
    after = _hashes(tmp_path)
    assert all(before[k] != after[k] for k in before), (
        f"{edited} left some shard's session reusable: "
        f"{sorted(k for k in before if before[k] == after[k])}"
    )


def test_sub_shards_of_one_module_get_distinct_hashes() -> None:
    """Two slices of one module share every input file but run different mutants.

    A shared key would let one slice restore the other's session and report a
    disjoint set of verdicts as its own.
    """
    shards = mod.expand_shards(REPO_ROOT)
    split = [s for s in shards if s["total"] > 1]
    assert split, "no split module in the tree to check"
    assert len({s["hash"] for s in shards}) == len(shards)


def test_shard_inputs_carry_the_module_its_oracle_and_the_harness() -> None:
    module = "ci_truth_serum/check_pipefail_grep_pipe.py"
    shards = [s for s in mod.expand_shards(REPO_ROOT) if s["module"] == module]
    assert shards, f"{module} has no shard to inspect"
    shard = shards[0]
    inputs = set(mod.shard_inputs(REPO_ROOT, shard))
    assert shard["module"] in inputs
    assert shard["tests"] in inputs
    assert set(mod.HARNESS_INPUTS) <= inputs
    # every input is a real file, so a hash never silently skips one
    assert all((REPO_ROOT / path).is_file() for path in inputs)


# ── the generated per-shard config ────────────────────────────────
def test_shard_config_is_valid_single_module_toml() -> None:
    shard = next(
        s
        for s in mod.expand_shards(REPO_ROOT)
        if s["module"] == "ci_truth_serum/check_pipefail_grep_pipe.py"
    )
    parsed = tomllib.loads(mod.shard_config_toml(REPO_ROOT, shard))["cosmic-ray"]
    # the config mutates the WHOLE module (the sub-shard slices mutants at runtime)
    assert parsed["module-path"] == shard["module"]
    assert parsed["excluded-modules"] == []
    # timeout + operator filter inherited from the base config (SSOT)
    assert parsed["timeout"] == _COSMIC["timeout"]
    # the test-command is scoped to this module's suite, keeping the base flags
    assert parsed["test-command"].endswith(f" {shard['tests']}")
    assert "tests/cts/test_check_pipefail_grep_pipe.py" in parsed["test-command"]
    # and it is NOT the whole-tree target any more
    assert not parsed["test-command"].endswith(" tests/cts")


def test_scoped_test_command_narrows_target() -> None:
    base = "python -m pytest -x -q -p no:cacheprovider tests/cts"
    assert (
        mod._scoped_test_command(base, "tests/cts/test_check_a.py")
        == "python -m pytest -x -q -p no:cacheprovider tests/cts/test_check_a.py"
    )


def test_scoped_test_command_rejects_unexpected_base() -> None:
    with pytest.raises(ValueError, match="must end in"):
        mod._scoped_test_command("pytest somewhere/else", "tests/cts/test_x.py")


# ── CLI ───────────────────────────────────────────────────
def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SRC), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cli_prints_matrix_json_matching_expand() -> None:
    out = _run_cli()
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == mod.expand_shards(REPO_ROOT)


def test_cli_write_config_writes_parseable_toml(tmp_path: Path) -> None:
    # --write-config writes cosmic-ray.shard.toml at the repo root; run it against
    # a copied minimal repo so the real tree is untouched.
    _write_min_repo(tmp_path, modules=["check_a.py"], tests=["test_check_a.py"])
    (tmp_path / ".github" / "scripts").mkdir(parents=True)
    dest = tmp_path / ".github" / "scripts" / "mutation_shards.py"
    dest.write_text(_SRC.read_text())
    result = subprocess.run(
        [sys.executable, str(dest), "--write-config", "check_a"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    written = (tmp_path / "cosmic-ray.shard.toml").read_text()
    assert (
        tomllib.loads(written)["cosmic-ray"]["module-path"]
        == "ci_truth_serum/check_a.py"
    )


def test_cli_write_config_unknown_id_fails() -> None:
    out = _run_cli("--write-config", "does_not_exist")
    assert out.returncode != 0
    assert "unknown shard id" in out.stderr

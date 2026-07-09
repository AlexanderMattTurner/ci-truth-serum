"""Tests for .github/scripts/mutation_shards.py — the cosmic-ray shard expander.

Drives the pure functions directly (no cosmic-ray) and pins the SSOT contract:
the shard set is exactly the mutated ``hooks/*.py`` (per cosmic-ray.toml) minus
its exclusions, one shard each, and every shard's generated config is valid TOML
that mutates one module and scopes the suite to that module.
"""

import importlib.util
import json
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
    """The `hooks/*.py` cosmic-ray.toml mutates: the package minus its exclusions.
    Computed independently of the code under test so the contract is a real check."""
    package = _COSMIC["module-path"]
    excluded = set(_COSMIC["excluded-modules"])
    return {
        f"{package}/{p.name}"
        for p in (REPO_ROOT / package).glob("*.py")
        if f"{package}/{p.name}" not in excluded
    }


# ── the SSOT contract: shard set == mutated modules, one each ──────────────
def test_shards_are_exactly_the_mutated_modules() -> None:
    shards = mod.expand_shards(REPO_ROOT)
    assert {s["module"] for s in shards} == _mutated_modules()
    # one shard per module, no dupes
    assert len(shards) == len(_mutated_modules())
    assert len({s["id"] for s in shards}) == len(shards)


def test_excluded_modules_get_no_shard() -> None:
    ids = {s["id"] for s in mod.expand_shards(REPO_ROOT)}
    for excluded in _COSMIC["excluded-modules"]:
        stem = Path(excluded).stem
        assert stem not in ids, f"{stem} is excluded in cosmic-ray.toml but got a shard"


def test_every_shard_oracle_exists_and_is_id_sorted() -> None:
    shards = mod.expand_shards(REPO_ROOT)
    assert shards == sorted(shards, key=lambda s: s["id"])
    for shard in shards:
        assert (REPO_ROOT / shard["tests"]).is_file()
        # module id is the file stem; module path is under the mutated package
        assert shard["module"] == f"{_COSMIC['module-path']}/{shard['id']}.py"


def test_expand_is_deterministic() -> None:
    assert mod.expand_shards(REPO_ROOT) == mod.expand_shards(REPO_ROOT)


# ── the oracle-file convention ─────────────────────────────────────────────
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


# ── fail-loud on a hostile / incomplete tree ───────────────────────────────
def _write_min_repo(root: Path, *, modules: list[str], tests: list[str]) -> None:
    (root / "hooks").mkdir()
    (root / "tests" / "cts").mkdir(parents=True)
    for m in modules:
        (root / "hooks" / m).write_text("x = 1\n")
    for t in tests:
        (root / "tests" / "cts" / t).write_text("def test_x(): pass\n")
    (root / "cosmic-ray.toml").write_text(
        "[cosmic-ray]\n"
        'module-path = "hooks"\n'
        'excluded-modules = ["hooks/__init__.py"]\n'
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


# ── the generated per-shard config ─────────────────────────────────────────
def test_shard_config_is_valid_single_module_toml() -> None:
    shard = next(
        s for s in mod.expand_shards(REPO_ROOT) if s["id"] == "check_pipefail_grep_pipe"
    )
    parsed = tomllib.loads(mod.shard_config_toml(REPO_ROOT, shard))["cosmic-ray"]
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


# ── CLI ────────────────────────────────────────────────────────────────────
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
    assert tomllib.loads(written)["cosmic-ray"]["module-path"] == "hooks/check_a.py"


def test_cli_write_config_unknown_id_fails() -> None:
    out = _run_cli("--write-config", "does_not_exist")
    assert out.returncode != 0
    assert "unknown shard id" in out.stderr

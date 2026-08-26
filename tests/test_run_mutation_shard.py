"""The mutation shard runner must reuse a restored cosmic-ray session.

The workflow restores ``cr-<id>.sqlite`` from a cache keyed on the shard's input
hash before this script runs. The script's job is then to resume that session
instead of rebuilding it: rebuilding throws away every verdict the cache exists
to carry, and the run costs exactly what it cost before the cache.

These drive the real script. ``cosmic-ray`` and its report tools are stubbed —
a real pass takes minutes per shard and this asserts nothing about what cosmic
-ray computes, only which commands the script issues and what it does to the
session file. Everything else runs for real: the shard planner is the committed
one and ``python`` is this interpreter.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "run-mutation-shard.sh"
PLANNER = REPO_ROOT / ".github" / "scripts" / "mutation_shards.py"

# Records its argv, one command per line, and creates the session file on
# `init` — the one side effect the script's control flow turns on.
COSMIC_RAY_STUB = """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"$STUB_LOG"
if [[ "${1:-}" == "init" ]]; then
  python -c 'import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); \
c.execute("CREATE TABLE work_items (job_id TEXT)"); \
c.executemany("INSERT INTO work_items VALUES (?)", [(str(i),) for i in range(10)]); \
c.commit()' "$3"
fi
"""

RATE_STUB = """#!/usr/bin/env bash
echo "$*" >>"$STUB_LOG"
echo "0.25"
"""


def _write_repo(root: Path) -> None:
    """A minimal tree the committed planner can expand one shard from."""
    (root / "ci_truth_serum").mkdir()
    (root / "tests" / "cts").mkdir(parents=True)
    (root / "ci_truth_serum" / "check_a.py").write_text("x = 1\n", encoding="utf-8")
    (root / "tests" / "cts" / "test_check_a.py").write_text(
        "def test_x(): pass\n", encoding="utf-8"
    )
    (root / "cosmic-ray.toml").write_text(
        "[cosmic-ray]\n"
        'module-path = "ci_truth_serum"\n'
        "excluded-modules = []\n"
        "timeout = 60.0\n"
        'test-command = "python -m pytest -x -q -p no:cacheprovider tests/cts"\n'
        '\n[cosmic-ray.distributor]\nname = "local"\n'
        "\n[cosmic-ray.filters.operators-filter]\nexclude-operators = []\n",
        encoding="utf-8",
    )
    scripts = root / ".github" / "scripts"
    scripts.mkdir(parents=True)
    for source in (SCRIPT, PLANNER):
        (scripts / source.name).write_bytes(source.read_bytes())
    (scripts / SCRIPT.name).chmod(0o755)


def _write_stubs(root: Path) -> Path:
    """A PATH dir holding the cosmic-ray stubs and a real `python`."""
    stub_dir = root / "stub-bin"
    stub_dir.mkdir()
    for name, body in (
        ("cosmic-ray", COSMIC_RAY_STUB),
        ("cr-report", RATE_STUB),
        ("cr-rate", RATE_STUB),
    ):
        path = stub_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    (stub_dir / "python").symlink_to(sys.executable)
    return stub_dir


def _run(root: Path, shard_id: str, **extra: str) -> subprocess.CompletedProcess[str]:
    stub_dir = _write_stubs(root)
    env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(root / "commands.log"),
        "SHARD_ID": shard_id,
        **extra,
    }
    return subprocess.run(
        ["bash", str(root / ".github" / "scripts" / SCRIPT.name)],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _commands(root: Path) -> list[str]:
    return (root / "commands.log").read_text(encoding="utf-8").splitlines()


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    _write_repo(tmp_path)
    return tmp_path


def test_a_cold_shard_builds_its_session_and_reports(repo: Path) -> None:
    """With no cached session the run inits one, execs it and writes a report."""
    result = _run(repo, "check_a")
    assert result.returncode == 0, result.stderr
    commands = _commands(repo)
    assert any(c.startswith("init ") for c in commands), commands
    assert any(c.startswith("exec ") for c in commands), commands
    assert (repo / "cr-check_a.sqlite").is_file()
    assert (repo / "reports" / "mutation" / "check_a.json").is_file()


def test_a_restored_session_is_resumed_never_rebuilt(repo: Path) -> None:
    """A session present before the run survives it untouched, and `init` never runs.

    This is the incremental property. Re-initing would discard every verdict the
    restored session carries, so the shard would recompute work the cache had
    already paid for — the cache would then cost storage and save nothing.
    """
    session = repo / "cr-check_a.sqlite"
    session.write_bytes(b"restored-session-bytes")
    result = _run(repo, "check_a")
    assert result.returncode == 0, result.stderr
    commands = _commands(repo)
    assert not any(c.startswith("init ") for c in commands), commands
    assert any(c.startswith("exec ") for c in commands), commands
    assert session.read_bytes() == b"restored-session-bytes"
    assert (repo / "reports" / "mutation" / "check_a.json").is_file()


def test_a_restored_split_shard_keeps_its_mutant_slice(repo: Path) -> None:
    """A resumed sub-shard does not re-slice the session it restored.

    The slice is applied once, at init, against the rows cosmic-ray created. A
    restored session already holds only this sub-shard's rows, so re-applying
    the residue filter would drop most of them and score a fraction of the slice.
    """
    session = repo / "cr-check_a.sqlite"
    session.write_bytes(b"restored-session-bytes")
    result = _run(repo, "check_a", SHARD_INDEX="1", SHARD_TOTAL="3")
    assert result.returncode == 0, result.stderr
    assert session.read_bytes() == b"restored-session-bytes"

"""The sdist self-sufficiency invariant that MANIFEST.in states but never tests.

``MANIFEST.in`` says ``package.json`` must ship in the sdist: ``ci_truth_serum.
_version`` reads it as the release SSOT, so a build from an unpacked sdist that
lacks it cannot resolve its own version. Nothing built an sdist to check that
claim, so a dropped ``include package.json`` line would ship silently.

The build here copies the git-tracked tree into a fresh ``tmp_path`` rather than
building the working repo directly. The working repo can carry a stale
``ci_truth_serum.egg-info/SOURCES.txt`` from an earlier build, and setuptools
reuses that cached file list instead of recomputing it from ``MANIFEST.in`` --
which would let this test pass even with a broken ``MANIFEST.in``, exactly the
gap it exists to close.
"""

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from tests._helpers import REPO_ROOT


def _clean_tree_copy(dest: Path) -> None:
    """Copy every git-tracked file under REPO_ROOT into DEST.

    ``git ls-files`` reads the current working tree's tracked-file list (so
    uncommitted edits to a tracked file are picked up) while skipping anything
    ``.gitignore`` excludes, in particular the ``*.egg-info/`` build cache.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")[:-1]
    for raw in tracked:
        rel = Path(raw.decode())
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, target)


def _build_sdist(src_dir: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["uv", "build", "--sdist", "-o", str(out_dir), str(src_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"sdist build from {src_dir} failed:\n{result.stdout}\n{result.stderr}"
    )
    [tarball] = list(out_dir.glob("*.tar.gz"))
    return tarball


def test_sdist_ships_package_json_and_rebuilds_from_itself(tmp_path: Path) -> None:
    src_tree = tmp_path / "src"
    _clean_tree_copy(src_tree)

    first_tarball = _build_sdist(src_tree, tmp_path / "dist1")
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(first_tarball) as tar:
        tar.extractall(unpacked, filter="data")
    [sdist_root] = list(unpacked.iterdir())

    manifest = sdist_root / "package.json"
    assert manifest.is_file(), (
        "package.json is missing from the sdist. A build from the unpacked "
        "sdist cannot resolve its own version (ci_truth_serum/_version.py "
        "reads this file). Check the `include package.json` line in "
        "MANIFEST.in."
    )
    assert json.loads(manifest.read_text(encoding="utf-8"))["name"] == "ci-truth-serum"

    # The real failure mode: a build run a second time, from inside the
    # unpacked sdist and with no git checkout available, must still succeed.
    _build_sdist(sdist_root, tmp_path / "dist2")

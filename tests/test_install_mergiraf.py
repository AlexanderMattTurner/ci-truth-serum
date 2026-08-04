"""Behavior tests for .github/scripts/install-mergiraf.sh.

The script reads its pinned version and digest from `.github/tool-versions.sh`,
a sibling of its own directory. That file is what the auto-resolve workflow's
conflict resolver depends on, and the failure it produces when absent is silent
in the only place anyone looks: the resolver dies before it merges anything, so
a conflicted pull request simply stays conflicted with no comment on it.

These drive the real script at its real path, so the `source` of the pins
resolves against the real repository tree. A stub `curl` keeps them offline.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

SCRIPT = REPO_ROOT / ".github" / "scripts" / "install-mergiraf.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None and not os.environ.get("CI"),
    reason="sha256sum not available (CI runners must have it: skipping there would silently drop this suite)",
)


def _curl_stub(bindir: Path, payload: bytes) -> None:
    """A `curl` that writes PAYLOAD to whatever `-o FILE` target it is given."""
    bindir.mkdir(parents=True, exist_ok=True)
    payload_file = bindir / "payload.bin"
    payload_file.write_bytes(payload)
    curl = bindir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "out=\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  if [[ "$1" == "-o" ]]; then out="$2"; shift 2; continue; fi\n'
        "  shift\n"
        "done\n"
        '[[ -n "$out" ]] || { echo "stub curl: no -o target" >&2; exit 2; }\n'
        f'cp "{payload_file}" "$out"\n'
    )
    curl.chmod(0o755)


def _run(tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the real installer with a stub curl, installing into TMP_PATH."""
    bindir = tmp_path / "bin"
    _curl_stub(bindir, b"not the pinned tarball")
    dest = tmp_path / "dest"
    dest.mkdir()
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(SCRIPT), str(dest)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )


def test_the_pins_the_installer_sources_are_present_and_non_empty(
    tmp_path: Path,
) -> None:
    """The installer reaches its digest check.

    Two earlier failures are what this rules out: the sourced pins file missing
    from the tree, and the file present but naming no mergiraf version. Both
    stop the script before it ever compares a digest, and both leave the
    conflict resolver dead for every pull request in the repository.
    """
    result = _run(tmp_path)
    assert "No such file or directory" not in result.stderr, (
        "install-mergiraf.sh could not source .github/tool-versions.sh"
    )
    assert "refusing to install an unverified binary" not in result.stderr, (
        "the pins file is present but names no mergiraf version or digest"
    )
    # The stub serves bytes no pin names, so reaching the digest check means
    # refusing them — the fail-closed behavior, and proof the pins were read.
    assert result.returncode != 0
    assert "sha256sum" in result.stderr or "FAILED" in result.stdout + result.stderr


def test_no_binary_is_installed_when_the_digest_does_not_match(
    tmp_path: Path,
) -> None:
    """A tarball the pin does not name never reaches the destination."""
    result = _run(tmp_path)
    assert result.returncode != 0
    assert list((tmp_path / "dest").iterdir()) == []

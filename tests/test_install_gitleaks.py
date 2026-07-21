"""Behavior tests for .github/scripts/install-gitleaks.sh.

The install script must fail closed: it refuses to extract the gitleaks binary
unless the downloaded tarball matches the committed SHA-256, and it refuses to
run at all for a version it has no pinned digest for.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("sha256sum") is None and not os.environ.get("CI"),
    reason="sha256sum not available (CI runners must have it: skipping there would silently drop this suite)",
)


def _install_curl_stub(bindir: Path, payload: bytes) -> None:
    """A `curl` that writes `payload` to whatever `-o FILE` target it's given,
    so the test never touches the network."""
    bindir.mkdir(parents=True, exist_ok=True)
    curl = bindir / "curl"
    payload_file = bindir / "payload.bin"
    payload_file.write_bytes(payload)
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


def _run(repo: Path, bindir: Path, version: str) -> subprocess.CompletedProcess:
    script = repo / "install-gitleaks.sh"
    shutil.copy2(REPO_ROOT / ".github" / "scripts" / "install-gitleaks.sh", script)
    script.chmod(0o755)
    import os

    env = {**os.environ, "GITLEAKS_VERSION": version, "GITLEAKS_DEST": str(repo)}
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(script)], cwd=repo, env=env, capture_output=True, text=True
    )


def test_fails_closed_on_digest_mismatch(tmp_path: Path) -> None:
    """A tampered tarball (wrong bytes) fails the sha256sum check; no binary is
    extracted and the script exits non-zero."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    _install_curl_stub(bindir, b"this is not the real gitleaks tarball")

    result = _run(repo, bindir, "8.30.1")
    assert result.returncode != 0
    assert not (repo / "gitleaks").exists()


def test_rejects_version_without_pinned_digest(tmp_path: Path) -> None:
    """An unknown version has no committed digest, so the script refuses to run
    (fail loud) rather than downloading unverifiable bytes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    bindir = tmp_path / "bin"
    _install_curl_stub(bindir, b"whatever")

    result = _run(repo, bindir, "0.0.0-nonexistent")
    assert result.returncode != 0
    assert "no pinned SHA-256" in (result.stdout + result.stderr)
    assert not (repo / "gitleaks").exists()

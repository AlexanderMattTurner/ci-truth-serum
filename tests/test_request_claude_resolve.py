"""Tests for .github/scripts/request-claude-resolve.sh.

The env-guard (missing PR_NUM) failure direction is covered by
test_required_env.py. This file covers the POSITIVE direction: given a valid
PR_NUM, the script builds the comment body and invokes `gh pr comment` with the
right PR number and body sections. A stub `gh` on PATH captures the argv so we
assert on the real invocation, not on source text.
"""

import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "request-claude-resolve.sh"


def _install_gh_stub(bin_dir: Path, argv_file: Path) -> None:
    """Write a fake `gh` that records its argv NUL-delimited (the comment body is
    multi-line, so a newline delimiter would corrupt the split) and exits 0, so
    the script's `gh pr comment …` call is captured, not executed."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(f'#!/usr/bin/env bash\nprintf "%s\\0" "$@" > {argv_file}\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _read_argv(argv_file: Path) -> list[str]:
    raw = argv_file.read_bytes()
    parts = raw.split(b"\x00")
    if parts and parts[-1] == b"":  # trailing delimiter
        parts.pop()
    return [p.decode() for p in parts]


def _run(bin_dir: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "PR_NUM": "123", **env_extra}
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_posts_comment_to_pr_number(tmp_path: Path) -> None:
    argv_file = tmp_path / "gh_argv.txt"
    _install_gh_stub(tmp_path / "bin", argv_file)

    result = _run(tmp_path / "bin", {})
    assert result.returncode == 0, result.stderr

    argv = _read_argv(argv_file)
    # `gh pr comment 123 --body <body>`
    assert argv[:3] == ["pr", "comment", "123"]
    assert "--body" in argv
    body = argv[argv.index("--body") + 1]
    assert "@claude Resolve this template sync PR" in body


def test_body_includes_conflict_and_deletion_sections(tmp_path: Path) -> None:
    argv_file = tmp_path / "gh_argv.txt"
    _install_gh_stub(tmp_path / "bin", argv_file)

    result = _run(
        tmp_path / "bin",
        {
            "HAS_CONFLICTS": "true",
            "HAS_DELETIONS": "true",
            "CONFLICT_FILES": "config/a.txt config/b.txt",
            "DELETED_FILES": "config/gone.txt",
        },
    )
    assert result.returncode == 0, result.stderr

    argv = _read_argv(argv_file)
    body = argv[argv.index("--body") + 1]
    assert "**Resolve conflicts in:** config/a.txt config/b.txt" in body
    assert "**Deleted files:**" in body
    assert "config/gone.txt" in body


def test_body_omits_sections_when_flags_default(tmp_path: Path) -> None:
    argv_file = tmp_path / "gh_argv.txt"
    _install_gh_stub(tmp_path / "bin", argv_file)

    result = _run(tmp_path / "bin", {})
    assert result.returncode == 0, result.stderr

    argv = _read_argv(argv_file)
    body = argv[argv.index("--body") + 1]
    assert "Resolve conflicts in:" not in body
    assert "Deleted files:" not in body

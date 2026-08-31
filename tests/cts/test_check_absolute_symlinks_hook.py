"""Tests for ci_truth_serum/check_absolute_symlinks.sh — the packaged guard against
a tracked symlink a fresh clone cannot follow.

Two targets qualify: an absolute path, and a path git ignores. The second half is
judged by asking git, so these tests write a real `.gitignore` and let the script
read it, rather than asserting against a list of tool directory names."""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import commit_all


def run_script(repo: Path, copy_script) -> subprocess.CompletedProcess:
    script = copy_script("check_absolute_symlinks.sh", repo)
    return subprocess.run(
        ["bash", str(script)], cwd=repo, capture_output=True, text=True
    )


@pytest.mark.parametrize(
    "setup, expect_pass, expected_violation",
    [
        ("no_symlinks", True, None),
        ("relative_symlink", True, None),
        ("absolute_symlink", False, "link -> /etc/passwd"),
    ],
)
def test_check_absolute_symlinks(
    empty_git_repo: Path,
    copy_script,
    setup: str,
    expect_pass: bool,
    expected_violation: str | None,
) -> None:
    if setup == "no_symlinks":
        (empty_git_repo / "regular.txt").write_text("hi", encoding="utf-8")
    elif setup == "relative_symlink":
        (empty_git_repo / "target.txt").write_text("hi", encoding="utf-8")
        (empty_git_repo / "link").symlink_to("target.txt")
    elif setup == "absolute_symlink":
        (empty_git_repo / "link").symlink_to("/etc/passwd")
    commit_all(empty_git_repo)

    result = run_script(empty_git_repo, copy_script)
    if expect_pass:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert expected_violation in result.stdout + result.stderr


def test_ignores_untracked_absolute_symlink(empty_git_repo: Path, copy_script) -> None:
    """Untracked links aren't anyone else's problem yet."""
    (empty_git_repo / "link").symlink_to("/etc/passwd")
    # Don't commit — link stays untracked.
    result = run_script(empty_git_repo, copy_script)
    assert result.returncode == 0, result.stderr


def _ignored_tree(repo: Path, link_at: str, target: str) -> None:
    """A repo whose `node_modules/` is ignored, with LINK_AT pointing at TARGET."""
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "node_modules" / "pkg" / "bin.js").write_text("x", encoding="utf-8")
    link = repo / link_at
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def test_a_link_into_an_ignored_directory_is_rejected(
    empty_git_repo: Path, copy_script
) -> None:
    """The target is built by a tool and never committed, so the clone dangles."""
    _ignored_tree(empty_git_repo, "link", "node_modules/pkg/bin.js")
    commit_all(empty_git_repo)

    result = run_script(empty_git_repo, copy_script)
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "link -> node_modules/pkg/bin.js" in output
    assert "node_modules/pkg/bin.js" in output


def test_the_target_resolves_against_the_links_own_directory(
    empty_git_repo: Path, copy_script
) -> None:
    """A link one directory down writes `../node_modules/...`.

    Resolving that against the repo root instead would land outside the tree and
    the finding would be skipped — the fail-open direction.
    """
    _ignored_tree(empty_git_repo, "tools/link", "../node_modules/pkg/bin.js")
    commit_all(empty_git_repo)

    result = run_script(empty_git_repo, copy_script)
    assert result.returncode == 1
    assert "tools/link" in result.stdout + result.stderr


def test_a_link_to_a_committed_file_under_an_unignored_path_passes(
    empty_git_repo: Path, copy_script
) -> None:
    """Non-vacuity for the rule above: only the IGNORE verdict makes it fire."""
    (empty_git_repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    (empty_git_repo / "vendor").mkdir()
    (empty_git_repo / "vendor" / "bin.js").write_text("x", encoding="utf-8")
    (empty_git_repo / "link").symlink_to("vendor/bin.js")
    commit_all(empty_git_repo)

    result = run_script(empty_git_repo, copy_script)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_target_outside_the_repository_is_left_alone(
    empty_git_repo: Path, copy_script
) -> None:
    """git has no ignore verdict for a path it does not own, so the script skips
    it rather than guessing."""
    (empty_git_repo / "link").symlink_to("../outside/thing.txt")
    commit_all(empty_git_repo)

    result = run_script(empty_git_repo, copy_script)
    assert result.returncode == 0, result.stdout + result.stderr

"""Behavior tests for .github/scripts/tag-release.sh version-advance detection.

The load-bearing property: a version advance is detected by comparing
package.json's version to the latest existing release tag, NOT by a HEAD~1 diff.
Under rebase-and-merge the bump commit need not be HEAD~1, so a HEAD~1 diff
silently skips the tag — the exact regression these tests pin.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from tests._helpers import REPO_ROOT, commit_all, git_env, init_test_repo

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _write_pkg(repo: Path, version: str) -> None:
    (repo / "package.json").write_text(f'{{"name": "x", "version": "{version}"}}\n')


def _install_gh_stub(bindir: Path, gh_log: Path) -> None:
    """A `gh` that logs its args and reports every release as already existing,
    so tag-release.sh stops after tagging without invoking real release infra."""
    bindir.mkdir(parents=True, exist_ok=True)
    gh = bindir / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{gh_log}"\n'
        # `release view` exit 0 == release already exists -> script exits early.
        "exit 0\n"
    )
    gh.chmod(0o755)


def _make_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A git repo with tag-release.sh + retry.bash wired up, a bare origin, and a
    gh stub. Returns (repo, gh_log, env-PATH-prefixed bindir)."""
    repo = tmp_path / "repo"
    init_test_repo(repo)

    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / ".github" / "scripts" / "tag-release.sh", scripts)
    (scripts / "tag-release.sh").chmod(0o755)
    # tag-release.sh sources changelog-notes.sh only on the release path, which
    # the gh stub short-circuits; copy it anyway so the path resolves.
    shutil.copy2(REPO_ROOT / ".github" / "scripts" / "changelog-notes.sh", scripts)

    libdir = repo / "bin" / "lib"
    libdir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "lib" / "retry.bash", libdir)

    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)

    bindir = tmp_path / "bin"
    gh_log = tmp_path / "gh.log"
    _install_gh_stub(bindir, gh_log)

    _write_pkg(repo, "0.0.0")
    commit_all(repo, "seed")
    subprocess.run(
        ["git", "remote", "add", "origin", str(bare)], cwd=repo, env=git_env(), check=True
    )
    return repo, gh_log, bindir


def _run(repo: Path, bindir: Path) -> subprocess.CompletedProcess:
    env = git_env()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return subprocess.run(
        ["bash", ".github/scripts/tag-release.sh"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def _tags(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "tag", "--list"], cwd=repo, env=git_env(), capture_output=True, text=True
    )
    return set(out.stdout.split())


def _tag(repo: Path, name: str) -> None:
    subprocess.run(["git", "tag", name], cwd=repo, env=git_env(), check=True)


def test_tags_when_version_advances_under_rebase_merge(tmp_path: Path) -> None:
    """The discriminating case: HEAD~1's package.json ALREADY carries the new
    version (as after a rebase-merge that replays the bump before later commits),
    and the latest tag is older. A HEAD~1 diff would see no change and skip; the
    tag-vs-latest logic must still tag."""
    repo, _, bindir = _make_repo(tmp_path)
    _write_pkg(repo, "1.0.0")
    commit_all(repo, "release 1.0.0")
    _tag(repo, "v1.0.0")

    _write_pkg(repo, "1.1.0")
    commit_all(repo, "bump to 1.1.0")  # the bump
    commit_all(repo, "docs: follow-up")  # HEAD~1 now also shows 1.1.0

    result = _run(repo, bindir)
    assert result.returncode == 0, result.stderr
    assert "v1.1.0" in _tags(repo), result.stdout + result.stderr


def test_first_release_tags_when_no_tags_exist(tmp_path: Path) -> None:
    repo, _, bindir = _make_repo(tmp_path)
    _write_pkg(repo, "1.0.0")
    commit_all(repo, "release 1.0.0")

    result = _run(repo, bindir)
    assert result.returncode == 0, result.stderr
    assert "v1.0.0" in _tags(repo)


def test_no_tag_when_version_matches_latest_tag(tmp_path: Path) -> None:
    """An ordinary commit that does not bump the version creates no new tag."""
    repo, _, bindir = _make_repo(tmp_path)
    _write_pkg(repo, "1.0.0")
    commit_all(repo, "release 1.0.0")
    _tag(repo, "v1.0.0")
    commit_all(repo, "docs: unrelated change")

    result = _run(repo, bindir)
    assert result.returncode == 0, result.stderr
    assert _tags(repo) == {"v1.0.0"}


def test_advances_past_highest_tag_not_a_stale_one(tmp_path: Path) -> None:
    """`--sort=-v:refname` picks the highest tag, so 1.10.0 is recognized as newer
    than 1.9.0 (a lexical compare would wrongly call them equal-or-lower)."""
    repo, _, bindir = _make_repo(tmp_path)
    _write_pkg(repo, "1.9.0")
    commit_all(repo, "release 1.9.0")
    _tag(repo, "v1.9.0")
    _write_pkg(repo, "1.10.0")
    commit_all(repo, "bump to 1.10.0")

    result = _run(repo, bindir)
    assert result.returncode == 0, result.stderr
    assert "v1.10.0" in _tags(repo)

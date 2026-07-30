"""Tests for ci_truth_serum/release_canary.py — the apply-side console script that
asserts the max `v*` git tag, the changelog's top dated heading, and (when a
PKGBUILD is present) its `pkgver=` all agree.

Every marker is local, so the tests drive real fixtures — a real git repo with
real tags, real files — rather than injected readers. The one monkeypatch left
records the processes spawned, to pin that the tool stays offline.
"""

import subprocess

import pytest

from tests._helpers import git_env, init_test_repo, load_hook

mod = load_hook("release_canary.py", "release_canary")


# ── semver machinery ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "versions, expected",
    [
        (["1.0.0", "1.10.0", "1.2.0"], "1.10.0"),  # numeric, not lexicographic
        (["5.0.1", "5.0.10", "5.0.9"], "5.0.10"),
        (["v1.0.0", "2.0.0"], "2.0.0"),
        (["1.0.0", "1.0.1-rc.1"], "1.0.1-rc.1"),
        (["1.0.1-rc.1", "1.0.1"], "1.0.1"),  # release outranks its pre-release
        (["1.0.0", "junk", ""], "1.0.0"),  # non-semver entries ignored
        (["junk"], None),
        ([], None),
    ],
)
def test_max_semver(versions: list[str], expected: str | None) -> None:
    assert mod.max_semver(versions) == expected


# ── changelog parsing ────────────────────────────────────────────────────
def test_changelog_top_version_skips_unreleased() -> None:
    text = "# Changelog\n\n## Unreleased\n\n## [1.4.0] - 2026-07-01\n\n## [1.3.0]\n"
    assert mod.changelog_top_version(text) == "1.4.0"


@pytest.mark.parametrize(
    "heading, expected",
    [
        ("## [1.2.3] - 2026-01-01", "1.2.3"),
        ("## 1.2.3 - 2026-01-01", "1.2.3"),
        ("## [v1.2.3]", "1.2.3"),
        ("## [1.2.3-rc.1] - 2026-01-01", "1.2.3-rc.1"),
    ],
)
def test_changelog_heading_shapes(heading: str, expected: str) -> None:
    assert mod.changelog_top_version(f"# C\n\n{heading}\n") == expected


def test_changelog_without_dated_heading_is_none() -> None:
    assert mod.changelog_top_version("# Changelog\n\n## Unreleased\n") is None


# ── git tags ─────────────────────────────────────────────────────────────
def _tagged_repo(tmp_path, tags: list[str]):
    init_test_repo(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-q", "--allow-empty", "-m", "x"],
        env=git_env(),
        check=True,
    )
    for tag in tags:
        subprocess.run(["git", "-C", str(tmp_path), "tag", tag], check=True)
    return tmp_path


def test_latest_git_tag_takes_semver_max(tmp_path) -> None:
    repo = _tagged_repo(tmp_path, ["v1.9.0", "v1.10.0", "v1.2.3", "vendor-tag"])
    assert mod.latest_git_tag(repo) == "1.10.0"


def test_latest_git_tag_none_when_untagged(tmp_path) -> None:
    repo = _tagged_repo(tmp_path, [])
    assert mod.latest_git_tag(repo) is None


# ── PKGBUILD / AUR parsing ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "body, expected",
    [
        ("pkgver=1.2.3\npkgrel=1\n", "1.2.3"),
        ("pkgver='1.2.3'\n", "1.2.3"),  # single-quoted
        ('pkgver="1.2.3"\n', "1.2.3"),  # double-quoted
        ("pkgver=v1.2.3\n", "1.2.3"),  # leading v normalized off
        ("pkgver=1.2.3 # inline comment\n", "1.2.3"),
        ("pkgver=1.0.0\npkgver=1.2.3\n", "1.2.3"),  # last static assignment wins
        ("pkgver=1.2.3-rc.1\n", "1.2.3-rc.1"),
    ],
)
def test_pkgbuild_version_reads_static_pkgver(body: str, expected: str) -> None:
    assert mod.pkgbuild_version(f"pkgname=demo\n{body}") == expected


@pytest.mark.parametrize(
    "body",
    [
        "pkgname=demo\n",  # no pkgver at all
        "pkgver=$(git describe --tags)\n",  # command substitution
        "pkgver=`git describe`\n",  # backtick substitution
        "pkgver=$_base\n",  # variable reference
        "pkgver=1.2.3\npkgver() {\n  echo 1.2.4\n}\n",  # VCS pkgver() function
    ],
)
def test_pkgbuild_version_skips_non_static(body: str) -> None:
    assert mod.pkgbuild_version(body) is None


# ── compare ──────────────────────────────────────────────────────────────
def test_compare_agreement_is_empty() -> None:
    assert mod.compare("1.2.3", "1.2.3") == []


def test_compare_absent_aur_is_not_a_failure() -> None:
    # AUR defaults to None (no PKGBUILD): the canary passes on the two
    # mandatory markers alone.
    assert mod.compare("1.2.3", "1.2.3", None) == []


def test_compare_agreeing_aur_is_empty() -> None:
    assert mod.compare("1.2.3", "1.2.3", "1.2.3") == []


def test_compare_disagreeing_aur_fails_and_is_listed() -> None:
    report = mod.compare("1.2.3", "1.2.3", "1.2.2")
    joined = "\n".join(report)
    assert "AUR (PKGBUILD pkgver): 1.2.2" in joined
    assert report[-1] == "release-canary: mismatch: 1.2.2 != 1.2.3"


def test_compare_mismatch_lists_both_markers_and_the_diff() -> None:
    report = mod.compare("1.2.4", "1.2.3")
    joined = "\n".join(report)
    assert "git tag (max v*): 1.2.4" in joined
    assert "changelog (top dated heading): 1.2.3" in joined
    assert report[-1] == "release-canary: mismatch: 1.2.3 != 1.2.4"


def test_compare_missing_tag_is_a_failure() -> None:
    report = mod.compare(None, "1.2.3")
    assert "missing marker(s): git tag (max v*)" in report[-1]


def test_compare_missing_changelog_is_a_failure() -> None:
    report = mod.compare("1.2.3", None)
    assert "missing marker(s): changelog (top dated heading)" in report[-1]


# ── main: each axis can break the canary ─────────────────────────────────
def _release_repo(tmp_path, tag: str, heading: str):
    repo = _tagged_repo(tmp_path, [tag])
    (repo / "CHANGELOG.md").write_text(
        f"# C\n\n## Unreleased\n\n## [{heading}] - 2026-07-01\n"
    )
    return repo


def test_main_agreeing_markers_exit_zero(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    assert mod.main(["--repo-dir", str(repo)]) == 0
    assert "OK — git tag and changelog all say 1.4.0" in capsys.readouterr().out


def test_main_tag_axis_mismatch_fails(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.3.0", "1.4.0")
    assert mod.main(["--repo-dir", str(repo)]) == 1
    err = capsys.readouterr().err
    assert "git tag (max v*): 1.3.0" in err and "1.4.0" in err


def test_main_changelog_axis_mismatch_fails(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.3.9")
    assert mod.main(["--repo-dir", str(repo)]) == 1
    assert "changelog (top dated heading): 1.3.9" in capsys.readouterr().err


def test_main_untagged_repo_is_a_missing_marker(tmp_path, capsys) -> None:
    # A changelog rolled but never tagged: the half-finished release the canary
    # exists to catch, reported rather than crashed on.
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    subprocess.run(["git", "tag", "-d", "v1.4.0"], cwd=repo, env=git_env(), check=True)
    assert mod.main(["--repo-dir", str(repo)]) == 1
    assert "missing marker(s): git tag (max v*)" in capsys.readouterr().err


def test_main_absent_changelog_is_a_missing_marker(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    (repo / "CHANGELOG.md").unlink()
    assert mod.main(["--repo-dir", str(repo)]) == 1
    assert "missing marker(s): changelog (top dated heading)" in capsys.readouterr().err


def test_main_agreeing_pkgbuild_is_folded_in(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    (repo / "PKGBUILD").write_text("pkgname=demo\npkgver=1.4.0\npkgrel=1\n")
    assert mod.main(["--repo-dir", str(repo)]) == 0
    assert "git tag, changelog, and AUR all say 1.4.0" in capsys.readouterr().out


def test_main_pkgbuild_axis_mismatch_fails(tmp_path, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    (repo / "PKGBUILD").write_text("pkgname=demo\npkgver=1.3.0\npkgrel=1\n")
    assert mod.main(["--repo-dir", str(repo)]) == 1
    assert "AUR (PKGBUILD pkgver): 1.3.0" in capsys.readouterr().err


def test_main_computed_pkgver_is_skipped_not_failed(tmp_path) -> None:
    # A VCS PKGBUILD whose pkgver() computes the version can't be read offline;
    # its presence must not fail an otherwise-agreeing release.
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    (repo / "PKGBUILD").write_text(
        "pkgname=demo-git\npkgver=1.4.0\npkgver() {\n  echo 9.9.9\n}\n"
    )
    assert mod.main(["--repo-dir", str(repo)]) == 0


def test_main_custom_pkgbuild_path(tmp_path) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    (repo / "aur").mkdir()
    (repo / "aur" / "PKGBUILD").write_text("pkgver=1.3.0\n")  # would mismatch
    # Default path (./PKGBUILD) is absent → AUR skipped → passes.
    assert mod.main(["--repo-dir", str(repo)]) == 0
    # Pointed at the real PKGBUILD → the mismatch is caught.
    assert mod.main(["--repo-dir", str(repo), "--pkgbuild", "aur/PKGBUILD"]) == 1


def test_main_makes_no_subprocess_call_but_git(tmp_path, monkeypatch) -> None:
    """The canary is offline: the only process it spawns is the tag read.

    Pins the property that removed the npm dependency — a future marker that
    reaches the network would have to break this test to land."""
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")  # built before recording
    spawned: list[list[str]] = []
    real_run = mod.subprocess.run

    def record(cmd, *args, **kwargs):
        spawned.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", record)
    assert mod.main(["--repo-dir", str(repo)]) == 0
    assert [cmd[0] for cmd in spawned] == ["git"], spawned
    assert spawned[0][3:] == ["tag", "--list", "v*"]

"""Tests for hooks/release_canary.py — the apply-side console script that
asserts the max published npm version, the max `v*` git tag, and the
changelog's top dated heading all agree.

The npm lookup is the tool's only network touch and is injected via
monkeypatch; git-tag and changelog parsing run against real fixtures.
"""

import json
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


# ── compare ──────────────────────────────────────────────────────────────
def test_compare_agreement_is_empty() -> None:
    assert mod.compare("1.2.3", "1.2.3", "1.2.3") == []


def test_compare_mismatch_lists_all_three_and_the_diff() -> None:
    report = mod.compare("1.2.3", "1.2.4", "1.2.3")
    joined = "\n".join(report)
    assert "npm (max published): 1.2.3" in joined
    assert "git tag (max v*): 1.2.4" in joined
    assert "changelog (top dated heading): 1.2.3" in joined
    assert report[-1] == "release-canary: mismatch: 1.2.3 != 1.2.4"


def test_compare_missing_marker_is_a_failure() -> None:
    report = mod.compare("1.2.3", None, "1.2.3")
    assert "missing marker(s): git tag (max v*)" in report[-1]


# ── main: each axis can break the canary ─────────────────────────────────
def _release_repo(tmp_path, tag: str, heading: str):
    repo = _tagged_repo(tmp_path, [tag])
    (repo / "package.json").write_text(json.dumps({"name": "demo-pkg"}))
    (repo / "CHANGELOG.md").write_text(
        f"# C\n\n## Unreleased\n\n## [{heading}] - 2026-07-01\n"
    )
    return repo


def _inject_npm(monkeypatch, versions: list[str]):
    monkeypatch.setattr(mod, "npm_published_versions", lambda package: versions)


def test_main_all_three_agree_exits_zero(tmp_path, monkeypatch, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    _inject_npm(monkeypatch, ["1.3.0", "1.4.0"])
    assert mod.main(["--repo-dir", str(repo)]) == 0
    assert "OK" in capsys.readouterr().out


def test_main_npm_axis_mismatch_fails(tmp_path, monkeypatch, capsys) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.4.0")
    _inject_npm(monkeypatch, ["1.3.0", "5.0.0"])  # the runaway-publish incident
    assert mod.main(["--repo-dir", str(repo)]) == 1
    assert "5.0.0" in capsys.readouterr().err


def test_main_tag_axis_mismatch_fails(tmp_path, monkeypatch) -> None:
    repo = _release_repo(tmp_path, "v1.3.0", "1.4.0")
    _inject_npm(monkeypatch, ["1.4.0"])
    assert mod.main(["--repo-dir", str(repo)]) == 1


def test_main_changelog_axis_mismatch_fails(tmp_path, monkeypatch) -> None:
    repo = _release_repo(tmp_path, "v1.4.0", "1.3.9")
    _inject_npm(monkeypatch, ["1.4.0"])
    assert mod.main(["--repo-dir", str(repo)]) == 1


def test_main_explicit_package_skips_package_json(tmp_path, monkeypatch) -> None:
    repo = _release_repo(tmp_path, "v1.0.0", "1.0.0")
    (repo / "package.json").unlink()
    seen: list[str] = []

    def fake(package: str) -> list[str]:
        seen.append(package)
        return ["1.0.0"]

    monkeypatch.setattr(mod, "npm_published_versions", fake)
    assert mod.main(["--package", "other-pkg", "--repo-dir", str(repo)]) == 0
    assert seen == ["other-pkg"]


def test_main_missing_package_json_without_flag_dies(tmp_path) -> None:
    repo = _tagged_repo(tmp_path, ["v1.0.0"])
    with pytest.raises(SystemExit, match="no package.json"):
        mod.main(["--repo-dir", str(repo)])


def test_npm_published_versions_uses_versions_json_not_latest(monkeypatch) -> None:
    # Contract pin: the subprocess argv must ask for `versions --json`, never
    # the `latest` dist-tag via `version`.
    captured: dict = {}

    class _Done:
        stdout = '["1.0.0", "1.1.0"]'
        returncode = 0

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert mod.npm_published_versions("p") == ["1.0.0", "1.1.0"]
    assert captured["cmd"][:2] == ["npm", "view"]
    assert "versions" in captured["cmd"] and "--json" in captured["cmd"]
    assert "version" not in captured["cmd"]  # the dist-tag form is banned


def test_npm_single_version_string_shape(monkeypatch) -> None:
    class _Done:
        stdout = '"1.0.0"'
        returncode = 0

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Done())
    assert mod.npm_published_versions("p") == ["1.0.0"]

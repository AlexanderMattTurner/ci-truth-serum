"""Tests for hooks/check_provenance_repo_url.py — the identity lint that pins
package.json / pyproject.toml repository URLs to the repo the origin remote
actually names (npm provenance rejects a mismatched repository.url with E422).

Drives ``normalize_repo_url()`` for the normalization rules and ``check_repo()``
/ ``main()`` against throwaway git repos with real origin remotes.
"""

import json
import subprocess

import pytest

from tests._helpers import init_test_repo, load_hook

mod = load_hook("check_provenance_repo_url.py", "check_provenance_repo_url")


# ── normalize_repo_url ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/Owner/Repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git+https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("ssh://git@github.com/owner/repo", "owner/repo"),
        ("http://user@127.0.0.1:4444/git/Owner/repo", "owner/repo"),
        ("https://github.com/owner/repo#readme", "owner/repo"),
        ("github.com/owner/repo", "owner/repo"),
        ("not-a-url", None),
        ("https://github.com/", None),
        ("", None),
    ],
)
def test_normalize_repo_url(url: str, expected: str | None) -> None:
    assert mod.normalize_repo_url(url) == expected


# ── fixtures ─────────────────────────────────────────────────────────────
def _repo(tmp_path, origin: str | None = "https://github.com/real/owner-repo"):
    init_test_repo(tmp_path)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(tmp_path), "remote", "add", "origin", origin], check=True
        )
    return tmp_path


def _pkg(repo, url: str | None):
    doc: dict = {"name": "x", "version": "1.0.0"}
    if url is not None:
        doc["repository"] = {"type": "git", "url": url}
    (repo / "package.json").write_text(json.dumps(doc))


def _publish_workflow(repo):
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yaml").write_text(
        "jobs:\n  r:\n    steps:\n      - run: npm publish --provenance\n"
    )


# ── check_repo ───────────────────────────────────────────────────────────
def test_matching_package_json_passes(tmp_path) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "git+https://github.com/Real/Owner-Repo.git")
    assert mod.check_repo(repo) == []


def test_mismatched_package_json_fails(tmp_path) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "git+https://github.com/upstream/owner-repo.git")
    msgs = mod.check_repo(repo)
    assert len(msgs) == 1
    assert "package.json" in msgs[0] and "real/owner-repo" in msgs[0]


def test_string_form_repository_field_is_compared(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "package.json").write_text(
        json.dumps({"repository": "https://github.com/other/thing"})
    )
    assert len(mod.check_repo(repo)) == 1


def test_no_origin_remote_skips_silently(tmp_path) -> None:
    repo = _repo(tmp_path, origin=None)
    _pkg(repo, "git+https://github.com/anything/at-all.git")
    _publish_workflow(repo)
    assert mod.check_repo(repo) == []


def test_publish_workflow_without_repository_url_fails(tmp_path) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, None)
    _publish_workflow(repo)
    msgs = mod.check_repo(repo)
    assert len(msgs) == 1
    assert "npm/pnpm publish" in msgs[0] and "E422" in msgs[0]


def test_no_publish_workflow_tolerates_missing_repository_url(tmp_path) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, None)
    assert mod.check_repo(repo) == []


def test_no_package_json_is_not_a_publish_violation(tmp_path) -> None:
    repo = _repo(tmp_path)
    _publish_workflow(repo)
    assert mod.check_repo(repo) == []


def test_pyproject_repository_key_mismatch_fails(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        "[project]\nname = 'x'\n\n[project.urls]\n"
        'Repository = "https://github.com/wrong/place"\n'
    )
    msgs = mod.check_repo(repo)
    assert len(msgs) == 1 and "pyproject.toml" in msgs[0]


def test_pyproject_homepage_is_never_compared(tmp_path) -> None:
    # Docs sites legitimately live elsewhere: only repository-ish keys count.
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project.urls]\nHomepage = "https://github.com/upstream/docs"\n'
    )
    assert mod.check_repo(repo) == []


def test_pyproject_matching_source_key_passes(tmp_path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text(
        '[project.urls]\n"Source Code" = "https://github.com/real/owner-repo"\n'
    )
    assert mod.check_repo(repo) == []


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, monkeypatch, capsys) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "https://github.com/upstream/owner-repo")
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    assert mod.main() == 1
    assert "::error file=package.json::" in capsys.readouterr().out


def test_main_clean_repo_exits_zero(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "https://github.com/real/owner-repo")
    monkeypatch.setattr(mod, "REPO_ROOT", repo)
    assert mod.main() == 0

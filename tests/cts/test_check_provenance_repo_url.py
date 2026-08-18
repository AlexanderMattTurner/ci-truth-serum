"""Tests for ci_truth_serum/check_provenance_repo_url.py — the identity lint that pins
package.json / pyproject.toml repository URLs to the repo the origin remote
actually names (npm provenance rejects a mismatched repository.url with E422).

Drives ``normalize_repo_url()`` for the normalization rules and ``check_repo()``
/ ``main()`` against throwaway git repos with real origin remotes.
"""

import json
import os
import shutil
import stat
import subprocess

import pytest

from tests._helpers import init_test_repo, load_hook

mod = load_hook("check_provenance_repo_url.py", "check_provenance_repo_url")


@pytest.fixture(autouse=True)
def _no_inherited_github_repository(monkeypatch) -> None:
    """Drop `$GITHUB_REPOSITORY` for every test here.

    The check now prefers that variable over the remote, and CI exports it — so
    without this the whole suite would compare against the repo running the
    tests, and every origin-remote case would pass or fail for the wrong reason.
    """
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)


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


def test_publish_only_mentioned_in_a_comment_is_not_a_publish(tmp_path) -> None:
    # Demanding a repository.url off a comment that says the opposite is the
    # same class as the secret-name false positive: the remedy is a config edit
    # the repo has no use for.
    repo = _repo(tmp_path)
    _pkg(repo, None)
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yaml").write_text(
        "jobs:\n  r:\n    steps:\n"
        "      # releases are manual; we never run npm publish here\n"
        "      - run: echo done\n"
    )
    assert mod.check_repo(repo) == []


def test_publish_after_a_quoted_hash_is_still_seen(tmp_path) -> None:
    # False-negative guard: a `#` inside a quoted scalar is content, so a real
    # publish later on the same line must still register.
    repo = _repo(tmp_path)
    _pkg(repo, None)
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "release.yaml").write_text(
        'jobs:\n  r:\n    steps:\n      - run: "release #42 && npm publish"\n'
    )
    assert len(mod.check_repo(repo)) == 1


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


# ── $GITHUB_REPOSITORY ───────────────────────────────────────────────────
def test_github_repository_env_outranks_a_stale_origin(tmp_path, monkeypatch) -> None:
    # The rename case as CI sees it: origin still names the old repo, and npm
    # signs the provenance bundle with $GITHUB_REPOSITORY, which package.json
    # already matches.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    monkeypatch.setenv("GITHUB_REPOSITORY", "New/Name")
    _pkg(repo, "git+https://github.com/new/name.git")
    assert mod.check_repo(repo) == []


def test_github_repository_env_mismatch_fails(tmp_path, monkeypatch) -> None:
    # The origin remote agrees with package.json, and the publisher does not.
    # npm validates against the publisher, so this release dies.
    repo = _repo(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/publisher")
    _pkg(repo, "https://github.com/real/owner-repo")
    msgs = mod.check_repo(repo)
    assert len(msgs) == 1
    assert "other/publisher" in msgs[0] and "$GITHUB_REPOSITORY" in msgs[0]


def test_github_repository_env_pins_identity_without_any_remote(
    tmp_path, monkeypatch
) -> None:
    # A checkout with no origin was skipped entirely. The variable alone is
    # enough to judge it.
    repo = _repo(tmp_path, origin=None)
    monkeypatch.setenv("GITHUB_REPOSITORY", "real/owner-repo")
    _pkg(repo, "https://github.com/upstream/owner-repo")
    assert len(mod.check_repo(repo)) == 1


@pytest.mark.parametrize("value", ["", "   ", "owner", "owner/repo/extra", "/repo"])
def test_malformed_github_repository_falls_back_to_origin(
    tmp_path, monkeypatch, value: str
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", value)
    _pkg(repo, "https://github.com/real/owner-repo")
    assert mod.check_repo(repo) == []


# ── rename redirects ─────────────────────────────────────────────────────
def _fake_git(tmp_path, monkeypatch, redirect_to: str | None, delay: float = 0):
    """Put a `git` on PATH that answers `ls-remote` itself and delegates every
    other subcommand to the real git.

    A rename redirect cannot be staged locally — it needs an HTTP server that
    answers 301 — so the probe's transport is the one part stubbed. Everything
    else the check runs (`remote get-url`, the repo setup) stays real git. The
    stub appends one line per call to `calls.txt`, which is how a test proves
    the probe ran once, or never.
    """
    real_git = shutil.which("git")
    assert real_git, "git must be on PATH"
    calls = tmp_path / "calls.txt"
    warning = (
        f'printf "warning: redirecting to {redirect_to}\\n" >&2' if redirect_to else ":"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "${arg}" == "ls-remote" ]]; then\n'
        f'    printf "%s\\n" "$*" >> "{calls}"\n'
        f"    sleep {delay}\n"
        f"    {warning}\n"
        '    printf "deadbeef\\tHEAD\\n"\n'
        "    exit 0\n"
        "  fi\n"
        "done\n"
        f'exec "{real_git}" "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return calls


def test_renamed_repo_clears_the_mismatch(tmp_path, monkeypatch) -> None:
    # The rename case in a plain clone: origin holds the old URL, package.json
    # holds the new name, and the old URL redirects to the new one.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    _pkg(repo, "git+https://github.com/real/new-name.git")
    calls = _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/")
    assert mod.check_repo(repo) == []
    assert calls.read_text().count("\n") == 1


def test_a_url_that_is_not_the_redirect_target_still_fails(
    tmp_path, monkeypatch
) -> None:
    # Non-vacuity for the probe: following the redirect must clear the renamed
    # name only, never every mismatch.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    _pkg(repo, "git+https://github.com/upstream/template.git")
    _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/")
    msgs = mod.check_repo(repo)
    assert len(msgs) == 1
    assert "old/name" in msgs[0] and "real/new-name" in msgs[0]


def test_origin_that_does_not_redirect_still_fails(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "git+https://github.com/upstream/owner-repo.git")
    _fake_git(tmp_path, monkeypatch, None)
    assert len(mod.check_repo(repo)) == 1


def test_two_mismatches_probe_the_remote_once(tmp_path, monkeypatch) -> None:
    # The probe is a network call, so a repo that declares the same stale URL in
    # both manifests must not pay for it twice.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    _pkg(repo, "git+https://github.com/upstream/template.git")
    (repo / "pyproject.toml").write_text(
        '[project.urls]\nRepository = "https://github.com/upstream/template"\n'
    )
    calls = _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/")
    assert len(mod.check_repo(repo)) == 2
    assert calls.read_text().count("\n") == 1


def test_a_pinned_identity_never_probes_the_network(tmp_path, monkeypatch) -> None:
    # $GITHUB_REPOSITORY IS the publisher npm validates against, so a redirect
    # cannot excuse a mismatch with it — and the probe must not run.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    monkeypatch.setenv("GITHUB_REPOSITORY", "real/new-name")
    _pkg(repo, "git+https://github.com/old/name.git")
    calls = _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/")
    assert len(mod.check_repo(repo)) == 1
    assert not calls.exists()


def test_a_clean_repo_never_probes_the_network(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _pkg(repo, "https://github.com/real/owner-repo")
    calls = _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/")
    assert mod.check_repo(repo) == []
    assert not calls.exists()


def test_redirect_probe_reads_the_warning_from_stderr(tmp_path, monkeypatch) -> None:
    # Drives the probe on its own: the redirect target comes back normalized.
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    _fake_git(tmp_path, monkeypatch, "https://github.com/Real/New-Name.git/")
    assert mod.redirected_origin_repo(repo) == "real/new-name"


def test_redirect_probe_returns_none_without_a_warning(tmp_path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _fake_git(tmp_path, monkeypatch, None)
    assert mod.redirected_origin_repo(repo) is None


def test_a_slow_probe_leaves_the_finding_standing(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = _repo(tmp_path, origin="https://github.com/old/name")
    _pkg(repo, "git+https://github.com/real/new-name.git")
    _fake_git(tmp_path, monkeypatch, "https://github.com/real/new-name.git/", delay=5)
    monkeypatch.setattr(mod, "_PROBE_TIMEOUT", 0.5)
    assert len(mod.check_repo(repo)) == 1
    assert "did not answer" in capsys.readouterr().err


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

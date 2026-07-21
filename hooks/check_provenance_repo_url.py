#!/usr/bin/env python3
"""Verify the repo URLs a package publishes match the repo it actually lives in.

npm publishes with provenance mint a sigstore bundle from `GITHUB_REPOSITORY`,
and the registry rejects the upload (`E422 … Failed to validate repository
information`) when `package.json`'s `repository.url` names a different
owner/repo. This bites every fork of a template: the URLs still point at the
upstream, so the very first release dies at publish — it killed the first
releases of three sibling repos.

Checks (all local — the only git touched is `git remote get-url origin`):

  * `package.json` `repository` / `repository.url` and `pyproject.toml`
    `[project.urls]` repository-ish keys (`repository`, `source`, `source
    code`; `homepage` is deliberately NOT compared — docs sites legitimately
    live elsewhere) must name the same owner/repo as the `origin` remote,
    after normalization (`git+` prefix, `.git` suffix, ssh vs https, case).
  * A workflow that runs `npm publish`/`pnpm publish` with no
    `package.json` `repository.url` at all is flagged — provenance has
    nothing to validate against and the first release dies.

No opt-out for a mismatch: a `repository.url` naming a repo other than the one
publishing is always wrong — forks must repoint their self-referential URLs.
A repo with no `origin` remote is skipped silently (nothing to compare
against). Globs every workflow like the other workflow lints; the passed file
list is ignored.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# [project.urls] keys that name the source repository. `homepage`/`documentation`
# legitimately point elsewhere and are never compared (precision over recall).
_REPO_URL_KEYS = frozenset({"repository", "source", "source code", "repo"})

_PUBLISH = re.compile(r"\b(?:npm|pnpm)\s+publish\b")


def normalize_repo_url(url: str) -> str | None:
    """OWNER/REPO (lowercased) named by URL, or None when no owner/repo shape is
    recognizable. Handles `git+https://`, `git@host:owner/repo.git`,
    `ssh://git@host/owner/repo`, and plain https forms."""
    u = url.strip().removeprefix("git+")
    u = re.sub(r"^[a-zA-Z][\w+.-]*://", "", u)  # scheme
    u = re.sub(r"^[^/@]*@", "", u)  # user@ (ssh)
    u = u.replace(":", "/", 1) if "//" not in u and ":" in u.split("/", 1)[0] else u
    parts = [p for p in u.split("/") if p][1:]  # drop host
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    repo = repo.removesuffix(".git")
    repo = repo.split("#", 1)[0].split("?", 1)[0]
    if not owner or not repo:
        return None
    return f"{owner}/{repo}".lower()


def origin_repo(repo_root: Path) -> str | None:
    """OWNER/REPO of the `origin` remote, or None when there is no origin (or
    its URL has no recognizable owner/repo)."""
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return normalize_repo_url(proc.stdout.strip())


def package_json_repo_url(repo_root: Path) -> str | None:
    """The `repository` URL string from package.json, or None when absent (no
    package.json, no repository field, or a shape without a URL)."""
    path = repo_root / "package.json"
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    repository = doc.get("repository")
    if isinstance(repository, str):
        return repository
    if isinstance(repository, dict) and isinstance(repository.get("url"), str):
        return repository["url"]
    return None


# One `key = "value"` line inside [project.urls]. A bespoke scan (not tomllib,
# which is stdlib only from 3.11) covering the flat shape [project.urls] takes
# in practice; an exotic TOML layout simply yields no entries (fail open).
_TOML_URL_LINE = re.compile(
    r"""^\s*(?P<q>["']?)(?P<key>[\w .-]+)(?P=q)\s*=\s*(?P<vq>["'])(?P<url>[^"']*)(?P=vq)"""
)


def pyproject_repo_urls(repo_root: Path) -> list[tuple[str, str]]:
    """(key, url) for every repository-ish [project.urls] entry in
    pyproject.toml."""
    path = repo_root / "pyproject.toml"
    if not path.exists():
        return []
    entries: list[tuple[str, str]] = []
    in_urls = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_urls = stripped == "[project.urls]"
            continue
        if not in_urls:
            continue
        m = _TOML_URL_LINE.match(line)
        if m and m.group("key").strip().lower() in _REPO_URL_KEYS:
            entries.append((m.group("key").strip(), m.group("url")))
    return entries


def has_npm_publish(repo_root: Path) -> bool:
    """True when any workflow/composite action runs `npm publish`/`pnpm publish`."""
    return any(
        _PUBLISH.search(path.read_text(encoding="utf-8"))
        for path in _workflow_files(
            repo_root / ".github" / "workflows", repo_root / ".github" / "actions"
        )
    )


def check_repo(repo_root: Path) -> list[str]:
    """Every provenance-URL violation for the repo, as printable messages."""
    origin = origin_repo(repo_root)
    if origin is None:
        return []  # nothing to compare against — a repo without an origin remote

    found: list[str] = []
    pkg_url = package_json_repo_url(repo_root)
    if pkg_url is not None:
        named = normalize_repo_url(pkg_url)
        if named != origin:
            found.append(
                f"::error file=package.json::repository.url names `{named or pkg_url}` "
                f"but the origin remote is `{origin}`. npm provenance validates "
                "repository.url against the publishing repo and rejects the upload "
                "(E422) on mismatch — repoint the URL (forks must repoint every "
                "self-referential GitHub URL)."
            )
    elif (repo_root / "package.json").exists() and has_npm_publish(repo_root):
        found.append(
            "::error file=package.json::a workflow runs `npm/pnpm publish` but "
            "package.json has no repository.url — provenance publish dies with "
            f'E422 on the first release. Add: "repository": {{"type": "git", '
            f'"url": "git+https://github.com/{origin}.git"}}'
        )

    for key, url in pyproject_repo_urls(repo_root):
        named = normalize_repo_url(url)
        if named != origin:
            found.append(
                f"::error file=pyproject.toml::[project.urls] {key} names "
                f"`{named or url}` but the origin remote is `{origin}` — repoint "
                "the URL (forks must repoint every self-referential URL)."
            )
    return found


def main() -> int:
    violations = check_repo(REPO_ROOT)
    for message in violations:
        print(message)
    if violations:
        print(f"\nERROR: {len(violations)} provenance-URL violation(s) found.")
        print(
            "A repository URL naming a repo other than the one publishing kills "
            "the release at the registry's provenance validation."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Verify the release version markers agree: npm, git tag, changelog, AUR.

A release pipeline leaves the version in several places — the npm registry, a
`v*` git tag, and the changelog's top dated heading — and any pair can drift
(a publish that died after tagging, a tag push that 403'd after publishing, a
changelog promotion that never ran). This canary asserts all three are EQUAL
and prints exactly what disagrees when they aren't, so downstream repos run
one `pip install ci-truth-serum && release-canary` step instead of each
hand-rolling the comparison.

The npm side uses `npm view <pkg> versions --json` (the full published list)
and takes a REAL semver max. It deliberately does NOT use
`npm view <pkg> version`, which returns the `latest` dist-tag — a value that
silently misreports whenever a publish set the tag wrong or a later version
shipped without retagging (a past bug this tool exists to prevent).

A repo that also ships to the Arch User Repository carries a `PKGBUILD`; when
one is present its `pkgver=` is folded in as a fourth marker (the classic
drift is bumping npm/tag/changelog and forgetting the PKGBUILD). AUR is
OPTIONAL: absent PKGBUILD, or a `pkgver` computed at build time by a
`pkgver()` function / `$(…)` expansion that can't be read statically, is
simply skipped, never a failure. Reading the local PKGBUILD keeps the `npm
view` call the tool's ONLY network touch; git-tag, changelog, and AUR parsing
are all local. Not a pre-commit lint and not in any tier aggregate — like
`sync-required-checks`, it is an apply-side console script::

    pip install ci-truth-serum
    release-canary                     # package name read from ./package.json
    release-canary --package my-pkg --changelog CHANGELOG.md --repo-dir .
    release-canary --pkgbuild aur/PKGBUILD   # non-default PKGBUILD location
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# X.Y.Z with optional pre-release/build suffix; the numeric triple orders the
# comparison, a pre-release suffix ranks below its release (SemVer rule 11's
# common case — full pre-release field-by-field ordering is out of scope and
# release pipelines here never compare two pre-releases).
_SEMVER = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?P<pre>-[0-9A-Za-z.-]+)?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
# A dated changelog release heading: `## [1.2.3] - 2026-01-31` (bracket and
# date optional in the wild; `## Unreleased` is skipped by the version shape).
_HEADING = re.compile(r"^##\s*\[?v?(?P<version>\d+\.\d+\.\d+[^\]\s]*)\]?")
# A static `pkgver=` assignment in a PKGBUILD (bash): the value runs to the
# first whitespace/comment; quotes are stripped by the parser.
_PKGVER = re.compile(r"^\s*pkgver\s*=\s*(?P<version>[^\s#]+)")
# A `pkgver()` function means the version is computed at build time (a VCS
# package), so the static `pkgver=` seed is not the release marker.
_PKGVER_FUNC = re.compile(r"^\s*pkgver\s*\(\s*\)", re.MULTILINE)


def semver_key(version: str) -> tuple | None:
    """A sort key for VERSION, or None when it is not semver-shaped."""
    m = _SEMVER.match(version.strip())
    if not m:
        return None
    return (
        int(m.group("major")),
        int(m.group("minor")),
        int(m.group("patch")),
        m.group("pre") is None,  # a release outranks its own pre-releases
        m.group("pre") or "",
    )


def max_semver(versions: list[str]) -> str | None:
    """The semver-max of VERSIONS (non-semver entries ignored), or None when
    nothing semver-shaped is present. Normalized without a leading `v`."""
    keyed = [(semver_key(v), v) for v in versions]
    valid = [(k, v) for k, v in keyed if k is not None]
    if not valid:
        return None
    _, best = max(valid)
    return best.strip().lstrip("v")


def npm_published_versions(package: str) -> list[str]:
    """Every published version of PACKAGE, via `npm view <pkg> versions --json`
    — the full list, never the `latest` dist-tag. The tool's only network
    touch; a failure propagates loudly."""
    proc = subprocess.run(
        ["npm", "view", package, "versions", "--json"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(proc.stdout)
    # npm prints a bare string when exactly one version is published.
    return [data] if isinstance(data, str) else list(data)


def latest_git_tag(repo_dir: Path) -> str | None:
    """Semver-max of the repo's `v*` tags, or None when there are none."""
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), "tag", "--list", "v*"],
        capture_output=True,
        text=True,
        check=True,
    )
    return max_semver(proc.stdout.split())


def changelog_top_version(text: str) -> str | None:
    """The first dated `## [x.y.z]` heading's version in TEXT (an `##
    Unreleased` heading is skipped by shape), or None when no release heading
    exists."""
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            return m.group("version").lstrip("v")
    return None


def pkgbuild_version(text: str) -> str | None:
    """The AUR package version from a PKGBUILD's `pkgver=`, normalized without a
    leading `v`, or None when no static release version can be read.

    Returns None (skip AUR, never fail) when TEXT has a `pkgver()` function or a
    `pkgver=` whose value is a `$(…)`/backtick/`$var` expansion — those compute
    the version at build time and can't be resolved offline. The last static
    `pkgver=` wins, matching bash's last-assignment-wins."""
    if _PKGVER_FUNC.search(text):
        return None
    version = None
    for line in text.splitlines():
        m = _PKGVER.match(line)
        if not m:
            continue
        raw = m.group("version").strip().strip("\"'")
        if "$" in raw or "`" in raw:  # a computed value, not a static release
            return None
        version = raw
    return version.lstrip("v") if version else None


def package_name(repo_dir: Path) -> str:
    """The `name` from ./package.json — the default when --package is omitted."""
    path = repo_dir / "package.json"
    if not path.exists():
        raise SystemExit(
            "release-canary: no --package given and no package.json in "
            f"{repo_dir} to read `name` from."
        )
    name = json.loads(path.read_text(encoding="utf-8")).get("name")
    if not isinstance(name, str) or not name:
        raise SystemExit("release-canary: package.json has no usable `name`.")
    return name


def compare(
    npm: str | None,
    tag: str | None,
    changelog: str | None,
    aur: str | None = None,
) -> list[str]:
    """Human-readable report lines; empty means every present marker agrees.

    npm/tag/changelog are mandatory (a None among them is a `missing marker`
    failure). AUR is optional: it is folded in only when AUR is not None, so an
    absent PKGBUILD never fails the canary, but a PKGBUILD that disagrees
    does."""
    labeled = [
        ("npm (max published)", npm),
        ("git tag (max v*)", tag),
        ("changelog (top dated heading)", changelog),
    ]
    if aur is not None:
        labeled.append(("AUR (PKGBUILD pkgver)", aur))
    missing = [label for label, value in labeled if value is None]
    values = {value for _label, value in labeled}
    if not missing and len(values) == 1:
        return []
    lines = [
        f"  {label}: {value if value is not None else '<none found>'}"
        for label, value in labeled
    ]
    present = sorted({v for v in values if v is not None})
    diff = (
        f"mismatch: {' != '.join(present)}"
        if len(present) > 1
        else f"missing marker(s): {', '.join(missing)}"
    )
    return lines + [f"release-canary: {diff}"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package", help="npm package name (default: `name` from ./package.json)"
    )
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"), help="changelog path"
    )
    parser.add_argument(
        "--repo-dir", type=Path, default=Path.cwd(), help="git repo to read tags from"
    )
    parser.add_argument(
        "--pkgbuild",
        type=Path,
        default=Path("PKGBUILD"),
        help="AUR PKGBUILD path; its pkgver is checked only when the file exists",
    )
    args = parser.parse_args(argv)

    def resolve(path: Path) -> Path:
        return path if path.is_absolute() else args.repo_dir / path

    package = args.package or package_name(args.repo_dir)
    npm = max_semver(npm_published_versions(package))
    tag = latest_git_tag(args.repo_dir)
    changelog_path = resolve(args.changelog)
    changelog = (
        changelog_top_version(changelog_path.read_text(encoding="utf-8"))
        if changelog_path.exists()
        else None
    )
    pkgbuild_path = resolve(args.pkgbuild)
    aur = (
        pkgbuild_version(pkgbuild_path.read_text(encoding="utf-8"))
        if pkgbuild_path.exists()
        else None
    )

    report = compare(npm, tag, changelog, aur)
    for line in report:
        print(line, file=sys.stderr)
    if report:
        return 1
    markers = "npm, git tag, and changelog"
    if aur is not None:
        markers = "npm, git tag, changelog, and AUR"
    print(f"release-canary: OK — {markers} all say {npm}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

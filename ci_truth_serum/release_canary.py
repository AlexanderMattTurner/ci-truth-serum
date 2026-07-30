#!/usr/bin/env python3
"""Verify the release version markers agree: git tag, changelog, AUR.

A release pipeline leaves the version in several places — a `v*` git tag, the
changelog's top dated heading, and (for a repo that ships to the Arch User
Repository) a `PKGBUILD`'s `pkgver=` — and any pair can drift: a tag pushed
before the changelog was promoted, a changelog roll that never got tagged, a
PKGBUILD nobody bumped. This canary asserts they are EQUAL and prints exactly
what disagrees when they aren't, so downstream repos run one `release-canary`
step instead of each hand-rolling the comparison.

Every marker is read locally — a git tag list, two file parses — so the tool
makes no network request and needs no registry credentials. That is what lets it
run in the same restricted job that cut the release.

AUR is OPTIONAL: an absent PKGBUILD, or a `pkgver` computed at build time by a
`pkgver()` function / `$(…)` expansion that cannot be read statically, is
skipped, never a failure. Tag and changelog are mandatory — a None among them is
a `missing marker` failure, which is what a half-finished release looks like.

Not a pre-commit lint and not in any tier aggregate — like `sync-required-checks`,
it is an apply-side console script::

    release-canary                     # tag + changelog in the current repo
    release-canary --changelog CHANGELOG.md --repo-dir .
    release-canary --pkgbuild aur/PKGBUILD   # non-default PKGBUILD location
"""

import argparse
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


def compare(
    tag: str | None,
    changelog: str | None,
    aur: str | None = None,
) -> list[str]:
    """Human-readable report lines; empty means every present marker agrees.

    tag/changelog are mandatory (a None among them is a `missing marker`
    failure). AUR is optional: it is folded in only when AUR is not None, so an
    absent PKGBUILD never fails the canary, but a PKGBUILD that disagrees
    does."""
    labeled = [
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

    report = compare(tag, changelog, aur)
    for line in report:
        print(line, file=sys.stderr)
    if report:
        return 1
    markers = "git tag and changelog"
    if aur is not None:
        markers = "git tag, changelog, and AUR"
    print(f"release-canary: OK — {markers} all say {tag}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

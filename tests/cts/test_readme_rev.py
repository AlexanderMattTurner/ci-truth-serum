"""The README's documented `rev:` pins must resolve for a consumer.

The bug class this pins closed: the README instructed `rev: v0.1.0` while no
such tag or release existed — every copy-pasted consumer config failed at
`pre-commit install-hooks` with an unreachable rev. Releases here tag
`v<package.json version>` (`tag-release.sh` reads that file and nothing else,
and release_canary asserts the max `v*` tag, the published version, and the
changelog agree), so a README rev is guaranteed to resolve exactly when it
names the version in package.json: the release that ships this README also
creates that tag. Comparing against package.json rather than a second copy of
the version is what makes this a real check — two stale copies agree with each
other while every consumer's pin still 404s.
"""

import json
import re

import pytest

from tests._helpers import REPO_ROOT

_REV = re.compile(r"^\s*rev:\s*(?P<rev>\S+)", re.MULTILINE)


def _released_version() -> str:
    manifest = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    return manifest["version"]


@pytest.mark.drift_guard(
    "the README's yaml examples are prose a consumer copy-pastes — they cannot "
    "read package.json at render time, so the doc copy is pinned against the "
    "release SSOT here instead"
)
def test_readme_rev_pins_name_the_released_tag() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    revs = _REV.findall(readme)
    # Non-vacuity: the README genuinely documents rev-pinned consumer configs.
    assert len(revs) >= 2, "README no longer shows rev-pinned examples?"
    expected = f"v{_released_version()}"
    assert set(revs) == {expected}, (
        f"README documents rev(s) {sorted(set(revs))} but the released version "
        f"is {expected!r} — a consumer copy-pasting the config would pin a rev "
        "that does not resolve. Update the README pins together with the "
        "package.json bump."
    )

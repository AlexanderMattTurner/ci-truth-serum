"""The README's documented `rev:` pins must resolve for a consumer.

The bug class this pins closed: the README instructed `rev: v0.1.0` while no
such tag or release existed — every copy-pasted consumer config failed at
`pre-commit install-hooks` with an unreachable rev. Releases here tag
`v<pyproject version>` (release_canary asserts the max `v*` tag, the published
version, and the changelog agree), so a README rev is guaranteed to resolve
exactly when it names the CURRENT packaged version: the release that ships
this README also creates that tag. Pinning the doc to pyproject means the rev
can go stale only by skipping the version bump — which this test then reddens.
"""

import re

import tomllib

from tests._helpers import REPO_ROOT

_REV = re.compile(r"^\s*rev:\s*(?P<rev>\S+)", re.MULTILINE)


def _packaged_version() -> str:
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    return pyproject["project"]["version"]


def test_readme_rev_pins_name_the_packaged_release_tag() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    revs = _REV.findall(readme)
    # Non-vacuity: the README genuinely documents rev-pinned consumer configs.
    assert len(revs) >= 2, "README no longer shows rev-pinned examples?"
    expected = f"v{_packaged_version()}"
    assert set(revs) == {expected}, (
        f"README documents rev(s) {sorted(set(revs))} but the packaged version "
        f"is {expected!r} — a consumer copy-pasting the config would pin a rev "
        "that does not resolve. Update the README pins (and cut the release "
        "tag) together with the version."
    )

"""Resolve the distribution version from ``package.json``, the release SSOT.

The release pipeline bumps exactly one file: ``package.json``
(``release-prep.sh`` writes it, ``tag-release.sh`` tags ``v<that value>``,
``release-readiness.sh`` gates on it). Any second hand-maintained copy of the
version — a literal in ``pyproject.toml`` — is a copy that nothing bumps, so it
drifts from the git tag permanently. ``pyproject.toml`` therefore declares the
version ``dynamic`` and reads it from here instead of restating it.

Resolution order matters. ``package.json`` wins when it is present next to the
package (a source checkout or an unpacked sdist — i.e. every build), so a build
run inside a virtualenv that already has an OLDER ci-truth-serum installed still
picks up the tree's version rather than the stale installed metadata. Only when
there is no sibling manifest (the ordinary installed-at-runtime case) does this
fall back to the metadata that the build already baked in.
"""

import json
from importlib.metadata import version as _metadata_version
from pathlib import Path

_DIST = "ci-truth-serum"


def _manifest_version() -> str | None:
    """The ``version`` from the repo-root ``package.json``, or None if absent.

    The ``name`` check is what makes the sibling lookup safe: when this module
    is imported from ``site-packages`` its parent's parent is a directory full
    of unrelated distributions, and an unrelated ``package.json`` landing there
    must not be mistaken for ours.
    """
    manifest = Path(__file__).resolve().parent.parent / "package.json"
    if not manifest.is_file():
        return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("name") != _DIST:
        return None
    return data["version"]


def resolve_version() -> str:
    """The package version: the release SSOT when readable, else installed metadata."""
    return _manifest_version() or _metadata_version(_DIST)


__version__ = resolve_version()

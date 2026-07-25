"""Tests for ci_truth_serum/_version.py — the resolver that keeps the package
version from becoming a second hand-maintained copy of the release SSOT.

The bug class is version drift: a literal restated in `pyproject.toml` is a copy
nothing bumps, so it diverges from the git tag the release scripts derive from
`package.json`. The resolver removes the copy, which puts the weight on three
behaviours — the sibling manifest wins when it is ours, a manifest belonging to
some *other* project is ignored rather than trusted, and an installed package
with no sibling manifest still resolves.

Each case loads a *copy* of the module into a temp tree under a unique module
name, so `__file__` points at that tree and the sibling lookup runs for real
against files on disk instead of a patched internal.
"""

import importlib.metadata
import importlib.util
import json
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from tests._helpers import HOOKS_DIR

INSTALLED_VERSION = importlib.metadata.version("ci-truth-serum")


def load_version_module(
    tmp_path: Path, manifest: dict | None, modname: str
) -> ModuleType:
    """Copy `_version.py` into `tmp_path/pkg/`, optionally writing a sibling
    `package.json` at `tmp_path/`, then import the copy under `modname`."""
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    dest = pkg / "_version.py"
    shutil.copy2(HOOKS_DIR / "_version.py", dest)
    if manifest is not None:
        (tmp_path / "package.json").write_text(json.dumps(manifest), encoding="utf-8")

    spec = importlib.util.spec_from_file_location(modname, dest)
    assert spec and spec.loader, dest
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sibling_manifest_is_the_source_of_truth(tmp_path: Path) -> None:
    mod = load_version_module(
        tmp_path,
        {"name": "ci-truth-serum", "version": "9.9.9"},
        "version_manifest_ours",
    )

    assert mod.resolve_version() == "9.9.9"
    assert mod.__version__ == "9.9.9"
    assert "9.9.9" != INSTALLED_VERSION


def test_foreign_manifest_is_ignored(tmp_path: Path) -> None:
    mod = load_version_module(
        tmp_path,
        {"name": "some-other-project", "version": "8.8.8"},
        "version_manifest_foreign",
    )

    assert mod.resolve_version() == INSTALLED_VERSION
    assert mod.resolve_version() != "8.8.8"


def test_no_sibling_manifest_falls_back_to_installed_metadata(tmp_path: Path) -> None:
    mod = load_version_module(tmp_path, None, "version_no_manifest")

    assert mod.resolve_version() == INSTALLED_VERSION
    assert mod.__version__ == INSTALLED_VERSION


def test_manifest_without_a_name_is_ignored(tmp_path: Path) -> None:
    mod = load_version_module(
        tmp_path, {"version": "7.7.7"}, "version_manifest_nameless"
    )

    assert mod.resolve_version() == INSTALLED_VERSION


def test_directory_named_package_json_is_not_read(tmp_path: Path) -> None:
    (tmp_path / "package.json").mkdir()
    mod = load_version_module(tmp_path, None, "version_manifest_is_dir")

    assert mod.resolve_version() == INSTALLED_VERSION


def test_malformed_manifest_raises(tmp_path: Path) -> None:
    """A corrupt SSOT is a loud failure, not a silent fall-through to a stale
    installed version that would ship under the wrong number."""
    (tmp_path / "pkg").mkdir()
    shutil.copy2(HOOKS_DIR / "_version.py", tmp_path / "pkg" / "_version.py")
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        "version_manifest_broken", tmp_path / "pkg" / "_version.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(json.JSONDecodeError):
        spec.loader.exec_module(mod)

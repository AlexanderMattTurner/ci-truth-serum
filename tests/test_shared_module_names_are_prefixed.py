"""A consumer's own module must not be able to shadow this package's helpers.

PROBLEM CLASS — each check runs as a direct script as well as under
``python -m``, so every module puts its own directory on ``sys.path`` and then
imports its siblings by bare name. That import reads ``sys.modules`` before it
reads ``sys.path``. A consumer whose repository also puts a scripts directory on
``sys.path`` and imports a module of the same name FIRST therefore owns the
cache entry, and this package silently binds to the consumer's module. The
``sys.path.insert`` prelude cannot prevent it: a cache hit never consults the
path at all.

The failure does not look like a collision. The module IS found, so the error
blames this package for a name it does define:

    ImportError: cannot import name 'safe_load' from '_fastyaml'
      (.../a-consumer/.github/scripts/_fastyaml.py)

Observed against agent-glovebox, whose ``.github/scripts/_fastyaml.py`` did
exactly this and reddened three of its required checks.

A relative import would end it outright, but it cannot be used here: the
``__main__`` guard of every check is driven as ``python ci_truth_serum/x.py``,
where a relative import raises ``attempted relative import with no known parent
package``. The ``_cts_`` prefix is what keeps these names off any name a
consumer plausibly picks.
"""

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ci_truth_serum

PACKAGE_DIR = Path(ci_truth_serum.__file__).resolve().parent
MODULES = sorted(p.stem for p in PACKAGE_DIR.glob("*.py") if p.stem != "__init__")
SIBLINGS = frozenset(MODULES)
PREFIX = "_cts_"
# The helper names this package used before the prefix, and the ones a consumer
# repository is most likely to pick for a module of its own.
OLD_NAMES = ["_fastyaml", "_linecheck", "_bash_ast", "_js_ast", "_registry"]


def _flat_sibling_imports(source: str) -> list[str]:
    """Every sibling module SOURCE imports by bare name, at any nesting depth."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module in SIBLINGS:
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name in SIBLINGS)
    return found


def test_the_package_has_modules_to_check() -> None:
    """Read no modules and every case below would pass over nothing."""
    assert len(MODULES) > 50, f"read only {len(MODULES)} modules from {PACKAGE_DIR}"


@pytest.mark.parametrize("module", MODULES)
def test_a_bare_sibling_import_names_a_prefixed_module(module: str) -> None:
    """A HELPER shared between checks carries the prefix, so nothing can shadow it.

    Scope is the leading-underscore helpers. A check module imported by another
    check (`sync_required_checks`, `run_tier`) is a weaker exposure — a consumer
    would have to name a module of its own after one of this pack's lints — and
    `sync_required_checks` is a published console script, so its name is API.
    """
    unprefixed = [
        name
        for name in _flat_sibling_imports((PACKAGE_DIR / f"{module}.py").read_text())
        if name.startswith("_") and not name.startswith(PREFIX)
    ]
    assert not unprefixed, (
        f"ci_truth_serum/{module}.py imports {unprefixed} by bare name. A consumer "
        f"module of that name on sys.path takes the sys.modules entry first and this "
        f"module binds to it. Rename the sibling to {PREFIX}<name> and update its "
        "importers."
    )


def _shadow_dir(where: Path, names: list[str]) -> Path:
    """A directory holding a hostile module for each of NAMES."""
    where.mkdir(parents=True, exist_ok=True)
    for name in names:
        # Importable, and carrying none of the names this package's own helper
        # exports — which is what agent-glovebox's module was. A module that
        # raised on import would kill the consumer's own import instead.
        (where / f"{name}.py").write_text(
            "SHADOW = 'a consumer module, not this package'\n"
        )
    return where


def _import_every_module_with(
    shadow: Path, names: list[str]
) -> subprocess.CompletedProcess[str]:
    """Import the whole package in a fresh interpreter whose sys.path holds SHADOW.

    A subprocess is the only honest way to run this. sys.modules in THIS process
    already holds the real siblings, so a cache that is correct before the test
    starts would hide the collision under test.
    """
    program = textwrap.dedent(f"""
        import sys, importlib, pkgutil
        sys.path.insert(0, {str(shadow)!r})
        for _name in {names!r}:
            importlib.import_module(_name)   # the consumer imports FIRST, as glovebox did
        import ci_truth_serum
        failed = []
        for m in pkgutil.iter_modules(ci_truth_serum.__path__):
            try:
                importlib.import_module("ci_truth_serum." + m.name)
            except Exception as exc:
                failed.append(f"{{m.name}}: {{type(exc).__name__}}: {{exc}}")
        print("\\n".join(failed))
        sys.exit(1 if failed else 0)
    """)
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        cwd=PACKAGE_DIR.parent,
    )


def test_the_package_survives_a_consumer_holding_the_old_names(tmp_path: Path) -> None:
    """The exact agent-glovebox collision: a consumer module at the pre-rename name."""
    shadow = _shadow_dir(tmp_path / "consumer", OLD_NAMES)
    done = _import_every_module_with(shadow, OLD_NAMES)
    assert done.returncode == 0, (
        f"a consumer module shadowed a package helper:\n{done.stdout}{done.stderr}"
    )


def test_a_consumer_holding_the_PREFIXED_names_still_breaks_it(tmp_path: Path) -> None:
    """The rename moves the names; it does not make bare imports safe.

    Without this the suite above passes against a package that resolves nothing
    from sys.path at all, so it would not notice the prefix being dropped again.
    """
    shadow = _shadow_dir(tmp_path / "hostile", [f"{PREFIX}fastyaml"])
    done = _import_every_module_with(shadow, [f"{PREFIX}fastyaml"])
    assert done.returncode != 0, (
        "shadowing the prefixed name changed nothing, so these imports no longer "
        "read sys.path and this file is testing a property the code lost."
    )

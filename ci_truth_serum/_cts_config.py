"""The per-check flags a consumer writes once, in `.ci-truth-serum.toml`.

A tier aggregate runs many checks under one hook id, so the hook's own `args:` cannot
say which flag belongs to which check. ``--check-arg <check>=<flag>`` can, but a long
list of them in `.pre-commit-config.yaml` is hard to read, and ``run_selection``
takes no ``--check-arg`` at all. This file is the one place for those flags:

    [checks.check-flag-arity]
    args = ["--wrapper=retry_cmd"]

Each table under ``checks`` names one aggregated check, by its hook id or its module
name. Its ``args`` are the tokens the check receives before its file arguments, one
token per item, in the order written. ``--check-arg`` flags follow them.

The file is read from the directory the hook runs in, which pre-commit sets to the
repository root. A missing file means no flags. Anything else that does not match
this shape is an error: a misspelled check name or key would otherwise configure
nothing, and the run would look configured.
"""

import sys
from collections.abc import Iterable
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

CONFIG_FILE = ".ci-truth-serum.toml"


class ConfigError(ValueError):
    """`.ci-truth-serum.toml` does not have the shape this module reads."""


def check_args(
    modules: Iterable[str], root: Path | None = None
) -> dict[str, list[str]]:
    """The flags `.ci-truth-serum.toml` under ROOT gives each check, keyed by module.

    MODULES is every check the file may name. Raises ConfigError for a file that does
    not parse, a key this module does not read, or a check outside MODULES.
    """
    path = (Path.cwd() if root is None else root) / CONFIG_FILE
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None
    extra_keys = set(data) - {"checks"}
    if extra_keys:
        raise ConfigError(
            f"{path}: unknown top-level key(s): {', '.join(sorted(extra_keys))}"
        )
    tables = data.get("checks", {})
    if not isinstance(tables, dict):
        raise ConfigError(f"{path}: `checks` must be a table of check tables")
    known = set(modules)
    flags: dict[str, list[str]] = {}
    for name, table in tables.items():
        module = name.replace("-", "_")
        if module not in known:
            raise ConfigError(
                f"{path}: [checks.{name}] names no check a tier aggregate runs; "
                "a standalone hook takes its flags in its own `args:`"
            )
        if module in flags:
            raise ConfigError(
                f"{path}: [checks.{name}] configures {module} a second time"
            )
        if not isinstance(table, dict) or set(table) - {"args"}:
            raise ConfigError(f"{path}: [checks.{name}] takes one key, `args`")
        args = table.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ConfigError(
                f"{path}: [checks.{name}] `args` must be a list of strings"
            )
        flags[module] = args
    return flags

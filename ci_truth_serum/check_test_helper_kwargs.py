#!/usr/bin/env python3
"""Flag a call in a scanned file that passes a keyword its resolvable helper
`def` does not accept.

WHY: a type checker that excludes a test directory (e.g. pyright's `[tool.
pyright] exclude`) gives a test's call sites no signature check at all — this
check is that check for the calls it CAN resolve.

Resolution is per-module, so a hit names a definite `def`:
  * Only a call whose callee is a plain `Name` — never `obj.method(...)`,
    whose binding this check cannot follow.
  * The name resolves through the calling module's OWN top-level `def`s
    first, then through `from <helper-package>.<module> import <name>` (with
    its `as` alias). `--helper-package NAME` (repeatable; default `tests`)
    names the import prefix a helper `def` is resolved through — the package
    root is walked whole, so a helper the argv files never mention is still
    resolvable. Nothing else is followed: a bare `import x.y`, a star import,
    or a name assigned from a call is left alone rather than guessed at.
  * A name also bound as a variable or parameter anywhere in the calling
    module is skipped — at the call site it may be a different object.

Opt a deliberate call out with `# allow-helper-kwargs: <reason>` on any line
it spans, or the line above. Exit 1 on any hit, 2 on an empty argv
(`run_file_cli`).
"""

import argparse
import ast
import gc
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
)

ALLOW = "allow-helper-kwargs"
DEFAULT_HELPER_PACKAGES = ("tests",)

FuncDef = ast.FunctionDef | ast.AsyncFunctionDef


class Finding(NamedTuple):
    """One call that does not match its callee's signature. `problem` reads as
    the predicate of a sentence whose subject is `callee()`."""

    path: str
    line: int
    callee: str
    problem: str


class Sig(NamedTuple):
    """What one `def` accepts, in the three shapes a call can violate.

    `keywords` is None when `**kwargs` swallows any name; `capacity` is None
    when `*args` swallows any count. `arity_known` is False for a decorated
    `def`, whose decorator may hand callers a different signature than the
    one written below it.
    """

    keywords: set[str] | None
    capacity: int | None
    required: set[str]
    positional: list[str]
    arity_known: bool


class CallSite(NamedTuple):
    """One call on a bare name, reduced to what judging it against a signature
    needs."""

    line: int
    callee: str
    named: frozenset[str]
    n_pos: int
    star_args: bool
    star_kwargs: bool


class Alias(NamedTuple):
    """One name a `from <module> import <original> as <bound>` binds in this
    module. Kept unresolved: the module it names may not be scanned yet."""

    module: str
    bound: str
    original: str


class Facts(NamedTuple):
    """What one module contributes to judging its OWN calls."""

    rel: str
    own: dict[str, Sig]
    imports: tuple[Alias, ...]
    shadowed: frozenset[str]
    calls: tuple[CallSite, ...]


def _signature(fn: FuncDef) -> Sig:
    """What `fn` accepts.

    `posonlyargs` is left out of `keywords`: a positional-only parameter
    cannot be passed by keyword, so counting its name would accept a call
    that raises at run time. It stays in `positional`, which is what a
    positional argument fills.

    A default makes a parameter optional. `args.defaults` covers the TAIL of
    posonly+args, and `kw_defaults` aligns one-to-one with kwonlyargs, where
    None means required.
    """
    a = fn.args
    by_position = [*a.posonlyargs, *a.args]
    n_optional = len(a.defaults)
    required = {p.arg for p in by_position[: len(by_position) - n_optional]}
    required |= {
        p.arg for p, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None
    }
    return Sig(
        keywords=None if a.kwarg else {p.arg for p in (*a.args, *a.kwonlyargs)},
        capacity=None if a.vararg else len(by_position),
        required=required,
        positional=[p.arg for p in by_position],
        arity_known=not fn.decorator_list,
    )


def _module_key(path: Path, root: Path, package: str) -> str:
    """`tests/_helpers.py` under root `tests` with package `tests` ->
    `tests._helpers`, the spelling its importers use."""
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join((package, *(p for p in parts if p != "__init__")))


def _is_fixture(fn: FuncDef) -> bool:
    """True for a `@pytest.fixture` (bare or called). A fixture's NAME at a
    call site is its yielded value, never the decorated function — a factory
    fixture is called as `pr_repo("README.md", msg=...)` while its own `def
    pr_repo(tmp_path)` takes neither argument."""
    for dec in fn.decorator_list:
        node = dec.func if isinstance(dec, ast.Call) else dec
        name = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
        if name == "fixture":
            return True
    return False


def _top_level_defs(tree: ast.Module) -> dict[str, Sig]:
    """Each top-level `def` this check will judge calls against.

    A name defined TWICE at module level is dropped rather than resolved to
    either one: which binding a call reaches depends on where it sits in
    execution order, and keeping the last `def` would judge an earlier call
    against a signature it never meant.
    """
    defs: dict[str, Sig] = {}
    duplicated: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _is_fixture(node):
            continue
        if node.name in defs:
            duplicated.add(node.name)
        defs[node.name] = _signature(node)
    return {name: sig for name, sig in defs.items() if name not in duplicated}


def _resolved(facts: Facts, defs: dict[str, dict[str, Sig]]) -> dict[str, Sig]:
    """The helpers this module can call by bare name, mapped to what each
    accepts. Own `def`s win over an import of the same name, matching Python:
    the later top-level binding is what a call in the module body reaches."""
    out: dict[str, Sig] = {}
    for alias in facts.imports:
        source = defs.get(alias.module)
        if source is not None and alias.original in source:
            out[alias.bound] = source[alias.original]
    out.update(facts.own)
    return out


def _mismatches(call: CallSite, sig: Sig) -> list[str]:
    """Every way this call fails to match `sig`, each phrased as a predicate.

    Each of the three is a TypeError at run time. Two call shapes hide what
    is supplied and disable the arity half: `f(*items)` makes the positional
    count unknown, and `f(**mapping)` can supply any required name.
    """
    problems: list[str] = []

    if sig.keywords is not None:
        problems += [
            f"has no parameter `{n}`" for n in sorted(call.named - sig.keywords)
        ]

    if not sig.arity_known or call.star_args:
        return problems

    if sig.capacity is not None and call.n_pos > sig.capacity:
        problems.append(
            f"takes {sig.capacity} positional argument(s), called with {call.n_pos}"
        )
    if not call.star_kwargs:
        supplied = call.named | set(sig.positional[: call.n_pos])
        problems += [f"needs `{n}`" for n in sorted(sig.required - supplied)]
    return problems


def _suppressed(node: ast.Call, lines: list[str]) -> bool:
    end = getattr(node, "end_lineno", None) or node.lineno
    return annotated_near(lines, node.lineno, ALLOW, span_end=end)


def _read_source(path: Path) -> tuple[str, ast.Module] | None:
    """(text, tree) for PATH, or None when it is unreadable or does not parse."""
    try:
        text = path.read_text(encoding="utf-8")
        return text, ast.parse(text)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def _default_repo_root() -> Path:
    """`git rev-parse --show-toplevel` — the tree `--helper-package` and argv
    paths resolve against by default."""
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return Path(out)


def build_helper_defs(
    repo_root: Path, packages: tuple[str, ...]
) -> dict[str, dict[str, Sig]]:
    """Every top-level `def` under each `--helper-package` root, keyed by the
    dotted module path its importers spell. The whole package tree is walked
    — not only the argv files — so a helper the scanned calls reference but
    never define is still resolvable."""
    defs: dict[str, dict[str, Sig]] = {}
    for package in packages:
        root = repo_root.joinpath(*package.split("."))
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            source = _read_source(path)
            if source is not None:
                defs[_module_key(path, root, package)] = _top_level_defs(source[1])
    return defs


def _calling_module_facts(path: Path) -> Facts | None:
    """Everything judging PATH's own calls needs, in a single `ast.walk`.

    None when PATH is unreadable or does not parse — the business of
    whatever lints syntax; this check still has an answer for every other
    file.
    """
    source = _read_source(path)
    if source is None:
        return None
    text, tree = source

    own = _top_level_defs(tree)
    lines = text.splitlines()
    imports: list[Alias] = []
    bound: set[str] = set()
    calls: list[CallSite] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                bound.add(node.id)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or _suppressed(node, lines):
                continue
            calls.append(
                CallSite(
                    line=node.lineno,
                    callee=node.func.id,
                    named=frozenset(
                        kw.arg for kw in node.keywords if kw.arg is not None
                    ),
                    n_pos=len(node.args),
                    star_args=any(isinstance(a, ast.Starred) for a in node.args),
                    star_kwargs=any(kw.arg is None for kw in node.keywords),
                )
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            bound.update(x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs))
            bound.update(x.arg for x in (a.vararg, a.kwarg) if x is not None)
            if not isinstance(node, ast.Lambda):
                bound.add(node.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            # A relative import (`from . import x`) has no module name, and
            # `defs` is keyed by one, so it could never resolve.
            imports.extend(
                Alias(
                    module=node.module,
                    bound=alias.asname or alias.name,
                    original=alias.name,
                )
                for alias in node.names
            )

    return Facts(
        rel=str(path),
        own=own,
        imports=tuple(imports),
        shadowed=frozenset(bound) - set(own),
        calls=tuple(calls),
    )


def findings(
    argv_paths: list[str], repo_root: Path, packages: tuple[str, ...]
) -> list[Finding]:
    """Every call in an ARGV path that does not match the signature it
    targets, resolved against every top-level `def` under PACKAGES."""
    helper_defs = build_helper_defs(repo_root, packages)
    hits: list[Finding] = []
    for path_str in argv_paths:
        facts = _calling_module_facts(Path(path_str))
        if facts is None:
            continue
        resolved = _resolved(facts, helper_defs)
        for call in facts.calls:
            if call.callee in facts.shadowed or call.callee not in resolved:
                continue
            hits.extend(
                Finding(facts.rel, call.line, call.callee, problem)
                for problem in _mismatches(call, resolved[call.callee])
            )
    return sorted(hits)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        help="the tree --helper-package and argv paths resolve against "
        "(default: `git rev-parse --show-toplevel`)",
    )
    parser.add_argument(
        "--helper-package",
        action="append",
        dest="helper_packages",
        metavar="NAME",
        help="the import prefix a helper `def` is resolved through "
        "(repeatable; default: tests)",
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)
    if not args.paths:
        print(
            "check_test_helper_kwargs: no files to scan. This check reads "
            "only the paths you give it, so an empty run would report a "
            "clean pass over nothing.",
            file=sys.stderr,
        )
        return 2

    repo_root = (
        Path(args.repo_root).resolve() if args.repo_root else _default_repo_root()
    )
    packages = tuple(args.helper_packages or DEFAULT_HELPER_PACKAGES)
    # THIS REFUSAL IS WHAT KEEPS A MISCONFIGURED PACKAGE FROM READING AS A
    # CLEAN TREE: a missing root walks zero files, so every call would go
    # unresolved and this would report zero findings forever.
    missing = [p for p in packages if not repo_root.joinpath(*p.split(".")).is_dir()]
    if missing:
        raise SystemExit(
            f"cannot check: {', '.join(missing)} is not a directory under {repo_root}"
        )

    py_paths = [p for p in args.paths if p.endswith(".py")]
    # `findings` parses every file under every helper package in one pass —
    # each AST forms a tree, never a cycle, so the cyclic collector re-walks
    # graphs refcounting already frees. Restored in `finally`: a test calls
    # `main` inside the pytest worker's own process, which needs its
    # collector back.
    gc.disable()
    try:
        hits = findings(py_paths, repo_root, packages)
    finally:
        gc.enable()
    if not hits:
        return 0
    print(
        "a call passes a keyword its helper `def` does not accept — "
        "that is a TypeError at run time, not a warning:",
        file=sys.stderr,
    )
    for hit in hits:
        print(f"  {hit.path}:{hit.line}: {hit.callee}() {hit.problem}", file=sys.stderr)
    print(
        "\nremedy: match the signature the helper declares, or annotate a "
        f"deliberate call `# {ALLOW}: <reason>`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

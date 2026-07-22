#!/usr/bin/env python3
"""
Catch a half-finished environment-variable rename: a prefixed env var that is
WRITTEN somewhere but never READ, or READ somewhere but never WRITTEN.

When a variable is renamed in the code that *sets* it but not in the code that
*reads* it (or vice-versa), nothing fails loudly: the reader just sees an unset
value and silently takes its default/empty branch. The two halves drift apart and
the feature quietly stops working. Scanning the whole tree for every var matching
a project prefix (e.g. ``GLOVEBOX_``) and asserting each has BOTH a write site and
a read site turns that silent drift into a pre-commit failure.

Parameterised by ``--prefix`` (required): only variables named ``<PREFIX>…`` (the
suffix all-uppercase) are considered, so the scan never touches unrelated env
vars. Detection is deliberately conservative to keep false positives near zero:

  WRITE  ``export X=…`` / ``X=… cmd`` (shell assignment, not ``==`` comparison);
         a YAML ``X: …`` scalar (an ``env:`` mapping entry); ``os.environ["X"] =``.
  READ   ``$X`` / ``${X…`` (shell); ``os.environ["X"]`` / ``os.environ.get("X"`` /
         ``os.getenv("X"`` (Python); ``process.env.X`` (JS/TS).

A dynamically-composed name (``${prefix}${suffix}``) is never matched — it is not a
literal token — so those are silently skipped. A var that is genuinely written or
read out of band (supplied by the CI environment, consumed by a templating layer
this scan can't see) opts out with a ``# env-symmetry-ok: <VARNAME> <reason>``
comment anywhere in the tree — the reason is REQUIRED.

Whole-tree by design: a write in one file and a read in another must be paired, so
the hook self-discovers the tracked files (``git ls-files``) and ignores the file
list pre-commit passes. Register it standalone with ``args: [--prefix, GLOVEBOX_]``.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path.cwd()

# A `# env-symmetry-ok: NAME <reason>` opt-out — NAME plus a non-empty reason.
_OPT_OUT = re.compile(r"#\s*env-symmetry-ok:\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+\S")


def _name_pattern(prefix: str) -> str:
    """A regex fragment matching a full ``<PREFIX><UPPER…>`` variable token: the
    prefix, at least one trailing uppercase/digit/underscore char, and NOT
    continued by a lowercase letter (so ``GLOVEBOX_Foo`` is not mis-split)."""
    return rf"{re.escape(prefix)}[A-Z0-9_]+(?![a-z])"


def find_writes(text: str, prefix: str, is_yaml: bool) -> set[str]:
    """Var names WRITTEN in TEXT: shell assignment, ``os.environ[...] =``, and —
    only for YAML — an ``env:``-style scalar key."""
    name = _name_pattern(prefix)
    writes: set[str] = set()
    # Shell assignment `X=…` / `export X=…` / `X=v cmd`: the name must not be
    # preceded by an identifier char or `$`/`{` (that would be a read/expansion),
    # and the `=` must not be `==` (a comparison).
    for m in re.finditer(rf"(?<![A-Za-z0-9_${{}}])(?P<n>{name})=(?!=)", text):
        writes.add(m.group("n"))
    # Python `os.environ["X"] = …` (subscript assignment).
    for m in re.finditer(
        rf"os\.environ\[\s*['\"](?P<n>{name})['\"]\s*\]\s*=(?!=)", text
    ):
        writes.add(m.group("n"))
    if is_yaml:
        # A YAML scalar assignment `X: value` — an env-map entry. Anchored to a
        # line so a `${{ env.X }}` reference elsewhere on the line is not caught.
        for m in re.finditer(rf"(?m)^\s*(?P<n>{name}):\s*\S", text):
            writes.add(m.group("n"))
    return writes


def find_reads(text: str, prefix: str) -> set[str]:
    """Var names READ in TEXT: shell ``$X``/``${X}``, Python ``os.environ``/
    ``os.getenv``, and JS ``process.env.X``."""
    name = _name_pattern(prefix)
    reads: set[str] = set()
    # Shell expansion `$X` or `${X…}` (the `{` form may carry `:-default` etc.).
    for m in re.finditer(rf"\$\{{?(?P<n>{name})\b", text):
        reads.add(m.group("n"))
    # Python `os.environ["X"]`, `os.environ.get("X"`, `os.getenv("X"`.
    for m in re.finditer(
        rf"os\.(?:environ\[|environ\.get\(|getenv\()\s*['\"](?P<n>{name})['\"]", text
    ):
        reads.add(m.group("n"))
    # JS/TS `process.env.X` or `process.env["X"]`.
    for m in re.finditer(
        rf"process\.env(?:\.(?P<n1>{name})\b|\[\s*['\"](?P<n2>{name})['\"])", text
    ):
        reads.add(m.group("n1") or m.group("n2"))
    return reads


def collect_optouts(text: str) -> set[str]:
    """Var names opted out via a reason-bearing ``# env-symmetry-ok: NAME …``."""
    return {m.group("name") for m in _OPT_OUT.finditer(text)}


def analyze(sources: dict[str, str], prefix: str) -> list[tuple[str, str, list[str]]]:
    """Given a map of path→text, return (name, kind, files) for each prefixed var
    that is write-only or read-only.

    ``kind`` is ``"write-only"`` (written, never read → the reader half is missing)
    or ``"read-only"`` (read, never written → the writer half is missing); ``files``
    are the paths carrying the present half, sorted.
    """
    writes: dict[str, set[str]] = {}
    reads: dict[str, set[str]] = {}
    optouts: set[str] = set()
    for path, text in sources.items():
        is_yaml = path.endswith((".yaml", ".yml"))
        for n in find_writes(text, prefix, is_yaml):
            writes.setdefault(n, set()).add(path)
        for n in find_reads(text, prefix):
            reads.setdefault(n, set()).add(path)
        optouts |= collect_optouts(text)

    results: list[tuple[str, str, list[str]]] = []
    for name in sorted(set(writes) | set(reads)):
        if name in optouts:
            continue
        if name in writes and name not in reads:
            results.append((name, "write-only", sorted(writes[name])))
        elif name in reads and name not in writes:
            results.append((name, "read-only", sorted(reads[name])))
    return results


def _tracked_sources(root: Path) -> dict[str, str]:
    """Every tracked file's text, keyed by repo-relative path; unreadable/binary
    files (decode failure) are skipped."""
    listing = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    sources: dict[str, str] = {}
    for rel in listing.split("\0"):
        if not rel:
            continue
        try:
            sources[rel] = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return sources


def _message(name: str, kind: str, files: list[str]) -> str:
    where = ", ".join(files)
    if kind == "write-only":
        return (
            f"{name} is WRITTEN ({where}) but never read anywhere in the tree — a "
            "reader still referencing the old name silently sees an unset value. "
            "Add the read site (or fix the reader's name), or annotate "
            f"'# env-symmetry-ok: {name} <reason>' if it is consumed out of band."
        )
    return (
        f"{name} is READ ({where}) but never written anywhere in the tree — the "
        "reader gets an unset value because the writer uses a different name. Add "
        f"the write site (or fix the writer's name), or annotate "
        f"'# env-symmetry-ok: {name} <reason>' if it is supplied externally."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="env-var write/read symmetry check")
    parser.add_argument(
        "--prefix", required=True, help="only vars named <PREFIX>… are checked"
    )
    parser.add_argument(
        "files", nargs="*", help="ignored — the tree is self-discovered"
    )
    args = parser.parse_args(argv)

    sources = _tracked_sources(REPO_ROOT)
    results = analyze(sources, args.prefix)
    for name, kind, files in results:
        print(f"::error::{_message(name, kind, files)}")
    if results:
        print(f"\nERROR: {len(results)} env-var symmetry violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

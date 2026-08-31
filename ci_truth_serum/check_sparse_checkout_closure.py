#!/usr/bin/env python3
"""Every sparse-checkout job's list must cover the files its own steps execute.

PROBLEM CLASS — a hand-maintained `sparse-checkout:` list versus the job's real
file closure. A job that checks out sparsely and then sources or invokes a
tracked file the list omits dies with "No such file or directory" on its first
reference, and nothing static sees it coming: sparse-checkout marks the
excluded entries SKIP_WORKTREE rather than removing them, so `git ls-files`
and every other check still read a complete tree — only the runner disagrees.

For each sparse-checkout step, this derives the job's dependencies from the
`run:` steps that execute against ITS checkout (up to the job's next full
checkout, or its end): a repo-relative path token under one of `--dep-dir`'s
directories names a file the tree must contain, and a local composite action
(`uses: ./dir`) names its whole directory. A Python file among them — or one
the sparse-checkout list names itself and a step hands to an interpreter — is
followed further through its own local imports (`_cts_py_imports.walk_imports`),
since a list that stops at the entry point still serves a tree that dies on
its first `import`.

An `import` and a `source` are the only two references either walk can
follow. A module that OPENS a file at run time — a helper that stats
`pyproject.toml` to find the repo root, a step that reads a template — writes
neither, so that file stays invisible here and the job dies on the runner the
first time that code path runs. A file in the closure declares such a path
with a `sparse-checkout-needs: <path> [<path>…]` comment, and this counts the
declared paths as dependencies of every job that reaches the declaring file.
The declaration is read from a real comment, through the language's own
grammar, and each path is repo-relative. A path that names no tracked file is
a violation: sparse-checkout serves tracked files alone, so a typo there would
widen nothing and say nothing.

The derivation must stay a WIDENING one — it may only raise the floor a
hand-written list must clear, never lower it. A `${{ }}` value or a wildcard
pattern is decided or matched at a level this cannot model, so that checkout
is skipped rather than judged against a guess; a step that merely reads a
`.py` file (`ruff check x.py`) without running it is not followed either.

Coverage is judged the way `git sparse-checkout` actually resolves a literal
(non-wildcard) pattern: in CONE mode a bare directory name anchors to the repo
root and a listed entry also covers the files directly inside its own
ancestor directories (the rungs git writes around `set A/B`); in NON-CONE mode
a slash-less pattern is gitignore-unanchored and matches at any depth, while a
pattern carrying a `/` is anchored either way.

Opt out with `# sparse-checkout-ok: <dep> <reason>` anywhere in the workflow —
one comment excuses that dependency for every checkout in the file. The
reason is mandatory; a marker without one is itself a violation.

Globs every workflow (not composite actions — a `sparse-checkout:` input lives
only on a job's own `actions/checkout` step); the passed file list is ignored.
"""

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    command_name,
    command_words,
    iter_nodes,
    node_text,
    parse as parse_bash,
    unquote,
)
from _cts_comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    workflow_files,
)
from _cts_py_imports import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    interpreter_scripts,
    walk_imports,
)

# One decoded JSON/YAML object whose keys this module does not model.
JsonObject = dict[str, Any]

OPT_OUT = "sparse-checkout-ok"
# `.github/scripts` is the one directory every consumer of this pack already
# treats as a source of referenced shell/Python entry points (see
# `check_path_gate_deps.py`'s `_SCRIPT_REF`). A consumer with more top-level
# source directories names them with a repeatable `--dep-dir`.
DEFAULT_DEP_DIRS = (".github/scripts",)

# A pattern this reader does not model exactly. A wildcard or a negation can
# widen or narrow the covered set either way, so a job carrying one is skipped
# rather than judged against a guess.
_UNMODELLED = ("*", "?", "[", "!")

_OPT_OUT_RE = re.compile(
    rf"#\s*{OPT_OUT}:\s*(?P<dep>\S+)[ \t]*(?P<reason>[^\n]*)", re.MULTILINE
)

NEEDS = "sparse-checkout-needs"

# `sparse-checkout-needs: <path> [<path>…]` — the paths one file in the closure
# declares it opens at run time. The pattern READS a value out of the
# annotation, so it is spelled here rather than built from
# `_cts_linecheck.annotation_re`, which models a boolean opt-out and captures
# nothing (see tests/cts/test_annotation_predicates.py). Its left edge is a
# token boundary, not a comment introducer: `_cts_comments` has already decided
# which text is a comment, and a JavaScript comment opens with `//`.
_NEEDS_RE = re.compile(rf"(?<![\w-]){re.escape(NEEDS)}:(?P<paths>[^\n]*)")


@dataclass(frozen=True)
class Checkout:
    """One job's sparse-checkout step, and the WINDOW of steps that run
    against that checkout's tree: everything after it, up to the job's next
    checkout that REPLACES the tree (or the job's end). A job that later
    re-checks out in full must judge each checkout only against the steps
    that actually run on its own tree, or a later full checkout's
    dependencies get demanded of the earlier sparse one."""

    workflow: Path
    job_name: str
    line: int
    window: tuple[JsonObject, ...]
    patterns: tuple[str, ...]
    cone: bool

    @property
    def where(self) -> str:
        return f"{self.workflow.name}:{self.job_name}"


def _is_checkout(step: JsonObject) -> bool:
    return str(step.get("uses") or "").startswith("actions/checkout")


def _window(steps: list[JsonObject], start: int) -> tuple[JsonObject, ...]:
    """The steps that run against a checkout's tree, from START onward. A
    checkout with `path:` clones beside the tree rather than replacing it, so
    it never ends the window. A CONDITIONAL replacing checkout might not run
    at all, so the window keeps going past it — except a later step whose
    `if:` is textually identical only runs together with it, against the
    tree IT produces, so it is excluded rather than judged against the tree
    here."""
    window: list[JsonObject] = []
    excluded_ifs: set[str] = set()
    for step in steps[start:]:
        condition = str(step.get("if") or "")
        if condition in excluded_ifs:
            continue
        if _is_checkout(step) and not (step.get("with") or {}).get("path"):
            if not condition:
                break
            excluded_ifs.add(condition)
            continue
        window.append(step)
    return tuple(window)


def checkouts(text: str, workflow: Path) -> list[Checkout] | None:
    """Every job in WORKFLOW's TEXT that checks out sparsely with a literal
    pattern list. A `${{ }}` value or an unmodelled pattern is decided or
    matched at a level this derivation does not read, so that job is skipped
    rather than judged against text that names no real path.

    None means the text is not readable as YAML. The caller reports that as a
    violation: no job was scanned, so an empty list would be a clean pass over
    a file this check never read."""
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return []
    found = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
        for index, step in enumerate(steps):
            if not _is_checkout(step):
                continue
            with_inputs = step.get("with") or {}
            raw = with_inputs.get("sparse-checkout")
            if not isinstance(raw, str) or not raw.strip() or "${{" in raw:
                continue
            if any(ch in raw for ch in _UNMODELLED):
                continue
            cone = (
                str(with_inputs.get("sparse-checkout-cone-mode", True)).lower()
                != "false"
            )
            window = _window(steps, index + 1)
            line = step.get("__line__", 1)
            found.append(
                Checkout(workflow, job_name, line, window, tuple(raw.split()), cone)
            )
    return found


def _cone_covers(patterns: tuple[str, ...], path: str, files: frozenset[str]) -> bool:
    """Cone mode's real pattern set, per `git sparse-checkout`'s internals: the
    listed directories, plus the files directly in each listed pattern's
    ancestor DIRECTORIES (the `/*`, `!/*/`, `/A/`, `!/A/*/` rungs git writes
    around a `set A/B`) and directly in the repo root.

    The ancestor rung fires only when the listed pattern is itself a
    DIRECTORY: a pattern that is a tracked FILE grants no such rung to its own
    siblings — listing `.github/scripts/render.py` makes render.py visible,
    not every other file in `.github/scripts/`, which is why FILES is asked
    here rather than treating every pattern as directory-shaped.
    """
    directory = path.rpartition("/")[0]
    if not directory:
        return True
    listed = [pattern.rstrip("/") for pattern in patterns]
    return any(
        path == pattern
        or path.startswith(f"{pattern}/")
        or (pattern.startswith(f"{directory}/") and pattern not in files)
        for pattern in listed
    )


def _noncone_covers(patterns: tuple[str, ...], path: str) -> bool:
    """Non-cone mode's gitignore semantics, restricted to the LITERAL patterns
    `_UNMODELLED` lets through: an anchored pattern (one carrying `/`) matches
    only that path or beneath it; a slash-less pattern is unanchored and
    matches when any path SEGMENT equals it, at any depth."""
    segments = path.split("/")
    for pattern in patterns:
        trimmed = pattern.rstrip("/")
        if "/" in trimmed:
            if path == trimmed or path.startswith(f"{trimmed}/"):
                return True
        elif trimmed in segments:
            return True
    return False


def covers(checkout: Checkout, path: str, files: frozenset[str]) -> bool:
    """Whether CHECKOUT's own sparse-checkout list includes PATH."""
    if checkout.cone:
        return _cone_covers(checkout.patterns, path, files)
    return _noncone_covers(checkout.patterns, path)


def _path_token_re(dep_dirs: tuple[str, ...]) -> "re.Pattern[str]":
    """A repo-relative path token rooted at one of DEP_DIRS. An empty DEP_DIRS
    matches nothing, rather than every `/`-joined token in the script."""
    if not dep_dirs:
        return re.compile(r"(?!)")
    dirs = "|".join(re.escape(d.rstrip("/")) for d in dep_dirs)
    return re.compile(rf"(?<![\w./])(?:{dirs})(?:/[\w.-]+)+")


def _dependencies(
    window: tuple[JsonObject, ...], path_token_re: "re.Pattern[str]"
) -> set[str]:
    deps: set[str] = set()
    for step in window:
        run = step.get("run")
        if isinstance(run, str):
            deps |= set(path_token_re.findall(run))
        uses = step.get("uses")
        if isinstance(uses, str) and uses.startswith("./"):
            deps.add(uses[2:].split("@", 1)[0].rstrip("/"))
    return deps


def _entrypoints(checkout: Checkout, files: frozenset[str]) -> list[str]:
    """The tracked Python files this job runs.

    A step that hands one to an interpreter names it on the command
    (`python3 .github/scripts/x.py`), which the bash grammar answers and a
    `.py` token in the text does not: `ruff check x.py` reads that file
    without importing anything. The rest are the sparse-checkout list's own
    entries — a step that invokes a script through a variable
    (`python3 "$DIR/render.py"`) puts no readable path on the command, so the
    list entry is the only place the job says it reads that file.
    """
    found = {pattern.rstrip("/") for pattern in checkout.patterns}
    for step in checkout.window:
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for node in iter_nodes(parse_bash(run), "command"):
            words = command_words(node)
            if words:
                found |= set(interpreter_scripts(words))
    return sorted(dep for dep in found if dep.endswith(".py") and dep in files)


def _imported(entrypoints: list[str], root: Path, files: frozenset[str]) -> set[str]:
    """Every tracked file ENTRYPOINTS import, transitively. A file outside
    this tree — reached through a `sys.path` insert that leaves the repo — is
    not something a sparse-checkout list can serve, so it is dropped rather
    than reported as a hole nobody can close."""
    _, visited = walk_imports([root / entry for entry in entrypoints])
    return {
        str(path.relative_to(root)) for path in visited if path.is_relative_to(root)
    } & files


# A `# shellcheck source=<path>` directive. The one place a script states, in
# text a tool can read, which file its `source` line reaches — `check-shell-
# source-declarations` is what makes every `source` carry one.
_SHELLCHECK_SOURCE = re.compile(r"#\s*shellcheck[^\n]*?\bsource=(?P<path>\S+)")

_SHELL_INTERPRETER = re.compile(r"(?:ba|z|k)?sh")
_SHELL_SUFFIX = re.compile(r"\.(?:sh|bash)$")


def _shell_entrypoints(checkout: Checkout, files: frozenset[str]) -> list[str]:
    """The tracked shell files this job runs.

    The same two sources as the Python entry points: a script a step hands to
    a shell (`bash .github/scripts/x.sh`) or runs directly (`./x.sh`), and the
    sparse-checkout list's own entries — a step that runs a script through a
    variable puts no readable path on the command.
    """
    found = {pattern.rstrip("/") for pattern in checkout.patterns}
    for step in checkout.window:
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for node in iter_nodes(parse_bash(run), "command"):
            words = command_words(node)
            if not words:
                continue
            name = words[0]
            if name is not None and _SHELL_SUFFIX.search(name):
                found.add(name.removeprefix("./"))
            if name is None or not _SHELL_INTERPRETER.fullmatch(
                name.rsplit("/", 1)[-1]
            ):
                continue
            for candidate in words[1:]:
                if candidate is None or candidate == "-c":
                    break
                if _SHELL_SUFFIX.search(candidate):
                    found.add(candidate.removeprefix("./"))
                    break
    return sorted(dep for dep in found if _SHELL_SUFFIX.search(dep) and dep in files)


def _source_targets(text: str) -> set[str]:
    """The path each `source` / `.` line in TEXT names, as written.

    A sourced path is almost never a bare literal: the idiom is
    `source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"`, whose value no static
    reader can compute. Two things are readable — the `# shellcheck source=`
    directive the script carries, and the literal SEGMENT after the last
    slash. `_resolve_source` decides which of those names a real file, so a
    guess that names nothing adds nothing.
    """
    targets = {match.group("path") for match in _SHELLCHECK_SOURCE.finditer(text)}
    for node in iter_nodes(parse_bash(text), "command"):
        if command_name(node) not in ("source", "."):
            continue
        # `command_arguments` yields the command NAME first, so the sourced
        # path is the word after it.
        arguments = command_arguments(node)[1:]
        if arguments:
            targets.add(unquote(node_text(arguments[0])))
    return targets


def _resolve_source(target: str, importer: str, files: frozenset[str]) -> str | None:
    """The tracked file TARGET names when IMPORTER sources it, else None.

    Tried against the repo root first, then IMPORTER's own directory, then —
    for a target an expansion decides — the last literal segment against that
    same directory. Every candidate must be a tracked file, so an unresolvable
    expansion drops out rather than becoming a hole nobody can close.
    """
    directory = importer.rsplit("/", 1)[0] if "/" in importer else ""
    tail = target.rsplit("/", 1)[-1]
    candidates = [
        _normalize(target),
        _normalize(f"{directory}/{target}" if directory else target),
        _normalize(f"{directory}/{tail}" if directory else tail),
    ]
    return next((c for c in candidates if c in files), None)


def _sourced(entrypoints: list[str], root: Path, files: frozenset[str]) -> set[str]:
    """Every tracked shell file ENTRYPOINTS source, transitively.

    A list that stops at the entry point serves a tree that dies on its first
    `source`, exactly as a Python list that stops before an `import` does.
    """
    seen: set[str] = set()
    pending = list(entrypoints)
    while pending:
        importer = pending.pop()
        try:
            text = (root / importer).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for target in _source_targets(text):
            resolved = _resolve_source(target, importer, files)
            if resolved is None or resolved in seen or resolved in entrypoints:
                continue
            seen.add(resolved)
            pending.append(resolved)
    return seen


@dataclass(frozen=True)
class Need:
    """One `sparse-checkout-needs:` declaration: a repo-relative PATH a file in
    the closure opens at run time, and the comment that declares it."""

    path: str
    declarer: str
    line: int


def declared_needs(reached: Iterable[str], root: Path) -> list[Need]:
    """Every `sparse-checkout-needs:` path the REACHED files declare.

    The import walk and the source walk find the files a job executes. A file
    one of them OPENS at run time is reachable by neither, so its own author
    names it here, and this is the only place the tree states that need.

    The declaration is read from a real comment, through the grammar
    `_cts_comments` picks for the path. A text scan would read the same words
    inside a string literal — an error message, a test fixture, a lint's own
    help text — as a claim about the tree.

    A declaration that names NO path yields one `Need` with an empty path: it
    is a marker that states nothing, and the caller reports it. Dropping it
    here would leave a typo looking like a declaration that works.

    An unreadable path is skipped: `git ls-files` reports the index, so a
    tracked path can be binary, or gone in a rename race.
    """
    found: list[Need] = []
    for rel in sorted(reached):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line, comment in sorted(comment_lines(text, rel).items()):
            for match in _NEEDS_RE.finditer(comment):
                paths = match.group("paths").split() or [""]
                found += [Need(_normalize(path), rel, line) for path in paths]
    return found


def _tracked(dep: str, files: frozenset[str]) -> bool:
    """A dependency exists when it is a tracked file or a tracked directory.
    `uses: ./actions/x` names a directory, so an exact-file test alone
    discards it and the job's sparse-checkout hole goes unreported."""
    return dep in files or any(rel.startswith(f"{dep}/") for rel in files)


def _normalize(dep: str) -> str:
    return dep.removeprefix("./").rstrip("/")


def suppressions(text: str) -> tuple[dict[str, str], list[str]]:
    """(dep -> reason, deps suppressed without a reason) from
    `# sparse-checkout-ok:` comments anywhere in the workflow's TEXT."""
    with_reason: dict[str, str] = {}
    reasonless: list[str] = []
    for match in _OPT_OUT_RE.finditer(text):
        dep = _normalize(match.group("dep"))
        reason = match.group("reason").strip()
        if reason:
            with_reason[dep] = reason
        else:
            reasonless.append(dep)
    return with_reason, reasonless


def uncovered(
    checkout: Checkout,
    files: frozenset[str],
    exempt: dict[str, str],
    root: Path,
    path_token_re: "re.Pattern[str]",
) -> tuple[list[str], list[Need]]:
    """(files the sparse-checkout list misses, declarations that name nothing).

    The first list holds the tracked files the job's own steps execute, or a
    reached file declares it opens, that the list does not cover and no
    `# sparse-checkout-ok:` comment excuses. The second holds every
    `sparse-checkout-needs:` path that is no tracked file: sparse-checkout
    serves tracked files alone, so such a declaration widens nothing, and the
    caller reports it rather than dropping it.
    """
    entrypoints = _entrypoints(checkout, files)
    shell_entrypoints = _shell_entrypoints(checkout, files)
    deps = _dependencies(checkout.window, path_token_re)
    deps |= _imported(entrypoints, root, files)
    deps |= _sourced(shell_entrypoints, root, files)
    # An entry point joins the SCAN set alone, never `deps`. A declaration
    # inside a script the job runs must count. The entry point itself is
    # already judged by `_dependencies` and by the list that names it, and this
    # derivation may only ADD the paths a declaration names.
    needs = declared_needs(deps | set(entrypoints) | set(shell_entrypoints), root)
    deps |= {need.path for need in needs if need.path}
    missing = sorted(
        dep
        for dep in deps
        if _tracked(dep, files)
        and dep not in exempt
        and not covers(checkout, dep, files)
    )
    return missing, [need for need in needs if not _tracked(need.path, files)]


def tracked_files(root: Path) -> frozenset[str]:
    """Every tracked path under ROOT, or none outside a repository."""
    proc = subprocess.run(
        ["git", "ls-files"], cwd=root, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return frozenset()
    return frozenset(line for line in proc.stdout.splitlines() if line)


def _git_repo_root() -> Path:
    """The repository the run reads, or the working directory outside one.

    A directory that is no git repository has no tracked file and no workflow,
    so the run has nothing to scan and says so. Raising here instead would make
    a check with nothing to do look like a broken hook."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return Path.cwd()
    return Path(proc.stdout.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="the repository root to scan (default: `git rev-parse --show-toplevel`)",
    )
    parser.add_argument(
        "--dep-dir",
        action="append",
        default=list(DEFAULT_DEP_DIRS),
        metavar="DIR",
        help="a repo-relative top-level directory a run: step's dependency "
        "path may start with (repeatable); extends the default "
        f"({DEFAULT_DEP_DIRS[0]}) rather than replacing it",
    )
    args = parser.parse_args(argv)
    root = args.repo_root or _git_repo_root()
    path_token_re = _path_token_re(tuple(args.dep_dir))
    files = tracked_files(root)

    total = 0
    # One broken declaration sits in a file many jobs reach, and it is the same
    # defect each time, so it is reported once.
    said: set[Need] = set()
    for workflow in workflow_files(root / ".github" / "workflows"):
        text = workflow.read_text(encoding="utf-8")
        found = checkouts(text, workflow)
        rel = workflow.relative_to(root)
        if found is None:
            print(
                f"::error file={rel}::could not parse as YAML; cannot verify "
                "that each sparse-checkout list covers what its job reads — fix "
                "the syntax (or run actionlint) and re-check."
            )
            total += 1
            continue
        if not found:
            continue
        exempt, reasonless = suppressions(text)
        for dep in reasonless:
            print(
                f"::error file={rel}::`# {OPT_OUT}: {dep}` has no reason — a "
                "suppression must say why the dependency is safe to leave out "
                f"of the sparse-checkout list (`# {OPT_OUT}: {dep} <reason>`)."
            )
            total += 1
        for checkout in found:
            # A step's `run:` block over _MAX_PIPE_BYTES of piped bytes fails the
            # grammar loudly rather than silently: skipping it would false-green
            # exactly the job this lint exists to read.
            try:
                missing, strays = uncovered(
                    checkout, files, exempt, root, path_token_re
                )
            except PathologicalInputError as err:
                print(
                    f"::error file={rel},line={checkout.line}::job "
                    f"{checkout.job_name}: {err}"
                )
                total += 1
                continue
            for need in strays:
                if need in said:
                    continue
                said.add(need)
                fault = (
                    f"`{NEEDS}: {need.path}` names no tracked file"
                    if need.path
                    else f"`{NEEDS}:` names no path"
                )
                print(
                    f"::error file={need.declarer},line={need.line}::{fault}. A "
                    "sparse-checkout list serves tracked files alone, so this "
                    "declaration widens nothing. Write each path relative to "
                    "the repository root, or drop the declaration."
                )
                total += 1
            for dep in missing:
                print(
                    f"::error file={rel},line={checkout.line}::job "
                    f"{checkout.job_name}: sparse-checkout list misses `{dep}`, "
                    "which this job's own steps reach — a step that sources, "
                    'invokes or opens it dies with "No such file or directory" '
                    "the first time that code path runs. Widen the sparse-checkout "
                    f"list, or annotate `# {OPT_OUT}: {dep} <reason>`."
                )
                total += 1
    if total:
        print(f"\nERROR: {total} sparse-checkout-closure violation(s) found.")
        print(
            "sparse-checkout sets SKIP_WORKTREE rather than removing an "
            "excluded entry, so every other check still sees a complete tree "
            "— only the runner disagrees, at the moment a step reaches it."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

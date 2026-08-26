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
followed further through its own local imports (`_py_imports.walk_imports`),
since a list that stops at the entry point still serves a tree that dies on
its first `import`.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    iter_nodes,
    parse as parse_bash,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    workflow_files,
)
from _py_imports import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
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
) -> list[str]:
    """Tracked files the job's own steps execute that its sparse-checkout list
    does not cover, and no `# sparse-checkout-ok:` comment excuses."""
    deps = _dependencies(checkout.window, path_token_re)
    deps |= _imported(_entrypoints(checkout, files), root, files)
    return sorted(
        dep
        for dep in deps
        if _tracked(dep, files)
        and dep not in exempt
        and not covers(checkout, dep, files)
    )


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
            missing = uncovered(checkout, files, exempt, root, path_token_re)
            for dep in missing:
                print(
                    f"::error file={rel},line={checkout.line}::job "
                    f"{checkout.job_name}: sparse-checkout list misses `{dep}`, "
                    "which this job's own steps execute — a step that sources "
                    'or invokes it dies with "No such file or directory" the '
                    "first time that code path runs. Widen the sparse-checkout "
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

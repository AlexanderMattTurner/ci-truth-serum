#!/usr/bin/env python3
"""A script run from a secondary checkout must not import a package nobody installed.

PROBLEM CLASS — a job checks out a second tree beside its own with
`actions/checkout` + `with.path:` (a pinned copy of the CI scripts, a tools
repo), then runs a Node script out of it. That tree carries no `node_modules`:
the job's install step, when it has one, ran against the main workspace, and a
job with no install step has none at all. A bare import in that script
(`import { parse } from "smol-toml"`) is then `ERR_MODULE_NOT_FOUND` the
moment Node loads it. Nothing static sees it coming — the same file passes
every lint and every test when it runs from the main tree, where the packages
are installed.

One worked case. A changelog gate ran
`bash _ci_scripts/.github/scripts/pr/changelog-gate.sh`, a shell script whose
last line is `exec node "$SCRIPTS/checks/changelog-fragment.mjs"`. The script
imported `smol-toml`. The job checked out `_ci_scripts` from the default
branch, set up `uv`, and installed nothing for Node. The required check went
red on every pull request, and the log named a module nobody had removed.

The rule. For each job that checks out a tree with a literal `with.path:` and
INSTALLS NO NODE DEPENDENCIES anywhere in the job, every JavaScript or
TypeScript file the job executes out of that tree is read through the
ECMAScript grammar, and each static import specifier that is neither relative,
nor absolute, nor a `node:` builtin, nor a `#subpath`, is a violation. Both
conditions must hold: a job that runs `npm ci`, `pnpm install`, `yarn`,
`bun install`, or uses `actions/setup-node`, `pnpm/action-setup` or
`oven-sh/setup-bun`, has decided its own dependencies and is out of scope.

What counts as executing a file out of the tree, all read from the `run:`
body's bash grammar rather than its text:

  * `node <dir>/x.mjs`, `bun <dir>/x.ts`, `tsx <dir>/x.ts`, and the file run
    directly (`<dir>/x.mjs`, `./<dir>/x.mjs`), through `exec`, `command`,
    `env` or `corepack` in front.
  * One shell hop: `bash <dir>/x.sh`, `sh`, `source`, `.`, or the script run
    directly. That script is parsed in turn, and each `node`/`bun`/`tsx`
    command in it names the JavaScript file. Its operand is usually built from
    a variable (`"$SCRIPTS/checks/x.mjs"`), so the literal trailing path
    segments are resolved against the shell script's own directory and its
    ancestors, then against every tracked file by suffix. A second hop is not
    followed.

Blind spots, each deliberate:

  * A `with.path:` or a script path that carries a `${{ }}` expression or a
    shell expansion is decided at a level this cannot model, and is skipped.
  * A checkout of ANOTHER repository (`with.repository:`) serves files this
    tree does not hold, and is skipped.
  * A dynamic `import(name)` with a computed argument has no static target.
  * A `node_modules` directory committed into the tree would make the import
    work; this check does not look for one.

Opt out per step with `# allow-secondary-checkout-import: <reason>` on or
above the step that runs the script, or on or above the checkout step whose
tree it runs from. The reason is mandatory.

Globs every workflow (a `path:` checkout lives only on a job's own
`actions/checkout` step); the passed file list is ignored.
"""

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    parse as parse_bash,
    unquote,
)
from _cts_js_ast import is_js_source  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    annotated_near,
    workflow_files,
)
from check_relative_imports import specifiers  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_sparse_checkout_closure import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _git_repo_root,
    tracked_files,
)

JsonObject = dict[str, Any]

OPT_OUT = "allow-secondary-checkout-import"

# The actions and commands that give a job's Node scripts their dependencies.
# A job carrying any of these has decided its own dependencies and is out of
# scope, whether or not the install reached the secondary tree.
_INSTALLER_ACTIONS = ("actions/setup-node", "pnpm/action-setup", "oven-sh/setup-bun")
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_INSTALL_VERBS = frozenset({"ci", "install", "i", "add"})

# Words that run the command after them in the same argv position.
_WRAPPERS = frozenset({"exec", "command", "env", "corepack", "nice", "time"})
_JS_RUNNERS = frozenset({"node", "bun", "tsx", "ts-node", "deno"})
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh", "source", "."})
_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts")
_SHELL_SUFFIXES = (".sh", ".bash")
# Characters whose presence in a path segment means the shell computes it.
_EXPANSION_CHARS = frozenset("$`(){}*?")

# Node's own module names, as `require('module').builtinModules` lists them on
# Node 22. A specifier in this set, with or without the `node:` prefix, needs
# no package and never fails to resolve.
NODE_BUILTINS = frozenset(
    """
    assert assert/strict async_hooks buffer child_process cluster console
    constants crypto dgram diagnostics_channel dns dns/promises domain events fs
    fs/promises http http2 https inspector inspector/promises module net os path
    path/posix path/win32 perf_hooks process punycode querystring readline
    readline/promises repl stream stream/consumers stream/promises stream/web
    string_decoder sys timers timers/promises tls trace_events tty url util
    util/types v8 vm wasi worker_threads zlib
    """.split()
)

# A specifier opening with one of these resolves without a package.
_NOT_BARE_PREFIXES = ("./", "../", "/", "node:", "#", "data:", "file:")


@dataclass(frozen=True)
class Execution:
    """One file a job runs out of a secondary checkout: the JOB, the checkout's
    `with.path:` DIRECTORY, the tree-relative PATH of the file, the 1-based LINE
    of the step that runs it, and the CHECKOUT_LINE of the checkout step."""

    job: str
    directory: str
    path: str
    line: int
    checkout_line: int


def is_bare(specifier: str) -> bool:
    """True when SPECIFIER resolves through `node_modules` and nowhere else."""
    if specifier in (".", "..") or specifier.startswith(_NOT_BARE_PREFIXES):
        return False
    name = specifier.split("?", 1)[0]
    return name not in NODE_BUILTINS


def _plain_words(command) -> list[str]:
    """COMMAND's words with quotes and wrapper programs stripped, so the head is
    the program that runs. After `env`, its `K=V` assignments and flags go too."""
    words = [unquote(word) for word in command_words(command)]
    while words and words[0] in _WRAPPERS:
        wrapper = words.pop(0)
        if wrapper == "env":
            while words and ("=" in words[0] or words[0].startswith("-")):
                words.pop(0)
    return words


def _operand(words: list[str]) -> str:
    """The first word after the program that is neither a flag nor `run`."""
    return next((w for w in words[1:] if not w.startswith("-") and w != "run"), "")


def _installs_dependencies(steps: list[JsonObject]) -> bool:
    """True when any step in the job installs Node dependencies."""
    for step in steps:
        uses = str(step.get("uses") or "")
        if uses.split("@", 1)[0] in _INSTALLER_ACTIONS:
            return True
        run = step.get("run")
        if not isinstance(run, str):
            continue
        for command in iter_nodes(parse_bash(run), "command"):
            words = _plain_words(command)
            if not words:
                continue
            program = words[0].rsplit("/", 1)[-1]
            if program not in _PACKAGE_MANAGERS:
                continue
            verb = next((w for w in words[1:] if not w.startswith("-")), "")
            if verb in _INSTALL_VERBS or (program == "yarn" and not verb):
                return True
    return False


def _secondary_checkouts(steps: list[JsonObject]) -> list[tuple[str, int]]:
    """Each (with.path, step line) of a checkout of THIS repository into a
    literal directory. An expression or another repository is skipped."""
    found = []
    for step in steps:
        if not str(step.get("uses") or "").startswith("actions/checkout"):
            continue
        with_inputs = step.get("with") or {}
        path = with_inputs.get("path")
        if not isinstance(path, str) or "${{" in path:
            continue
        # `path: .` is the workspace itself, a full checkout by another name.
        if path.strip().strip("/") in ("", "."):
            continue
        if with_inputs.get("repository"):
            continue
        found.append((path.strip().strip("/"), step.get("__line__", 1)))
    return found


def _under(word: str, directory: str) -> str | None:
    """WORD's tree-relative path when WORD names a file under DIRECTORY, else
    None. A word the shell computes is not a path this can read."""
    if _EXPANSION_CHARS & set(word):
        return None
    for prefix in (f"{directory}/", f"./{directory}/"):
        if word.startswith(prefix):
            return word[len(prefix) :]
    return None


def _executed(run: str, directory: str) -> list[str]:
    """The tree-relative paths RUN executes out of DIRECTORY: a JavaScript file
    handed to a runner or run directly, and a shell script handed to a shell or
    run directly. The caller follows the shell script one hop."""
    found: list[str] = []
    for command in iter_nodes(parse_bash(run), "command"):
        words = _plain_words(command)
        if not words:
            continue
        program = words[0].rsplit("/", 1)[-1]
        if program in _JS_RUNNERS or program in _SHELL_RUNNERS:
            candidate = _operand(words)
        else:
            candidate = words[0]
        path = _under(candidate, directory)
        if path is not None and path.endswith(_JS_SUFFIXES + _SHELL_SUFFIXES):
            found.append(path)
    return found


def executions(text: str, workflow: Path) -> list[Execution] | None:
    """Every file a job in WORKFLOW's TEXT runs out of a secondary checkout
    while installing no Node dependencies. None means the text is not readable
    as YAML, which the caller reports: an empty list would be a clean pass over
    a file this check never read."""
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return []
    found: list[Execution] = []
    for job_name, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        raw_steps = job.get("steps")
        if not isinstance(raw_steps, list):
            continue
        steps = [s for s in raw_steps if isinstance(s, dict)]
        checkouts = _secondary_checkouts(steps)
        if not checkouts or _installs_dependencies(steps):
            continue
        for step in steps:
            run = step.get("run")
            if not isinstance(run, str):
                continue
            line = step.get("__line__", 1)
            for directory, checkout_line in checkouts:
                found += [
                    Execution(str(job_name), directory, path, line, checkout_line)
                    for path in _executed(run, directory)
                ]
    return found


def _literal_tail(word: str) -> str:
    """The trailing path segments of WORD that the shell does not compute:
    `"$SCRIPTS/checks/x.mjs"` gives `checks/x.mjs`."""
    segments = unquote(word).split("/")
    tail: list[str] = []
    for segment in reversed(segments):
        if _EXPANSION_CHARS & set(segment):
            break
        tail.insert(0, segment)
    return "/".join(tail)


def _resolve_tail(tail: str, script: str, files: frozenset[str]) -> str | None:
    """The tracked file TAIL names when SCRIPT runs it: tried against SCRIPT's
    own directory and each ancestor up to the root, then by suffix across the
    whole tree. A tail that names several tracked files is ambiguous and
    resolves to none."""
    if not tail:
        return None
    directory = script.rsplit("/", 1)[0] if "/" in script else ""
    while True:
        candidate = f"{directory}/{tail}" if directory else tail
        if candidate in files:
            return candidate
        if not directory:
            break
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
    matches = [f for f in files if f == tail or f.endswith(f"/{tail}")]
    return matches[0] if len(matches) == 1 else None


def scripts_run_by_shell(text: str, script: str, files: frozenset[str]) -> list[str]:
    """The tracked JavaScript files the shell SCRIPT with TEXT hands to a
    runner. Reads the bash grammar; a `${{ }}`-free script has nothing to
    neutralize."""
    found: list[str] = []
    for command in iter_nodes(parse_bash(text), "command"):
        words = _plain_words(command)
        if not words or words[0].rsplit("/", 1)[-1] not in _JS_RUNNERS:
            continue
        operand = _operand(words)
        if not operand.endswith(_JS_SUFFIXES):
            continue
        resolved = _resolve_tail(_literal_tail(operand), script, files)
        if resolved is not None and resolved not in found:
            found.append(resolved)
    return found


def javascript_files(
    execution: Execution, root: Path, files: frozenset[str]
) -> list[str]:
    """The tracked JavaScript files EXECUTION reaches: the file itself, or the
    ones a shell script hands to a runner, one hop down."""
    path = execution.path
    if path not in files:
        return []
    if is_js_source(path):
        return [path]
    text = (root / path).read_text(encoding="utf-8", errors="replace")
    return scripts_run_by_shell(text, path, files)


def bare_imports(source: str, path: str) -> list[tuple[int, str]]:
    """Every (1-based line, specifier) in SOURCE that needs a package."""
    return [(line, spec) for line, spec in specifiers(source, path) if is_bare(spec)]


def _excused(lines: list[str], execution: Execution) -> bool:
    return any(
        annotated_near(lines, lineno, OPT_OUT)
        for lineno in (execution.line, execution.checkout_line)
        if lineno <= len(lines)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="the repository root to scan (default: `git rev-parse --show-toplevel`)",
    )
    args = parser.parse_args(argv)
    root = args.repo_root or _git_repo_root()
    files = tracked_files(root)

    total = 0
    for workflow in workflow_files(root / ".github" / "workflows"):
        text = workflow.read_text(encoding="utf-8")
        rel = workflow.relative_to(root)
        try:
            found = executions(text, workflow)
        except PathologicalInputError as err:
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        if found is None:
            print(
                f"::error file={rel}::could not parse as YAML; cannot verify "
                "that each script run from a secondary checkout imports only "
                "what is installed — fix the syntax (or run actionlint) and "
                "re-check."
            )
            total += 1
            continue
        lines = text.split("\n")
        for execution in found:
            if _excused(lines, execution):
                continue
            # The shell hop parses a tracked script with the same grammar, and
            # one past its size bound fails loudly rather than reading as clean.
            try:
                scripts = javascript_files(execution, root, files)
            except PathologicalInputError as err:
                print(f"::error file={rel},line={execution.line}::{err}")
                total += 1
                continue
            for script in scripts:
                source = (root / script).read_text(encoding="utf-8", errors="replace")
                for line, specifier in bare_imports(source, script):
                    print(
                        f"::error file={rel},line={execution.line}::job "
                        f"{execution.job}: `{script}:{line}` imports "
                        f'"{specifier}", but this step runs it out of the '
                        f"`{execution.directory}` checkout and the job installs "
                        "no Node dependencies, so the import is "
                        "ERR_MODULE_NOT_FOUND the moment Node loads it. Install "
                        "the dependencies in this job, run the script from the "
                        "main checkout, or drop the import — or annotate "
                        f"`# {OPT_OUT}: <reason>` on the step."
                    )
                    total += 1
    if total:
        print(f"\nERROR: {total} secondary-checkout-import violation(s) found.")
        print(
            "A tree checked out with `path:` has no node_modules of its own; the "
            "script fails only on the runner, where nobody is watching for it."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

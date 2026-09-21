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
ECMAScript grammar, and each static specifier the runtime cannot resolve on its
own is a violation. A relative path, an absolute path, a `node:` builtin and a
`#subpath` always resolve. The runner decides the rest: deno resolves `npm:`,
`jsr:` and a URL through its own cache, and bun resolves `bun:`. A `require()`
argument counts, and a TypeScript type-only import does not — the runtime
erases it. Both conditions must hold: a job that runs `npm ci`, `pnpm install`,
`yarn`, `bun install`, or uses `actions/setup-node`, `pnpm/action-setup` or
`oven-sh/setup-bun`, has decided its own dependencies and is out of scope. A
step with `if: false` never runs, so it neither executes a file nor installs.

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
    followed. A shell script the bash grammar cannot read is reported, never
    passed.
  * The tracked siblings each of those files relative-imports, one level down.
    The runtime loads a sibling while it loads the entrypoint.

A step's `working-directory`, and the `defaults.run.working-directory` of its
job or of the workflow, move where a relative operand starts.

Blind spots, each deliberate:

  * A `with.path:` or a script path that carries a `${{ }}` expression or a
    shell expansion is decided at a level this cannot model, and is skipped.
  * A checkout of ANOTHER repository (`with.repository:`) serves files this
    tree does not hold, and is skipped. `repository: ${{ github.repository }}`
    names this repository, so it is read.
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
import posixpath
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    UnparseableShellError,
    assert_parseable,
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
from check_relative_imports import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    require_specifiers,
    runtime_specifiers,
)
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
# Package-manager options that consume the word after them. The install verb is
# the first operand, and an option's value is not an operand: `npm --prefix _ci
# ci` installs.
_VALUE_OPTIONS = frozenset(
    {
        "--prefix",
        "-C",
        "--dir",
        "--cwd",
        "--filter",
        "-F",
        "--workspace",
        "-w",
        "--registry",
        "--loglevel",
        "--config",
        "--userconfig",
        "--cache",
    }
)

# Words that run the command after them in the same argv position, each mapped
# to its own options that take a value. That value is not the program the
# wrapper runs.
_WRAPPER_VALUE_OPTIONS = {
    "exec": ("-a",),
    "command": (),
    "env": ("-u", "--unset", "-C", "--chdir", "-S", "--split-string"),
    "corepack": (),
    "nice": ("-n", "--adjustment"),
    "time": ("-o", "--output", "-f", "--format"),
}
_WRAPPERS = frozenset(_WRAPPER_VALUE_OPTIONS)
_JS_RUNNERS = frozenset({"node", "bun", "tsx", "ts-node", "deno"})
_SHELL_RUNNERS = frozenset({"bash", "sh", "zsh", "source", "."})
_JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
_SHELL_SUFFIXES = (".sh", ".bash")
# The runtime of a file the step runs with no runner word in front of it.
_DEFAULT_JS_RUNNER = "node"
_DEFAULT_SHELL_RUNNER = "bash"
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

# A specifier opening with one of these resolves without a package, whatever
# runs the file.
_NOT_BARE_PREFIXES = ("./", "../", "/", "node:", "#", "data:", "file:")
# What each runner resolves by itself, beyond the prefixes above. Deno fetches
# `npm:`, `jsr:` and a URL into its own global cache; Bun serves `bun:`.
_RUNNER_PREFIXES = {
    "deno": ("npm:", "jsr:", "http:", "https:"),
    "bun": ("bun:",),
}


@dataclass(frozen=True)
class Execution:
    """One file a job runs out of a secondary checkout: the JOB, the checkout's
    `with.path:` DIRECTORY, the tree-relative PATH of the file, the RUNNER that
    runs it, the 1-based LINE of the step that runs it, and the CHECKOUT_LINE of
    the checkout step."""

    job: str
    directory: str
    path: str
    runner: str
    line: int
    checkout_line: int


def is_bare(specifier: str, runner: str = _DEFAULT_JS_RUNNER) -> bool:
    """True when SPECIFIER resolves through `node_modules` and nowhere else.
    RUNNER decides it: deno and bun resolve schemes node cannot."""
    prefixes = _NOT_BARE_PREFIXES + _RUNNER_PREFIXES.get(runner, ())
    if specifier in (".", "..") or specifier.startswith(prefixes):
        return False
    name = specifier.split("?", 1)[0]
    return name not in NODE_BUILTINS


def _plain_words(command) -> list[str]:
    """COMMAND's words with quotes and wrapper programs stripped, so the head is
    the program that runs.

    Each wrapper's own options go too, with the value of an option that takes
    one: `command -p node x.mjs` and `nice -n 5 node x.mjs` both run node. An
    empty list means the command runs no program at all.
    """
    words = [unquote(word) for word in command_words(command)]
    while words and words[0].rsplit("/", 1)[-1] in _WRAPPERS:
        wrapper = words.pop(0).rsplit("/", 1)[-1]
        value_options = _WRAPPER_VALUE_OPTIONS[wrapper]
        while words and (
            words[0].startswith("-") or (wrapper == "env" and "=" in words[0])
        ):
            option = words.pop(0)
            # `command -v` / `-V` prints where a program lives and runs nothing.
            if wrapper == "command" and not option.startswith("--"):
                if set(option[1:]) & set("vV"):
                    return []
            if option in value_options and words:
                words.pop(0)
    return words


def _operand(words: list[str]) -> str:
    """The first word after the program that is neither a flag nor `run`."""
    return next((w for w in words[1:] if not w.startswith("-") and w != "run"), "")


def _package_operands(words: list[str]) -> list[str]:
    """WORDS with every option, and the value an option takes, removed. What is
    left starts with the subcommand: `--prefix _ci ci` gives `["ci"]`."""
    operands: list[str] = []
    index = 0
    while index < len(words):
        word = words[index]
        index += 1
        if not word.startswith("-"):
            operands.append(word)
        elif word in _VALUE_OPTIONS:
            index += 1
    return operands


def _never_runs(step: JsonObject) -> bool:
    """True when STEP's `if:` is the constant false. GitHub never runs the step,
    so it neither executes a script nor installs a dependency."""
    condition = step.get("if")
    if isinstance(condition, bool):
        return not condition
    if not isinstance(condition, str):
        return False
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text.lower() == "false"


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
            operands = _package_operands(words[1:])
            verb = operands[0] if operands else ""
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
        if not _is_this_repository(with_inputs.get("repository")):
            continue
        found.append((path.strip().strip("/"), step.get("__line__", 1)))
    return found


def _is_this_repository(repository: Any) -> bool:
    """True when a checkout's `repository:` input names the repository under
    test. An absent input and `${{ github.repository }}` both name it; any other
    value serves files this tree does not hold."""
    if not repository:
        return True
    text = str(repository).strip()
    if text.startswith("${{") and text.endswith("}}"):
        return text[3:-2].strip() == "github.repository"
    return False


def _under(word: str, directory: str) -> str | None:
    """WORD's tree-relative path when WORD names a file under DIRECTORY, else
    None. A word the shell computes is not a path this can read."""
    if _EXPANSION_CHARS & set(word):
        return None
    for prefix in (f"{directory}/", f"./{directory}/"):
        if word.startswith(prefix):
            return word[len(prefix) :]
    return None


def _at(word: str, working_directory: str) -> str:
    """WORD as a workspace-relative path when the step runs in
    WORKING_DIRECTORY. An absolute path and a word the shell computes stay as
    written."""
    if not working_directory or word.startswith("/") or _EXPANSION_CHARS & set(word):
        return word
    return posixpath.normpath(f"{working_directory}/{word}")


def _executed(
    run: str, directory: str, working_directory: str = ""
) -> list[tuple[str, str]]:
    """Each (tree-relative path, runner) RUN executes out of DIRECTORY: a
    JavaScript file handed to a runner or run directly, and a shell script handed
    to a shell or run directly. The caller follows the shell script one hop. The
    step runs in WORKING_DIRECTORY, so a relative operand starts there."""
    found: list[tuple[str, str]] = []
    for command in iter_nodes(parse_bash(run), "command"):
        words = _plain_words(command)
        if not words:
            continue
        program = words[0].rsplit("/", 1)[-1]
        named_runner = program in _JS_RUNNERS or program in _SHELL_RUNNERS
        candidate = _operand(words) if named_runner else words[0]
        path = _under(_at(candidate, working_directory), directory)
        if path is None:
            continue
        if path.endswith(_JS_SUFFIXES):
            found.append((path, program if named_runner else _DEFAULT_JS_RUNNER))
        elif path.endswith(_SHELL_SUFFIXES):
            found.append((path, program if named_runner else _DEFAULT_SHELL_RUNNER))
    return found


def _defaults_working_directory(holder: JsonObject) -> str | None:
    """The `defaults.run.working-directory` HOLDER sets, else None."""
    defaults = holder.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    value = run.get("working-directory") if isinstance(run, dict) else None
    return value if isinstance(value, str) else None


def _working_directory(
    doc: JsonObject, job: JsonObject, step: JsonObject
) -> str | None:
    """Where STEP's `run:` body executes, relative to the workspace: the step's
    own `working-directory`, else the job's `defaults.run`, else the workflow's.
    None means an expression decides it, so no path in the body can be read."""
    own = step.get("working-directory")
    candidates = [
        own if isinstance(own, str) else None,
        _defaults_working_directory(job),
        _defaults_working_directory(doc),
    ]
    value = next((c for c in candidates if c is not None), "")
    return None if "${{" in value else value.strip().strip("/")


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
        steps = [s for s in raw_steps if isinstance(s, dict) and not _never_runs(s)]
        checkouts = _secondary_checkouts(steps)
        if not checkouts or _installs_dependencies(steps):
            continue
        for step in steps:
            run = step.get("run")
            if not isinstance(run, str):
                continue
            working_directory = _working_directory(doc, job, step)
            if working_directory is None:
                continue
            line = step.get("__line__", 1)
            for directory, checkout_line in checkouts:
                found += [
                    Execution(
                        str(job_name), directory, path, runner, line, checkout_line
                    )
                    for path, runner in _executed(run, directory, working_directory)
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
    whole tree. Each candidate is normalized, so a `..` segment walks up a
    directory rather than becoming a path `git ls-files` never emits. A tail
    that names several tracked files is ambiguous and resolves to none."""
    if not tail:
        return None
    directory = script.rsplit("/", 1)[0] if "/" in script else ""
    while True:
        candidate = posixpath.normpath(f"{directory}/{tail}" if directory else tail)
        if candidate in files:
            return candidate
        if not directory:
            break
        directory = directory.rsplit("/", 1)[0] if "/" in directory else ""
    matches = [f for f in files if f == tail or f.endswith(f"/{tail}")]
    return matches[0] if len(matches) == 1 else None


def scripts_run_by_shell(
    text: str, script: str, files: frozenset[str]
) -> list[tuple[str, str]]:
    """Each (tracked JavaScript file, runner) the shell SCRIPT with TEXT hands to
    a runner.

    Reads the bash grammar. A file the grammar cannot read raises
    `UnparseableShellError`: tree-sitter recovers from an unparsed construct by
    dropping later nodes, so an empty result would be a clean pass over a script
    this never inspected.
    """
    assert_parseable(text)
    found: list[tuple[str, str]] = []
    for command in iter_nodes(parse_bash(text), "command"):
        words = _plain_words(command)
        if not words:
            continue
        runner = words[0].rsplit("/", 1)[-1]
        if runner not in _JS_RUNNERS:
            continue
        operand = _operand(words)
        if not operand.endswith(_JS_SUFFIXES):
            continue
        resolved = _resolve_tail(_literal_tail(operand), script, files)
        if resolved is not None and (resolved, runner) not in found:
            found.append((resolved, runner))
    return found


def _relative_target(specifier: str, importer: str) -> str | None:
    """The tree-relative file SPECIFIER names when IMPORTER loads it, or None
    when the specifier names a package rather than a path."""
    if not specifier.startswith("."):
        return None
    bare = specifier.split("?", 1)[0].split("#", 1)[0]
    return posixpath.normpath(posixpath.join(posixpath.dirname(importer), bare))


def _relative_closure(
    entries: list[tuple[str, str]], root: Path, files: frozenset[str]
) -> list[tuple[str, str]]:
    """The tracked files ENTRIES relative-import, one level down, each with the
    runner of the file that imports it. The runtime loads a sibling while it
    loads the entrypoint, so a bare import there fails the same way."""
    found: list[tuple[str, str]] = []
    seen = {path for path, _runner in entries}
    for path, runner in entries:
        source = (root / path).read_text(encoding="utf-8", errors="replace")
        imported = runtime_specifiers(source, path) + require_specifiers(source, path)
        for _line, specifier in imported:
            target = _relative_target(specifier, path)
            if target is None or target in seen or target not in files:
                continue
            if not is_js_source(target):
                continue
            seen.add(target)
            found.append((target, runner))
    return found


def javascript_files(
    execution: Execution, root: Path, files: frozenset[str]
) -> list[tuple[str, str]]:
    """Each (tracked JavaScript file, runner) EXECUTION reaches: the file itself,
    or the ones a shell script hands to a runner one hop down, plus the tracked
    siblings each of those relative-imports."""
    path = execution.path
    if path not in files:
        return []
    if is_js_source(path):
        entries = [(path, execution.runner)]
    else:
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        entries = scripts_run_by_shell(text, path, files)
    return entries + _relative_closure(entries, root, files)


def bare_imports(
    source: str, path: str, runner: str = _DEFAULT_JS_RUNNER
) -> list[tuple[int, str]]:
    """Every (1-based line, specifier) in SOURCE that needs a package RUNNER
    cannot resolve by itself.

    A TypeScript type-only import is erased before the runtime resolves
    anything, so it is out. A `require()` argument is in: CommonJS resolves it
    through `node_modules` the same way an `import` does.
    """
    imported = runtime_specifiers(source, path) + require_specifiers(source, path)
    return sorted((line, spec) for line, spec in imported if is_bare(spec, runner))


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
            # The shell hop parses a tracked script with the same grammar. One
            # past its size bound, and one the grammar cannot read, each fail
            # loudly rather than reading as clean.
            try:
                scripts = javascript_files(execution, root, files)
            except (PathologicalInputError, UnparseableShellError) as err:
                print(
                    f"::error file={rel},line={execution.line}::{execution.path}: {err}"
                )
                total += 1
                continue
            for script, runner in scripts:
                source = (root / script).read_text(encoding="utf-8", errors="replace")
                for line, specifier in bare_imports(source, script, runner):
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

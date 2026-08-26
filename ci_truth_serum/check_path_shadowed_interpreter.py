#!/usr/bin/env python3
"""No workflow step downstream of an agent step reaches Python by bare name.

`--agent-action`'s action writes `/usr/bin` (or another system directory) onto
`$GITHUB_PATH` while it runs. A runner does not APPEND to `$PATH` between
steps — each step's job PREPENDS every `$GITHUB_PATH` line written so far
onto the base `$PATH`, in write order, so the LAST writer wins the front of
the line. Every step after the agent step therefore resolves a bare
`python3` to the system interpreter instead of the repository's own venv. A
step that ran fine before the agent step then dies on `ModuleNotFoundError`
for a package `uv sync` demonstrably installed, and nothing in the failure
names the action that moved the ground under it.

Naming the interpreter by path (`"$REPO_ROOT/.venv/bin/python3"`, or an
explicit `PYTHON` environment variable) is immune: a virtualenv binds its
packages to that one interpreter binary, wherever `$PATH` points. Scoped to
Python because the failure needs the shadowing binary to actually EXIST at
the prepended path, which `/usr/bin/python3` does on every runner image this
pack targets.

Read off the bash grammar's command words, never off line text: a word is a
bare interpreter only when the shell would treat it as a whole command word,
so `gb_warn "run python3 now"` is one `gb_warn` command whose message merely
spells the name, and `.venv/bin/python3` is a different, longer word.

Known blind spots, both false-negative directions — a shadowed interpreter
reached only through one of these is not seen: this does not expand a LOCAL
composite action's own steps into the scan, and it does not follow a `run:`
step's own reference to a `.github/scripts/*.sh` file into that script's
body.

Opt out on the offending line, or in the comment block above it, with
`# allow-path-shadowed-interpreter: <reason>`.

Globs every workflow; the passed file list is ignored.
"""

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    command_words,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    annotated_near,
    workflow_files,
)

# One decoded JSON/YAML object whose keys this module does not model.
JsonObject = dict[str, Any]

OPT_OUT = "allow-path-shadowed-interpreter"
DEFAULT_AGENT_ACTIONS = ("anthropics/claude-code-action",)
_BARE_NAMES = frozenset({"python", "python3"})
# A `${VAR:-python3}` parameter-expansion default naming a bare interpreter —
# the shape the shadowing defect first shipped in. Applied to one already-
# isolated command-word's text (never a whole line), so a neighbouring word's
# own default can never be crossed into.
_BARE_DEFAULT = re.compile(r":-\s*python3?(?![\w.-])")


def _is_agent_step(step: JsonObject, agent_actions: tuple[str, ...]) -> bool:
    uses = str(step.get("uses") or "")
    return any(action in uses for action in agent_actions)


def _bare_default(word: str) -> bool:
    return bool(_BARE_DEFAULT.search(word))


def bare_python_words(run: str) -> list[tuple[int, str]]:
    """(1-based line, its source text) for every command word in RUN that is
    exactly `python`/`python3`, or a `${VAR:-python3}` default naming one.

    Read per `command` node's own word list, which is bash's real word
    boundary: `.venv/bin/python3`, `python3.11`, and `--python 3.12` are each
    one or two DIFFERENT words, so no lookaround is needed to spare them —
    the grammar already segmented them away from the bare spelling.
    """
    hits: list[tuple[int, str]] = []
    lines = run.split("\n")
    for node in iter_nodes(parse(run), "command"):
        words = command_words(node)
        if not any(word in _BARE_NAMES or _bare_default(word) for word in words):
            continue
        lineno = node.start_point[0] + 1
        hits.append((lineno, lines[lineno - 1].strip()))
    return hits


def violations(text: str, agent_actions: tuple[str, ...]) -> list[tuple[int, str]]:
    """(step's 1-based line, message) for every bare-interpreter site
    downstream of an agent step in one workflow's TEXT, in each job's own
    step order. A file this cannot parse as YAML is itself reported (line 1)
    rather than silently passed as clean.

    Anchored at the STEP's own line (its first key, from `LineLoader`), like
    every sibling workflow lint's `::error file=,line=` — a bare word's line
    WITHIN a multi-line `run:` block is local to that block's own text, not
    to the file, so it is used only to place the opt-out, never reported.
    """
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                1,
                f"could not parse as YAML ({first_line}); cannot verify no step "
                "downstream of an agent step reaches Python by bare name — fix "
                "the syntax (or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return []

    found: list[tuple[int, str]] = []
    for job in doc["jobs"].values():
        if not isinstance(job, dict):
            continue
        downstream = False
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if downstream and isinstance(run, str):
                run_lines = run.split("\n")
                step_line = step.get("__line__", 1)
                for lineno, code in bare_python_words(run):
                    if annotated_near(run_lines, lineno, OPT_OUT):
                        continue
                    found.append(
                        (
                            step_line,
                            f"`{code}` reaches Python by bare name after an "
                            "agent step already rewrote $GITHUB_PATH — it "
                            "resolves to the system interpreter, not this "
                            "repository's venv. Name the interpreter by path, "
                            f"or annotate `# {OPT_OUT}: <reason>`.",
                        )
                    )
            downstream = downstream or _is_agent_step(step, agent_actions)
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-action",
        action="append",
        default=list(DEFAULT_AGENT_ACTIONS),
        metavar="NAME",
        help="a `uses:` substring naming an action whose $GITHUB_PATH write "
        "moves the interpreter (repeatable); extends the default "
        f"({DEFAULT_AGENT_ACTIONS[0]}) rather than replacing it",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="the repository root to scan (default: the current directory)",
    )
    args = parser.parse_args(argv)
    agent_actions = tuple(args.agent_action)

    total = 0
    for workflow in workflow_files(args.repo_root / ".github" / "workflows"):
        rel = workflow.relative_to(args.repo_root)
        text = workflow.read_text(encoding="utf-8")
        for line, message in violations(text, agent_actions):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} path-shadowed-interpreter violation(s) found.")
        print(
            "A runner rebuilds $PATH each step by prepending every "
            "$GITHUB_PATH line written so far, so the agent action's write "
            "shadows every later step's bare `python`/`python3`."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

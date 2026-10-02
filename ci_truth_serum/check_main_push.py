#!/usr/bin/env python3
"""Ban a shell ``git push`` whose destination is a protected branch.

A repository that lands work through pull requests or a merge queue assumes every
commit on the default branch passed review and the required checks. A script that
pushes there directly skips both. With a merge queue it also invalidates every
queued group, so the checks in flight start again.

The check reads the refspec DESTINATION: ``main``, ``+HEAD:main`` and
``HEAD:refs/heads/main`` land on ``main``, and ``main:topic`` does not. The bash
grammar supplies the words, so a push in a comment, a message or a heredoc body
is text. A destination that holds an expansion is unknown and is not reported.

``--branch NAME`` names a protected branch (repeatable; default ``main`` and
``master``). ``--git-command NAME`` names a function that takes git's own
arguments (repeatable). A shell file is read whole; a workflow or action file has
each ``run:`` value read. A push that must stay opts out with
``# main-push-ok: <reason>`` on the command or in the comment block above it.
"""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    is_lookup,
    iter_nodes,
    parse,
    program_name,
)
from _cts_comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    shell_comments,
    yaml_comments,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    MESSAGE_PREFIX,
    annotated_near,
    run_source_checks,
    yaml_run_scalars,
    yaml_run_script,
)

OPT_OUT = "main-push-ok"
DEFAULT_BRANCHES = ("main", "master")

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

# git's global options that take their value as the NEXT word (`git -C dir push`).
_GIT_VALUE_OPTIONS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
)
# `git push` options that take their value as the next word. Every other push
# option is a bare flag or takes its value after `=`.
_PUSH_VALUE_OPTIONS = frozenset(
    {
        "-o",
        "--push-option",
        "--repo",
        "--receive-pack",
        "--exec",
        "--recurse-submodules",
    }
)
# Nodes whose value the shell computes at run time. Each one becomes HOLE in a
# word, so a refspec half that holds one is unknown and a literal half still reads.
_COMPUTED = frozenset(
    {
        "simple_expansion",
        "expansion",
        "command_substitution",
        "process_substitution",
        "arithmetic_expansion",
    }
)
_ARGUMENTS = ARGUMENT_TYPES | _COMPUTED
HOLE = "\0"


def _message(branches: frozenset[str]) -> str:
    """The finding text, naming the protected branches and the opt-out."""
    names = ", ".join(f"`{name}`" for name in sorted(branches))
    return (
        f"`git push` lands on a protected branch ({names}). The commit skips "
        "review and the required checks, and a merge queue restarts every queued "
        "group. Push a topic branch and open a pull request, or annotate "
        f"`# {OPT_OUT}: <reason>` on the command or in the comment block above it."
    )


def _shape(node) -> str:
    """NODE's word as the shell sees it: quotes gone, each computed part a HOLE."""
    if node.type in _COMPUTED:
        return HOLE
    text = node.text.decode("utf-8", "replace")
    if node.type == "raw_string":
        return text[1:-1]
    if not node.children:
        return "" if node.type == '"' else text
    return "".join(_shape(child) for child in node.children)


def words_of(command) -> list[str]:
    """COMMAND's name and arguments, each shaped by ``_shape``.

    A ``$(…)`` argument counts as a word here, so the words after it keep their
    place. ``command_arguments`` drops it, which would shift the remote.
    """
    words: list[str] = []
    for child in command.children:
        if child.type == "command_name":
            words.extend(_shape(part) for part in child.children)
        elif child.type in _ARGUMENTS:
            words.append(_shape(child))
    return words


def _destination(refspec: str) -> str | None:
    """The branch a REFSPEC writes, or None when it is computed or negative.

    A refspec is ``[+]<src>[:<dst>]``. One with no ``:`` writes the ref of the same
    name, and ``:dst`` deletes ``dst``, which also writes it.
    """
    spec = refspec.removeprefix("+")
    destination = spec.rpartition(":")[2].removeprefix("refs/heads/")
    if spec.startswith("^") or HOLE in destination:
        return None
    return destination


def _git_subcommand_index(words: list[str], start: int) -> int | None:
    """The index of the git subcommand in WORDS, read from START past git's options."""
    index = start
    while index < len(words) and words[index].startswith("-"):
        index += 2 if words[index] in _GIT_VALUE_OPTIONS else 1
    return index if index < len(words) else None


def _refspecs(args: list[str]) -> list[str]:
    """The refspec words of a ``git push`` whose arguments are ARGS.

    Options and their values drop out. The first positional word is the remote.
    ``tag <name>`` names a tag, so the pair is no branch refspec.
    """
    positional: list[str] = []
    index = 0
    while index < len(args):
        word = args[index]
        index += 1
        if word == "--":
            positional += args[index:]
            break
        if word.startswith("-") and word != "-":
            if word in _PUSH_VALUE_OPTIONS:
                index += 1
            continue
        positional.append(word)
    refspecs, rest = [], positional[1:]
    while rest:
        word = rest.pop(0)
        if word == "tag" and rest:
            rest.pop(0)
            continue
        refspecs.append(word)
    return refspecs


def pushes_to(words: list[str], branches: frozenset[str], gits: frozenset[str]) -> bool:
    """True when WORDS run a ``git push`` whose destination is in BRANCHES.

    git may stand behind a wrapper (``sudo git push``), so it is found at any
    position. GITS names extra commands that take git's own arguments.
    """
    if not words or is_lookup(words) or MESSAGE_PREFIX.match(words[0]):
        return False
    for index, word in enumerate(words):
        if program_name(word) != "git" and word not in gits:
            continue
        sub = _git_subcommand_index(words, index + 1)
        if sub is None or words[sub] != "push":
            continue
        if any(_destination(spec) in branches for spec in _refspecs(words[sub + 1 :])):
            return True
    return False


def _pushes(
    script: str, branches: frozenset[str], gits: frozenset[str]
) -> dict[int, int]:
    """{start line: end line} for each push to BRANCHES in SCRIPT, 1-based.

    Two pushes that start on one line keep the wider span, so the line reports once.
    """
    widest: dict[int, int] = {}
    for command in iter_nodes(parse(script), "command"):
        if pushes_to(words_of(command), branches, gits):
            start, end = command.start_point[0] + 1, command.end_point[0] + 1
            widest[start] = max(widest.get(start, end), end)
    return widest


def _unexcused(
    spans: dict[int, int], lines: list[str], comments: dict[int, str]
) -> list[int]:
    """The start lines in SPANS that no reason-bearing opt-out covers.

    COMMENTS is what each line says as a real comment (1-based line to text), so
    a marker quoted inside a string or a heredoc excuses nothing.
    """
    said = [comments.get(line, "") for line in range(1, len(lines) + 1)]
    return sorted(
        start
        for start, end in spans.items()
        if not annotated_near(lines, start, OPT_OUT, span_end=end, comments=said)
    )


def violations(
    text: str,
    branches: frozenset[str] = frozenset(DEFAULT_BRANCHES),
    gits: frozenset[str] = frozenset(),
) -> list[int]:
    """1-based start lines of the pushes in shell TEXT that land on BRANCHES."""
    return _unexcused(
        _pushes(text, branches, gits), text.splitlines(), shell_comments(text)
    )


def workflow_violations(
    text: str,
    branches: frozenset[str] = frozenset(DEFAULT_BRANCHES),
    gits: frozenset[str] = frozenset(),
) -> list[int]:
    """1-based lines of the pushes to BRANCHES inside the ``run:`` values of TEXT.

    Each value is read in place, so a line number is the workflow file's own. An
    opt-out is read from a real YAML comment or a real script comment.
    """
    spans: dict[int, int] = {}
    for scalar in yaml_run_scalars(text):
        script = "\n" * text.count("\n", 0, scalar.start) + yaml_run_script(
            text, scalar
        )
        for start, end in _pushes(script, branches, gits).items():
            spans[start] = max(spans.get(start, end), end)
    return _unexcused(spans, text.splitlines(), yaml_comments(text))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--branch", action="append", metavar="NAME")
    parser.add_argument("--git-command", action="append", default=[], metavar="NAME")
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    if not args.files:
        print(
            "check_main_push: no files to scan. This check reads only the paths "
            "you give it, so an empty run would report a clean pass over nothing.",
            file=sys.stderr,
        )
        return 2
    branches = frozenset(args.branch or DEFAULT_BRANCHES)
    gits = frozenset(args.git_command)

    def find(text: str, path: str) -> list[int]:
        if _WORKFLOW_PATH.search(path.replace("\\", "/")):
            return workflow_violations(text, branches, gits)
        if path.endswith((".yaml", ".yml")):
            return []
        return violations(text, branches, gits)

    # One path at a time, so a file the grammar refuses fails LOUDLY and names
    # its path instead of ending the whole run with a traceback.
    status = 0
    for path in args.files:
        try:
            status = max(status, run_source_checks([path], find, _message(branches)))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

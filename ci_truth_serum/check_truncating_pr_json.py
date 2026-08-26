#!/usr/bin/env python3
"""Refuse a `gh pr view`/`gh pr list` call that reads a TRUNCATING `--json` field.

`gh`'s `--json` flag builds a GraphQL query whose connection fields are asked
for as `<field>(first: 100)` — no cursor, no `pageInfo`. Past 100 entries GitHub
returns 100 and `gh` exits 0 with a well-formed, silently short list.

SECURITY/CORRECTNESS INVARIANT: this refusal is what stops a consumer
re-introducing a read that is silently short.

RULE: fires on a `gh pr view`/`gh pr list` command whose `--json` value names a
field in the truncating set — built in: `files`, `commits`, `comments`,
`reviews`; extend it with `--field NAME`, repeatable, for a connection this
pack does not know about. A `--json` whose value is an expansion (`--json
"$fields"`) is NOT flagged — the field set is unknowable here. A wrapper in
front (`retry_stdout gh pr view …`) does not change what is read.

THREE SURFACES: shell text; a workflow's inline `run:` blocks (scanned as
shell, reported at the step's line); and Python — a literal
`subprocess.run(["gh", "pr", "view", …])` argv, or the same call written as
ONE `shell=True` command-line string, which the shell grammar reads the same
way it reads a `.sh` file. A non-literal `--json` value (a variable, an
f-string) is unknown in Python too, never guessed at.

A site that must keep a truncating read opts out with `# truncating-pr-json-ok:
<reason>` on the command's own line span (or call's own line, in Python), or
the line above it.

This check reads whatever shell and Python files its caller passes on argv —
scope it with a `files:` regex in the consumer's `.pre-commit-config.yaml`. A
`.github/{workflows,actions}` YAML path among them has each inline `run:`
block scanned instead, reported at the step's line.
"""

import argparse
import ast
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    MESSAGE_PREFIX,
    annotation_re,
    is_python_source,
    is_shell_source,
    run_file_cli,
)
from _py_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    lines as py_lines,
    name_of,
    trees,
)

OPT_OUT = "truncating-pr-json-ok"
_ALLOW = annotation_re(OPT_OUT, require_reason=True)

# Connection fields `gh` asks for as `<field>(first: 100)` with no natural
# bound. `labels`/`assignees` are absent — a repository defines ~40 labels, so
# neither can reach 100.
_TRUNCATING = frozenset({"files", "commits", "comments", "reviews"})

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")


def _requested_fields(words: list[str]) -> set[str]:
    """The field names a literal `--json` asks for, across both spellings. An
    expansion (`--json "$fields"`) contributes nothing — its value is decided
    at run time, so the honest answer is that this check does not know."""
    fields: set[str] = set()
    for i, word in enumerate(words):
        if word.startswith("--json="):
            raw = word.removeprefix("--json=")
        elif word == "--json":
            raw = words[i + 1] if i + 1 < len(words) else ""
        else:
            continue
        raw = unquote(raw)
        if "$" in raw or "`" in raw:
            continue
        fields |= {name.strip() for name in raw.split(",")}
    return fields


def _truncating_read(words: list[str], truncating: frozenset[str]) -> bool:
    """True when WORDS spell a `gh pr view`/`gh pr list` call asking for a
    field TRUNCATING names. Presence-based, not adjacency-based: a wrapper
    (`retry_stdout gh pr view …`) does not change what is read."""
    if "gh" not in words or "pr" not in words or not ({"view", "list"} & set(words)):
        return False
    return bool(_requested_fields(words) & truncating)


def _shell_hits(text: str, truncating: frozenset[str]) -> dict[int, int]:
    """{command start line: end line} for every `gh pr view`/`gh pr list`
    truncating read the bash grammar finds in TEXT, before the opt-out is
    applied. One entry per START, carrying the WIDEST span that starts there —
    two calls can begin on the same row, and the wider span gives the
    annotation lookup every physical line the reader would put it on."""
    widest: dict[int, int] = {}
    root = parse(text)
    for command in iter_nodes(root, "command"):
        tokens = [unquote(node_text(node)) for node in command_arguments(command)]
        if tokens and MESSAGE_PREFIX.match(tokens[0]):
            continue  # a command that only prints; its arguments are text
        if not _truncating_read(tokens, truncating):
            continue
        start, end = command.start_point[0] + 1, command.end_point[0] + 1
        widest[start] = max(widest.get(start, end), end)
    return widest


def violations(text: str, *, truncating: frozenset[str] = _TRUNCATING) -> list[int]:
    """1-based line numbers of `gh pr view`/`gh pr list` calls reading a
    connection field TRUNCATING names, absent a `# truncating-pr-json-ok:`
    annotation.

    Detection walks the bash grammar, so a `\\`-continued call is ONE node
    (reported at its first line), a call written in a comment is a `comment`
    node, and one written inside a message string or a heredoc body is that
    string or body rather than a command."""
    raw = text.splitlines()
    hits = []
    for start, end in sorted(_shell_hits(text, truncating).items()):
        if any(_ALLOW.search(line) for line in raw[start - 1 : end]) or (
            start >= 2 and _ALLOW.search(raw[start - 2])
        ):
            continue
        hits.append(start)
    return hits


# ── Python: subprocess.run(["gh", "pr", …]) and its shell=True string form ──


def _is_subprocess_run(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "run"
        and (name_of(func.value) == "subprocess")
    )


def _argv_arg(call: ast.Call) -> "ast.expr | None":
    """The argv a `subprocess.run` call passes, positionally or as `args=`."""
    return (
        call.args[0]
        if call.args
        else next((kw.value for kw in call.keywords if kw.arg == "args"), None)
    )


def _argv_words(call: ast.Call) -> "list[str | None] | None":
    """One entry per argv element a `subprocess.run` call passes — the string a
    literal wrote, `None` where the source wrote something else (a variable, an
    f-string, a splat). `None` overall when the call passes no positional or
    `args=` list/tuple literal at all."""
    argv = _argv_arg(call)
    if not isinstance(argv, (ast.List, ast.Tuple)):
        return None
    return [
        elt.value
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        else None
        for elt in argv.elts
    ]


def _shell_string_truncating_read(call: ast.Call, truncating: frozenset[str]) -> bool:
    """True when CALL is a `subprocess.run("gh pr view --json files",
    shell=True)` — a whole command line written as ONE string literal. The
    shell scanner reads that literal, so the same grammar judges it here as
    judges a `.sh` file."""
    argv = _argv_arg(call)
    if not isinstance(argv, ast.Constant) or not isinstance(argv.value, str):
        return False
    return bool(_shell_hits(argv.value, truncating))


def _python_truncating_read(
    words: "list[str | None]", truncating: frozenset[str]
) -> bool:
    """True when WORDS is a `gh pr view`/`gh pr list` argv asking for a literal
    `--json` field TRUNCATING names. Mirrors `_truncating_read`: a non-literal
    field value (`None`) is read as unknown, never as clean."""
    if words[:1] != ["gh"] or "pr" not in words or not ({"view", "list"} & set(words)):
        return False
    for i, word in enumerate(words):
        if word == "--json" and i + 1 < len(words):
            value = words[i + 1]
        elif isinstance(word, str) and word.startswith("--json="):
            value = word.removeprefix("--json=")
        else:
            continue
        if value is not None and set(value.split(",")) & truncating:
            return True
    return False


def _python_hits(
    tree: ast.AST, lines_: list[str], truncating: frozenset[str]
) -> list[int]:
    """1-based line numbers of a `subprocess.run(["gh", "pr", …])` call in TREE
    reading a truncating field, absent a `# truncating-pr-json-ok:` annotation."""
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_subprocess_run(node.func)):
            continue
        words = _argv_words(node)
        listed = words is not None and _python_truncating_read(words, truncating)
        if not listed and not _shell_string_truncating_read(node, truncating):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        if any(_ALLOW.search(ln) for ln in lines_[max(node.lineno - 2, 0) : end]):
            continue
        hits.append(node.lineno)
    return sorted(set(hits))


def python_violations(
    source: str, *, truncating: frozenset[str] = _TRUNCATING
) -> list[int]:
    """1-based line numbers of a `subprocess.run(["gh", "pr", …])` call (or the
    same call written as a `shell=True` command-line string) reading a
    truncating `--json` field, absent a `# truncating-pr-json-ok:` annotation."""
    physical = py_lines(source)
    hits: list[int] = []
    for tree in trees(source):
        hits += _python_hits(tree, physical, truncating)
    return sorted(set(hits))


def _run_scripts(path: Path) -> list[tuple[int, str]]:
    """(step line, script) for every inline `run:` block in a workflow or
    composite-action file. An unparseable file yields no scripts — YAML syntax
    is another tool's job, and the shell files this check owns are its argv."""
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    containers = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        containers += [job for job in jobs.values() if isinstance(job, dict)]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        containers.append(runs)
    scripts: list[tuple[int, str]] = []
    for container in containers:
        steps = container.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                scripts.append((step.get("__line__", 1), step["run"]))
    return scripts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--field",
        action="append",
        default=[],
        dest="fields",
        help="an extra `--json` connection field this repo's PRs can exceed "
        "100 of (repeatable)",
    )
    parser.add_argument("files", nargs="*")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    truncating = _TRUNCATING | set(args.fields)
    message = (
        "`gh pr view --json` reads a connection gh caps at 100 with no cursor "
        "— the short list arrives with exit 0 and nothing says it was cut. "
        "Read the paging REST endpoint (`gh api --paginate "
        "repos/{owner}/{repo}/pulls/N/files`), or a single object that needs "
        f"no list at all, or annotate `# {OPT_OUT}: <reason>`. The marker "
        "must sit on a line the command spans, or the line directly above it."
    )
    status = 0
    for arg in args.files:
        path = Path(arg)
        try:
            if _WORKFLOW_PATH.search(arg.replace("\\", "/")):
                hits = [
                    step_line
                    for step_line, script in _run_scripts(path)
                    if violations(script, truncating=truncating)
                ]
            else:
                text = path.read_text(encoding="utf-8")
                if is_python_source(arg):
                    hits = python_violations(text, truncating=truncating)
                elif is_shell_source(arg, text.split("\n", 1)[0]):
                    hits = violations(text, truncating=truncating)
                else:
                    continue  # neither shell nor Python — e.g. a non-workflow YAML
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails the check LOUDLY:
            # skipping it would false-green exactly the input an adversary
            # controls.
            print(f"{arg}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno in hits:
            print(f"{arg}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

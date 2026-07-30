#!/usr/bin/env python3
"""Flag `2>&1`-merged output that is then PARSED — stderr noise becomes data.

Merging stderr into a captured stream is fine for diagnostics ("show me
everything the command said"). It is a bug the moment the merged stream is fed
to a parser: any warning the tool prints becomes part of "the value". Real
incident: an npm stderr warning merged via `2>&1` became "the version", and
every release aborted on the nonsense comparison that followed.

Flagged (precision over recall — plain diagnostic captures must never fire):

  (a) a command substitution whose value is produced by merging (`2>&1`, or the
      `|&` pipe bash defines as `2>&1 |`) and then piping the merged stream into
      a parsing command (`v=$(cmd 2>&1 | tail -1)`, `v=$(cmd |& tail -1)`);
  (b) `var=$(cmd 2>&1)` where, within the next 10 lines, `$var` is piped into
      a parsing command or read in a `[[ … ]]` comparison / `(( … ))`
      arithmetic.

Parsing commands: head, tail, grep, awk, cut, sed, jq, sort, wc. NOT flagged:
`var=$(cmd 2>&1)` followed only by echo/printf/logging — capture-for-
diagnostics is the dominant legitimate use — and a capture the script branches
on (`if ! var=$(cmd 2>&1); then …`), where the merge feeds the failure path and
the stream the later read sees is the success path's. Opt out with a
`# stderr-merge-ok: <reason>` comment on any physical line of the flagged
command, the line above it, or (for rule b) the capturing assignment.

Every question this lint asks is about shell STRUCTURE, so it asks the real bash
grammar (``_bash_ast``) rather than the text: `2>&1` is a ``file_redirect``
(never a `2>&1` inside an argument's text), "piped into a parser" is a later
``pipeline`` stage whose ``command_name`` is one of the parsers, "captured" is a
``variable_assignment`` whose value is a ``command_substitution``, and
"compared" is a ``binary_expression`` under a ``test_command`` or an
``arithmetic_expansion``. That is also what keeps the lint off text that merely
DEPICTS the idiom: a single-quoted string and a quoted-delimiter heredoc body
hold no substitution nodes at all, and a `$(…)` spliced between literal
``string_content`` runs — `gb_warn "v=$(cmd 2>&1 | tail -1) parses noise"` — is
being interpolated into a message rather than becoming a value, so only a
substitution that IS the whole value at its position (an assignment's
right-hand side, or the entire string/argument) is judged.

Invoked by pre-commit with the staged shell files as arguments; a
`.github/{workflows,actions}` YAML path among them has each inline `run:`
block scanned instead (reported at the step's line).
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    iter_nodes,
    node_text,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    annotated,
)

OPT_OUT = "stderr-merge-ok"

# Commands that read a stream as structured data. A merged stream reaching one of
# these is the defect; `tee`, `cat`, a logger or a shell function are not.
_PARSERS = frozenset({"head", "tail", "grep", "awk", "cut", "sed", "jq", "sort", "wc"})
_MERGE = "2>&1"
# `cmd |& parser` is defined by bash as `cmd 2>&1 | parser`, so it merges the same
# two streams into the same parser. It is a pipe OPERATOR rather than a redirect,
# which is why it needs naming separately from `_MERGE`.
_MERGE_PIPE = "|&"
# Operators inside `[[ … ]]` that rank/match the value as data. `-z`/`-n`
# (emptiness) and `-f`-style file tests are diagnostics, so they are unary
# expressions here and never reach this set.
_COMPARE_OPS = frozenset(
    {"-eq", "-ne", "-lt", "-le", "-gt", "-ge", "=~", "==", "!=", "<", ">"}
)
# How far after the capture a use still refers to the merged value in practice.
_WINDOW = 10

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

MESSAGE_INLINE = (
    "`2>&1` merges stderr into a stream that is then piped into a parser — any "
    'warning on stderr becomes part of "the value" (an npm warning merged this '
    'way became "the version" and aborted every release). Parse stdout only, '
    f"or annotate `# {OPT_OUT}: <reason>`."
)
MESSAGE_LATER = (
    "this line parses/compares a variable captured with `2>&1` — any warning on "
    "stderr became part of the value. Capture stdout only (keep stderr for "
    f"diagnostics), or annotate `# {OPT_OUT}: <reason>`."
)

# Node types that OPEN a new block of statements. Climbing out of a node stops
# below one of these, which is what turns a flagged node into the physical line
# range of the single command carrying it — the lines a reader would put the
# `# stderr-merge-ok:` annotation on. Without the stop, a substitution inside an
# `if` would claim the whole `if … fi` block's lines and an annotation anywhere
# in the body would suppress the finding.
_BLOCKS = frozenset(
    {
        "program",
        "compound_statement",
        "subshell",
        "if_statement",
        "elif_clause",
        "else_clause",
        "while_statement",
        "until_statement",
        "for_statement",
        "c_style_for_statement",
        "case_statement",
        "case_item",
        "do_group",
        "function_definition",
        "heredoc_body",
    }
)
# The pipe operators between a `pipeline`'s stages, so stage enumeration can skip
# them without positional indexing.
_PIPE_TOKENS = frozenset({"|", "|&"})


def _statement_rows(node) -> tuple[int, int]:
    """(first row, last row) of the smallest complete statement containing NODE,
    both 0-based — a `\\`-continued or `|`-wrapped command included whole."""
    while node.parent is not None and node.parent.type not in _BLOCKS:
        node = node.parent
    return node.start_point[0], node.end_point[0]


def _is_whole_value(node) -> bool:
    """True when NODE's output IS the value at its position, rather than being
    spliced into surrounding literal text.

    `v=$(…)` and `echo "$(…)"` pass; `gb_warn "v=$(…) parses noise"` does not —
    there the substitution has ``string_content`` siblings, so its output is
    interpolated into a message the command prints, not used as a value."""
    parent = node.parent
    if parent is None:
        return True
    if parent.type == "concatenation":
        return False
    if parent.type == "string":
        content = [child for child in parent.children if child.type != '"']
        return len(content) == 1 and _is_whole_value(parent)
    return True


def _owns(container, node) -> bool:
    """True when NODE sits inside CONTAINER with no nested command substitution in
    between — so an inner `$(…)`'s pipeline is judged as its own value, not as
    part of the enclosing substitution's."""
    parent = node.parent
    while parent is not None and parent.id != container.id:
        if parent.type == "command_substitution":
            return False
        parent = parent.parent
    return parent is not None


def _stage_pairs(pipeline) -> list:
    """(stage, merges) for a `pipeline`'s stages in order, nested pipelines
    flattened. MERGES is true when the stage's stderr joins the stream the NEXT
    stage reads — either the stage carries its own `2>&1`, or the pipe after it is
    `|&`, which is bash's exact shorthand for `2>&1 |` and merges the same two
    streams by the same rule."""
    pairs: list = []
    for child in pipeline.children:
        if child.type == "comment":
            continue
        if child.type in _PIPE_TOKENS:
            # `|&` merges the stage to its LEFT, which after flattening a nested
            # pipeline is the last stage recorded so far.
            if child.type == _MERGE_PIPE and pairs:
                pairs[-1] = (pairs[-1][0], True)
            continue
        if child.type == "pipeline":
            pairs.extend(_stage_pairs(child))
        else:
            pairs.append((child, _stage_merges(child)))
    return pairs


def _stages(pipeline):
    """A `pipeline`'s stages in order, nested pipelines flattened."""
    return [stage for stage, _merges in _stage_pairs(pipeline)]


def _stage_merges(stage) -> bool:
    """True when STAGE redirects its own stderr onto stdout — a `file_redirect`
    child of the stage itself, so a `2>&1` deeper inside (a nested substitution,
    or the literal text of an argument) does not count."""
    return any(
        child.type == "file_redirect" and "".join(node_text(child).split()) == _MERGE
        for child in stage.children
    )


def _stage_command(stage):
    """The `command` a pipeline STAGE runs, or None when the stage is a compound."""
    if stage.type == "command":
        return stage
    if stage.type in ("redirected_statement", "negated_command"):
        return next((c for c in stage.children if c.type == "command"), None)
    return None


def _parses(stage) -> bool:
    """True when pipeline STAGE runs one of the parsing commands."""
    command = _stage_command(stage)
    name = None if command is None else command.child_by_field_name("name")
    return name is not None and node_text(name).rsplit("/", 1)[-1] in _PARSERS


def _merged_then_parsed(subst) -> bool:
    """True when SUBST's own value is produced by merging stderr and then piping
    the merged stream into a parsing command."""
    for pipeline in iter_nodes(subst, "pipeline"):
        if not _owns(subst, pipeline):
            continue
        merged = False
        for stage, stage_merges in _stage_pairs(pipeline):
            if merged and _parses(stage):
                return True
            merged = merged or stage_merges
    return False


def _merges(subst) -> bool:
    """True when SUBST merges stderr into the stream it captures — by a `2>&1`
    redirect, or by a `|&` pipe, which merges the identical pair of streams."""
    if any(
        "".join(node_text(node).split()) == _MERGE
        for node in iter_nodes(subst, "file_redirect")
    ):
        return True
    return any(True for _ in iter_nodes(subst, _MERGE_PIPE))


def _captured_substitution(assign):
    """The command substitution an assignment's value IS, or None when the value
    is something else (a literal, a concatenation, a message with a `$(…)` in
    it — none of which is a captured stream)."""
    value = assign.child_by_field_name("value")
    if value is None:
        return None
    if value.type == "command_substitution":
        return value
    if value.type == "string":
        content = [child for child in value.children if child.type != '"']
        if len(content) == 1 and content[0].type == "command_substitution":
            return content[0]
    return None


def _exit_status_tested(assign) -> bool:
    """True when the script branches on the CAPTURE's exit status — the
    assignment is an `if`/`while` condition, is negated with `!`, or is an
    `&&`/`||` operand.

    There the merge exists to keep the command's error text for the failure path
    (`if ! out=$(cmd 2>&1); then echo "$out" >&2; fi`), which is diagnostics: the
    stream is empty on the success path the later read runs on, so the read is
    not the defect this lint names."""
    node = assign
    while node.parent is not None:
        parent = node.parent
        index = next(i for i, c in enumerate(parent.children) if c.id == node.id)
        if parent.type in ("negated_command", "list"):
            return True
        if parent.field_name_for_child(index) == "condition":
            return True
        if parent.type in _BLOCKS:
            return False
        node = parent
    return False


def _arithmetic_nodes(root):
    """Every arithmetic context under ROOT: a `$(( … ))` expansion and a bare
    `(( … ))` command (which the grammar spells as a `compound_statement`)."""
    yield from iter_nodes(root, "arithmetic_expansion")
    for node in iter_nodes(root, "compound_statement"):
        if node.children and node.children[0].type == "((":
            yield node


def _reads(node):
    """Every variable READ inside NODE, as (node, name). An assignment's target
    is a write, so `FOO=1 cmd | grep x` reads nothing."""
    for name in iter_nodes(node, "variable_name"):
        if name.parent is not None and name.parent.type == "variable_assignment":
            continue
        yield name, node_text(name)


def _data_reads(root):
    """Every place a variable's value is treated as DATA rather than printed, as
    (node, name): piped into a parser, compared inside `[[ … ]]`, or read in
    arithmetic."""
    for pipeline in iter_nodes(root, "pipeline"):
        stages = list(_stages(pipeline))
        for index, stage in enumerate(stages):
            if any(_parses(later) for later in stages[index + 1 :]):
                yield from _reads(stage)
    for test in iter_nodes(root, "test_command"):
        for expression in iter_nodes(test, "binary_expression"):
            if any(node_text(child) in _COMPARE_OPS for child in expression.children):
                yield from _reads(expression)
    for arithmetic in _arithmetic_nodes(root):
        yield from _reads(arithmetic)


def _governing_assignment(assignments: list, read):
    """The assignment whose value READ actually sees: the last one (document
    order) that begins before it and does not CONTAIN it.

    Containment is what keeps `out=$(echo "$out" | grep x)` reading the previous
    value rather than its own, and "begins before" rather than "on an earlier
    line" is what keeps `out=$(cmd 2>&1); echo "$out" | grep x` in scope."""
    governing = None
    for assign in assignments:
        if assign.start_byte >= read.start_byte:
            break  # document order: everything past here is later still
        if assign.end_byte >= read.end_byte:
            continue  # the read is inside this assignment's own value
        governing = assign
    return governing


def _opted_out(physical: list[str], rows: tuple[int, int]) -> bool:
    """Opt-out marker on any physical line of the flagged statement (ROWS,
    0-based and inclusive) or on the line directly above it."""
    first, last = rows
    window = range(max(first - 1, 0), min(last + 1, len(physical)))
    return any(annotated(physical[row], OPT_OUT) for row in window)


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every merged-then-parsed capture in TEXT."""
    physical = text.splitlines()
    root = parse(text)

    # Rule (a): the substitution's own value is merged and then parsed.
    flagged: set[int] = set()
    inline: dict[int, bool] = {}
    for subst in iter_nodes(root, "command_substitution"):
        if not (_is_whole_value(subst) and _merged_then_parsed(subst)):
            continue
        flagged.add(subst.id)
        row = subst.start_point[0]
        # One finding per line: two such substitutions can open on the same row,
        # and reporting the row twice is a duplicate, not a second defect.
        inline[row] = inline.get(row, False) or not _opted_out(
            physical, _statement_rows(subst)
        )

    # Rule (b): a merged capture whose value is later read as data. Each variable
    # keeps its assignment history in document order, so a later assignment
    # supersedes an earlier one exactly as the shell does.
    captures: dict[str, list] = {}
    merged_capture: set[int] = set()
    for assign in iter_nodes(root, "variable_assignment"):
        target = assign.child_by_field_name("name")
        if target is None:
            continue
        subst = _captured_substitution(assign)
        # A substitution rule (a) already flagged is reported at its own line;
        # re-reporting every downstream read of it would be noise.
        if (
            subst is not None
            and _merges(subst)
            and subst.id not in flagged
            and not _exit_status_tested(assign)
        ):
            merged_capture.add(assign.id)
        captures.setdefault(node_text(target), []).append(assign)

    later: dict[tuple[int, str], bool] = {}
    for node, name in _data_reads(root):
        row = node.start_point[0]
        governing = _governing_assignment(captures.get(name, []), node)
        if governing is None or governing.id not in merged_capture:
            continue
        assigned_at = governing.start_point[0]
        if row - assigned_at > _WINDOW:
            continue
        opted = _opted_out(physical, _statement_rows(node)) or annotated(
            physical[assigned_at], OPT_OUT
        )
        later[(row, name)] = later.get((row, name), False) or not opted

    found = [(row + 1, MESSAGE_INLINE) for row, hit in inline.items() if hit]
    found += [(row + 1, MESSAGE_LATER) for (row, _name), hit in later.items() if hit]
    return sorted(found, key=lambda entry: entry[0])


def _run_scripts(path: Path) -> list[tuple[int, str]]:
    """(step line, script) for every inline `run:` block in a workflow or
    composite-action file. An unparseable file yields no scripts — YAML syntax
    is actionlint's job, and the shell files this lint owns are its argv."""
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    scripts: list[tuple[int, str]] = []
    containers = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        containers += [j for j in jobs.values() if isinstance(j, dict)]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        containers.append(runs)
    for container in containers:
        steps = container.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                scripts.append((step.get("__line__", 1), step["run"]))
    return scripts


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = 0
    for arg in argv:
        path = Path(arg)
        try:
            if _WORKFLOW_PATH.search(arg.replace("\\", "/")):
                hits = [
                    (step_line, message)
                    for step_line, script in _run_scripts(path)
                    for _line, message in violations(script)
                ]
            elif arg.endswith((".yaml", ".yml")):
                continue  # a non-workflow YAML file is not shell
            else:
                hits = violations(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails the check LOUDLY (the
            # same posture as check_untrusted_exec): skipping it would false-green
            # exactly the input an adversary controls.
            print(f"{arg}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno, message in hits:
            print(f"{arg}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())

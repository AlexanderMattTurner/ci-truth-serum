#!/usr/bin/env python3
"""Flag a literal `exit N` that a failed test reaches with nothing printed first.

`[[ -f "$cfg" ]] || exit 2` stops the script with status 2 and says nothing. The
CI log then holds a red step and no cause, so the reader starts the diagnosis from
zero. The test knew the reason (the file is missing), and one `echo … >&2` before
the exit would have kept it.

The scan runs on the bash grammar (``_cts_bash_ast``). An exit is flagged when all
of these hold:

  * it is a ``command`` named ``exit`` whose argument is a literal non-zero number.
    A bare ``exit`` or ``exit "$rc"`` passes on a status that came from elsewhere;
  * it ends the script. An exit inside ``( … )``, ``$( … )`` or ``<( … )`` ends only
    a subshell, and its status reaches a caller who can print the cause;
  * a probe guards it: it is the right operand of ``||`` after a probe, or it sits in
    an ``if`` / ``elif`` / ``else`` branch that only probes lead to. A probe is a
    ``[[ … ]]``, ``[ … ]`` or ``test …`` that asks a unary question (``-f x``,
    ``-n "$v"``, ``! -L x``), alone, negated, or joined to other probes. A probe
    prints nothing and looks at the world now. A comparison such as
    ``[[ $failed -eq 0 ]]`` is not a probe: it reads a verdict the script reached
    earlier, and the code that reached it usually printed the cause;
  * every statement before the exit in its branch is an assignment or a
    declaration. Any other command may print the reason, so the lint trusts it.

A `return N` is not flagged: a caller receives the status and can print the cause.
An exit inside a message string or a heredoc body is text, and the grammar never
reads it as a command.

Opt out with ``# allow-silent-exit: <reason>`` on the exit's line, on any line of
its statement, or on the line above. The usual reason is a predicate script whose
exit status is its whole answer.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    code_children,
    command_name,
    iter_nodes,
    node_text,
    parse,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    run_file_cli,
    run_line_checks,
    source_lines,
)
from _cts_comments import shell_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "allow-silent-exit"

MESSAGE = (
    "a failed probe reaches this `exit N` with nothing printed, so the log names no "
    "cause. Print the reason to stderr before the exit, or annotate "
    f"`# {OPT_OUT}: <reason>`."
)

# Statements that set a variable and print nothing, so they keep a branch silent.
_SILENT_STATEMENTS = frozenset({"variable_assignment", "declaration_command"})

# The words that open and close a condition inside an `if` or `elif`.
_CONDITION_OPENERS = frozenset({"if", "elif"})
_BRANCH_ENDS = frozenset({"elif_clause", "else_clause", "fi"})

# The nodes whose `exit` ends only a subshell.
_SUBSHELLS = frozenset({"subshell", "command_substitution", "process_substitution"})


def _literal_failure_status(exit_command) -> bool:
    """Whether EXIT_COMMAND (an `exit` command node) names a literal non-zero status."""
    arguments = code_children(exit_command)[1:]
    if len(arguments) != 1 or arguments[0].type != "number":
        return False
    text = node_text(arguments[0])
    return text.isdigit() and int(text) != 0


def _is_probe_expression(node) -> bool:
    """Whether NODE, an expression inside `[[ … ]]` or `[ … ]`, only asks a unary
    question: `-f x`, `! -L x`, or such questions joined with `&&` / `||`."""
    children = code_children(node)
    if node.type == "unary_expression":
        inner = [c for c in children if c.type == "unary_expression"]
        return not inner or all(_is_probe_expression(c) for c in inner)
    if node.type == "binary_expression":
        operator = [c for c in children if c.type in {"&&", "||"}]
        operands = [c for c in children if c.type not in {"&&", "||"}]
        return bool(operator) and all(_is_probe_expression(c) for c in operands)
    if node.type == "parenthesized_expression":
        return all(_is_probe_expression(c) for c in children if c.is_named)
    return False


def _is_test(node) -> bool:
    """Whether NODE is a probe: a test that asks a unary question about a file or a
    string, so it prints nothing and nothing before it has decided the outcome."""
    if node.type == "test_command":
        expressions = [c for c in code_children(node) if c.is_named]
        return len(expressions) == 1 and _is_probe_expression(expressions[0])
    if node.type == "command":
        if command_name(node) not in {"test", "["}:
            return False
        words = [node_text(c) for c in code_children(node)[1:]]
        words = [w for w in words if w != "]"]
        if words and words[0] == "!":
            words = words[1:]
        return len(words) == 2 and words[0].startswith("-")
    if node.type == "negated_command":
        return all(_is_test(child) for child in code_children(node)[1:])
    if node.type == "list":
        operands = [c for c in code_children(node) if c.type not in {"&&", "||"}]
        return all(_is_test(child) for child in operands)
    return False


def _exits_the_script(exit_command) -> bool:
    """Whether EXIT_COMMAND ends the script itself. An exit inside `( … )`, `$( … )` or
    `<( … )` ends only that subshell, and the status reaches a caller who can print it."""
    node = exit_command.parent
    while node is not None and node.type not in {"function_definition", "program"}:
        if node.type in _SUBSHELLS:
            return False
        node = node.parent
    return True


def _conditions(clause) -> list:
    """The condition nodes of an `if_statement` or `elif_clause`: what sits between
    its opening word and `then`, without the `;` separators."""
    parts = []
    inside = False
    for child in code_children(clause):
        if child.type in _CONDITION_OPENERS:
            inside = True
        elif child.type == "then":
            break
        elif inside and child.type != ";":
            parts.append(child)
    return parts


def _body(clause) -> list:
    """The statements of the branch CLAUSE opens: after `then` for an `if` or
    `elif`, after `else` for an `else`, up to the next clause or `fi`."""
    opener = "else" if clause.type == "else_clause" else "then"
    statements = []
    inside = False
    for child in code_children(clause):
        if child.type == opener:
            inside = True
        elif child.type in _BRANCH_ENDS:
            break
        elif inside and child.type != ";":
            statements.append(child)
    return statements


def _silent_before(statements, exit_command) -> bool:
    """Whether every statement in STATEMENTS before EXIT_COMMAND prints nothing."""
    for statement in (s for s in statements if s.is_named):
        if statement.start_byte >= exit_command.start_byte:
            return True
        if statement.type not in _SILENT_STATEMENTS:
            return False
    return False


def _guarded_by_or(exit_command):
    """The `list` node when EXIT_COMMAND is the silent right operand of `test || …`,
    else None. The operand is the exit itself, or a `{ …; }` block that reaches the
    exit through assignments only."""
    operand = exit_command
    parent = exit_command.parent
    if parent is not None and parent.type == "compound_statement":
        if not _silent_before(code_children(parent)[1:], exit_command):
            return None
        operand = parent
        parent = parent.parent
    if parent is None or parent.type != "list":
        return None
    children = code_children(parent)
    at = children.index(operand) if operand in children else -1
    if at < 2 or children[at - 1].type != "||" or not _is_test(children[at - 2]):
        return None
    return parent


def _guarded_by_if(exit_command):
    """The `if_statement` when EXIT_COMMAND is the first non-assignment statement of a
    branch that only tests lead to, else None."""
    clause = exit_command.parent
    if clause is None or clause.type not in {
        "if_statement",
        "elif_clause",
        "else_clause",
    }:
        return None
    statement = clause if clause.type == "if_statement" else clause.parent
    if statement is None or statement.type != "if_statement":
        return None
    if exit_command not in _body(clause) or not _silent_before(
        _body(clause), exit_command
    ):
        return None
    # Every condition bash evaluates before it enters this branch must be a test.
    evaluated = list(_conditions(statement))
    for other in code_children(statement):
        if other.type != "elif_clause" or other.start_byte > clause.start_byte:
            continue
        evaluated += _conditions(other)
    if evaluated and all(_is_test(condition) for condition in evaluated):
        return statement
    return None


def violations(text: str) -> list[int]:
    """1-based line numbers of each literal `exit N` that a failed test reaches with
    nothing printed first, and no `allow-silent-exit` annotation."""
    raw = source_lines(text)
    said = shell_comments(text)
    hits = []
    for exit_command in iter_nodes(parse(text), "command"):
        if command_name(exit_command) != "exit" or not _literal_failure_status(
            exit_command
        ):
            continue
        if not _exits_the_script(exit_command):
            continue
        guard = _guarded_by_or(exit_command) or _guarded_by_if(exit_command)
        if guard is None:
            continue
        line = exit_command.start_point[0] + 1
        start = guard.start_point[0] + 1
        if annotated_near(raw, start, OPT_OUT, span_end=line, comments=said):
            continue
        hits.append(line)
    return sorted(set(hits))


def main(argv: list[str]) -> int:
    return run_line_checks(argv, violations, MESSAGE)


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

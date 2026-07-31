"""Shared tree-sitter-bash parsing for the shell-analyzing lints.

`check_workflow_pipefail` and `check_flag_arity` used to approximate bash with
hand-rolled char-by-char quote/heredoc state machines and a stack-based
`case…esac` scanner. Those approximations mis-parsed real shell — an escaped
quote (`"a\\""`), a `$'…'` ANSI-C string, a nested `$()`/backtick command
substitution, or a heredoc all desynced the quote state and hid (or invented) a
pipe. This module hands both lints a REAL bash grammar instead, so what the lint
sees is what bash would run.

It also owns the node-reading helpers every shell lint needs once it is on the
grammar. Each is one decision about what the grammar means — which children of a
`command` are really its arguments, what a command's name is when it runs no
program — and a lint holding its own copy of that decision is a lint answering a
structural question differently from its siblings, silently. One copy, here.

Fails LOUD when the grammar bindings are absent: a shell lint that silently
degrades to "no findings" on a missing dependency would be exactly the false
green this pack exists to catch, so the ImportError propagates rather than being
swallowed. The bindings are pinned as a hook runtime dependency
(pyproject `dependencies`, and each hook's `additional_dependencies`), so
pre-commit and CI always have them.
"""

import tree_sitter_bash
from tree_sitter import Language, Node, Parser


class PathologicalInputError(ValueError):
    """Raised instead of feeding tree-sitter an input shape measured to allocate
    quadratically. Deliberately LOUD: a lint that silently skipped the file
    would false-green exactly the input an adversary controls."""


# tree-sitter-bash's GLR machinery allocates roughly QUADRATICALLY in the number
# of chained pipeline stages: 5k `cmd |` stages cost ~330 MB, 20k cost ~3.3 GB,
# 50k exhaust a 16 GB host (measured via resource.ru_maxrss on tree-sitter-bash
# 0.25). A hostile or generated file can therefore take the whole process down
# inside the C parser — an allocation-failure segfault, not a Python exception —
# so `parse` refuses such inputs up front. Real shell sits orders of magnitude
# below the cap (this repo's largest script carries a few dozen `|` bytes).
_MAX_PIPE_BYTES = 2_000

# tree-sitter-bash 0.25's external (C) scanner corrupts the heap when it lexes a
# SUPPLEMENTARY-PLANE character (codepoint ≥ U+10000, a 4-byte UTF-8 sequence)
# adjacent to certain word-opening tokens — e.g. a `{` immediately followed by an
# astral char. The overrun is a memory-safety bug, not a Python exception: it
# scribbles past a lexeme buffer and segfaults the whole process (SIGSEGV)
# NON-DETERMINISTICALLY, depending on heap layout, so no input-level allowlist can
# be proven exhaustive. The only safe posture is to never hand such a character to
# the C parser. `parse` therefore folds every non-BMP codepoint down to U+FFFD
# (the Unicode REPLACEMENT CHARACTER, a BMP char the scanner lexes safely) before
# encoding. The substitution is one-char-for-one-char and touches no line
# boundary, so line numbers and character indices stay aligned for callers; a
# supplementary-plane char is a plain word byte to bash (never a metacharacter),
# so collapsing it to another word byte cannot change any lint's verdict.
_REPLACEMENT = "\ufffd"


def _neutralize_supplementary(script: str) -> str:
    """SCRIPT with every supplementary-plane (non-BMP) codepoint replaced by
    U+FFFD, so tree-sitter-bash's scanner never lexes the 4-byte sequence that
    corrupts its heap. One-to-one on characters (line count and character indices
    preserved); idempotent (U+FFFD is BMP, so a second pass is a no-op)."""
    if all(ord(char) <= 0xFFFF for char in script):
        return script
    return "".join(_REPLACEMENT if ord(char) > 0xFFFF else char for char in script)


# Building the Language once is cheap; reuse it across every parse in a run.
_PARSER: Parser | None = None


def _parser() -> Parser:
    global _PARSER
    if _PARSER is None:
        _PARSER = Parser(Language(tree_sitter_bash.language()))
    return _PARSER


def parse(script: str) -> Node:
    """The root node of SCRIPT parsed as bash.

    tree-sitter NEVER raises on malformed input — a syntax error surfaces as
    ``ERROR`` nodes in the tree, so callers fail OPEN (treat unparseable spans as
    benign) instead of crashing a pre-commit hook on an unrelated commit. The one
    exception is a pipe-byte count past ``_MAX_PIPE_BYTES``, which raises
    ``PathologicalInputError`` (loud, never a silent pass) rather than letting
    the C parser's quadratic allocation kill the process. Supplementary-plane
    characters, which segfault that same C parser, are folded to U+FFFD up front
    (see ``_neutralize_supplementary``)."""
    if script.count("|") > _MAX_PIPE_BYTES:
        raise PathologicalInputError(
            f"input carries more than {_MAX_PIPE_BYTES} pipe bytes; "
            "tree-sitter-bash allocates quadratically on chained pipelines, so "
            "parsing it could exhaust memory. Split the file or reduce the "
            "pipeline chain to lint it."
        )
    return _parser().parse(_neutralize_supplementary(script).encode("utf-8")).root_node


def iter_nodes(node: Node, *types: str):
    """Every descendant of NODE (inclusive) whose ``type`` is in TYPES, yielded in
    document (pre-order) order."""
    want = set(types)
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in want:
            yield current
        # Reverse so children are popped left-to-right → pre-order, source order.
        stack.extend(reversed(current.children))


# Child types of a `command` that carry an argument VALUE. A `file_redirect`, a
# `variable_assignment` prefix and heredoc plumbing are the children this leaves
# out on purpose — that exclusion is what keeps a `>&2` out of an argument list.
#
# The substitution types (`command_substitution`, `process_substitution`,
# `arithmetic_expansion`) are absent, so a bare `$(pwd)` argument is not in the
# list either. That is the set the lints have always used, and each of them is a
# separate judgement about whether a value only the shell can compute should be
# read as an argument at all; widening it is a behaviour change, not a spelling.
ARGUMENT_TYPES = frozenset(
    {
        "word",
        "string",
        "raw_string",
        "concatenation",
        "number",
        "simple_expansion",
        "expansion",
    }
)


def node_text(node: Node) -> str:
    """NODE's source text, decoded from the bytes tree-sitter indexes."""
    return node.text.decode("utf-8", "replace")


def unquote(raw: str) -> str:
    """RAW with one layer of matching surrounding quotes removed (`'/dev/null'` →
    `/dev/null`), so a quoted token compares equal to its bare spelling."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def command_name(node: Node) -> str | None:
    """The command word of NODE, or None when NODE is not a `command` at all.

    The type check is here because callers reach this one while WALKING (a `||`
    operand that may be a pipeline, a sibling statement); `command_arguments` and
    `command_words` take a `command` they already have and so do not repeat it.

    A `command` that runs no program (`FOO=1 >out`, all prefix and redirect) still
    carries a ZERO-WIDTH `command_name` in the grammar, so this returns `""` there:
    a name that matches nothing, which is the right answer, rather than None."""
    if node.type != "command":
        return None
    for child in node.children:
        if child.type == "command_name":
            return node_text(child)
    return None


def command_arguments(command: Node) -> list[Node]:
    """A `command` node's name and argument nodes, in order.

    The name is yielded as its CHILDREN, so a name the shell assembles
    (`"$tool"`, `bin/foo`) is read at the same granularity as an argument. Every
    non-argument child is skipped, so only real arguments can be read as a flag, a
    package spec, or an output target."""
    args: list[Node] = []
    for child in command.children:
        if child.type == "command_name":
            args.extend(child.children)
        elif child.type in ARGUMENT_TYPES:
            args.append(child)
    return args


def command_words(command: Node) -> list[str]:
    """COMMAND's name followed by its argument words, as written.

    The name stays ONE word here, unlike `command_arguments`, which is what a
    caller wants when it is about to strip wrapper prefixes (`sudo`, `command`,
    `env`) off the front and read the program name off the head."""
    return [
        node_text(child)
        for child in command.children
        if child.type == "command_name" or child.type in ARGUMENT_TYPES
    ]


# Every character `str.splitlines()` treats as a line boundary. A comment blanked
# for a line-oriented lint must keep these intact, or `strip_comments(text)` would
# have a different line count than `text` and desync a caller's line indexing (a
# bash comment ends only at `\n`, so it can legitimately contain a bare `\r`, `\v`,
# or a Unicode LS/PS that Python still splits on).
_LINE_BOUNDARIES = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def strip_comments(script: str) -> str:
    """SCRIPT with every bash ``comment`` node blanked to spaces, leaving every
    line-boundary character in place so ``strip_comments(script).splitlines()`` is
    index-aligned with ``script.splitlines()`` (same count, same line numbers).

    This lets a line-oriented lint match a token only in executed code, never in a
    ``#`` comment — and, because the grammar (not a naive ``#`` split) decides what
    a comment is, a ``#`` inside a quoted string or word (``curl -o "a#b" url``) is
    correctly left as code."""
    # `parse` folds supplementary-plane chars to U+FFFD before lexing, so the
    # tree's byte offsets index the NEUTRALIZED string, not `script`. Map them
    # against that same string; the fold is one-char-for-one-char, so character
    # index i in it is character index i in `script` and blanking `script[i]` below
    # stays correct.
    safe = _neutralize_supplementary(script)
    spans = [(n.start_byte, n.end_byte) for n in iter_nodes(parse(safe), "comment")]
    if not spans:
        return script
    # tree-sitter reports byte offsets; map them to character indices so blanking
    # respects multibyte Unicode boundaries.
    char_at_byte: dict[int, int] = {}
    byte = 0
    for index, char in enumerate(safe):
        char_at_byte[byte] = index
        byte += len(char.encode("utf-8"))
    char_at_byte[byte] = len(safe)

    out = list(script)
    for start_byte, end_byte in spans:
        for index in range(char_at_byte[start_byte], char_at_byte[end_byte]):
            if out[index] not in _LINE_BOUNDARIES:
                out[index] = " "
    return "".join(out)

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

import re
from collections.abc import Sequence
from functools import lru_cache
from typing import NamedTuple

import tree_sitter_bash
from tree_sitter import Language, Node, Parser


class PathologicalInputError(ValueError):
    """Raised instead of feeding tree-sitter an input shape measured to allocate
    quadratically. Deliberately LOUD: a lint that silently skipped the file
    would false-green exactly the input an adversary controls."""


class UnparseableShellError(ValueError):
    """Raised when the grammar could not parse a file, so no lint over it can
    claim the file is clean.

    PROBLEM CLASS — a checker that cannot read its input reports the same empty
    result as a checker that read it and found nothing. tree-sitter never raises
    on malformed input; it recovers locally into `ERROR` nodes and keeps going,
    and the recovery is NOT local in effect: one unparsed construct drops the
    nodes a detector matches on for the rest of the file. Measured on
    tree-sitter-bash 0.25.1, a single `${x%]*}` on line 2 made
    `check_exit_suppression` miss a `|| true` on line 3 that it reports on line
    3 of an otherwise identical file.

    This refusal is what stops that empty result from reading as a pass. It is
    the rule `_cts_linecheck.run_source_checks` already states for an unparseable
    YAML workflow — "no findings" is a false-green on the very file under test —
    applied to shell, which had been the exception.

    Known collapse triggers in tree-sitter-bash 0.25.1, each with a
    behaviour-identical spelling that parses:
      * `${x%]*}` / `${x%%]*}` — quote the bracket: `${x%"]"*}`.
      * `$((10#$x))`, `((8#$m & 8#77))` — a base prefix inside arithmetic; hold
        the value in a variable first, or drop the explicit base.
      * `[^]]` written inline in `[[ =~ ]]` — hold the regex in a variable.
    """


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
_SUPPLEMENTARY = re.compile("[\U00010000-\U0010ffff]")


def _neutralize_supplementary(script: str) -> str:
    """SCRIPT with every supplementary-plane (non-BMP) codepoint replaced by
    U+FFFD, so tree-sitter-bash's scanner never lexes the 4-byte sequence that
    corrupts its heap. One-to-one on characters (line count and character indices
    preserved); idempotent (U+FFFD is BMP, so a second pass is a no-op).

    A regex, not a per-character Python loop: every cache miss in `parse` runs
    this over the whole script, and the loop was most of a shell lint's time."""
    if _SUPPLEMENTARY.search(script) is None:
        return script
    return _SUPPLEMENTARY.sub(lambda _: _REPLACEMENT, script)


# Building the Language once is cheap; reuse it across every parse in a run.
# `lru_cache` rather than a module-level binding a function writes: the cache
# carries its own `cache_clear`, so a caller that must not inherit a previous
# run's parser can drop it.
@lru_cache(maxsize=1)
def _parser() -> Parser:
    return Parser(Language(tree_sitter_bash.language()))


# Big enough that a file's own tree survives the fragment parses its detector
# makes — a `run:` block, a literal script, a `bash -c` argument all come
# through here too, and a one-entry cache would let them evict the whole-file
# tree between the two callers that share it.
@lru_cache(maxsize=32)
def parse(script: str) -> Node:
    """The root node of SCRIPT parsed as bash.

    Memoized on SCRIPT. `run_tier` hands one file to every member of a tier in
    turn, and each member parsed it again: the check that asks whether the file
    parses at all (`assert_parseable`) threw its tree away, and the detector
    behind it parsed the same text a second time. A tree-sitter tree is read
    only to these lints, so one parse serves every caller of it.

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


def assert_parseable(script: str) -> None:
    r"""Raise `UnparseableShellError` when the grammar cannot read SCRIPT.

    Call this only where a lint claims a whole FILE of real shell is clean.
    `parse` itself stays permissive on purpose, because two callers legitimately
    hand it text the bash grammar cannot read and must not be refused: a
    fragment taken out of its file (a `cat <<EOF` opener, a trailing `\`
    continuation), and a workflow `run:` block carrying `${{ … }}`, which is
    GitHub Actions syntax rather than bash.
    """
    root = parse(script)
    if root.has_error:
        line, byte_offset = _first_error_location(root, script)
        raise UnparseableShellError(
            f"the bash grammar could not parse this file (first unparsed "
            f"construct near line {line}, byte offset {byte_offset}). Fix the "
            "syntax (see UnparseableShellError for known triggers and their "
            "behaviour-identical rewrites), or add this file to the shell "
            "checks' exclude: pattern in .pre-commit-config.yaml if it is not "
            "real bash."
        )


def _first_error_location(root: Node, script: str) -> tuple[int, int]:
    """1-based line and 0-based byte offset, both in the ORIGINAL SCRIPT, of the
    earliest ERROR or missing node — the one construct a reader has to rewrite.

    `root` was parsed from `_neutralize_supplementary(script)`, so a node's own
    `start_byte` indexes the astral-folded string's UTF-8 bytes, not SCRIPT's: a
    folded char is 3 bytes (U+FFFD) where the original was 4, so the two byte
    streams diverge after the first supplementary-plane char. `_neutralize_
    supplementary` is one-to-one on CHARACTERS, so a byte offset is mapped to a
    character index in the folded string first, then re-encoded against SCRIPT
    up to that same character index to land back on SCRIPT's own bytes."""
    candidates = [
        (node.start_point[0] + 1, node.start_byte)
        for node in iter_nodes(root, "ERROR")
        if node.type == "ERROR"
    ]
    candidates += [
        (node.start_point[0] + 1, node.start_byte) for node in _walk_missing(root)
    ]
    line, safe_byte_offset = min(candidates, default=(1, 0))
    safe = _neutralize_supplementary(script)
    char_index = _char_index_at_byte(safe, safe_byte_offset)
    return line, len(script[:char_index].encode("utf-8"))


def _char_index_at_byte(safe: str, byte_offset: int) -> int:
    """The character index in SAFE whose UTF-8 encoding starts at BYTE_OFFSET."""
    total = 0
    for index, char in enumerate(safe):
        if total >= byte_offset:
            return index
        total += len(char.encode("utf-8"))
    return len(safe)


def _walk_missing(node: Node):
    """Every `is_missing` descendant: tree-sitter marks an omitted token that way
    rather than as an ERROR node, so a scan for ERROR alone can find nothing on a
    tree that `has_error` reports as broken."""
    stack = [node]
    while stack:
        current = stack.pop()
        if current.is_missing:
            yield current
        stack.extend(current.children)


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
    """RAW after bash's quote removal, so a quoted token compares equal to its
    bare spelling: `'/dev/null'`, `"/dev/null"`, `\\/dev/null` and `/dev/"null"`
    all read `/dev/null`. An expansion keeps its text (`"$HOME"` reads `$HOME`).
    RAW that is not one shell word (`a b`, `>&2`) loses only one layer of
    matching surrounding quotes."""
    # A word with no quote and no backslash is its own value, so it needs no parse.
    if not any(char in raw for char in "\"'\\"):
        return raw
    word = _word(raw)
    if word is not None:
        return _quote_removed(word)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


# A separate cache from `parse`: one entry per word would evict the file trees
# that the lints share there.
@lru_cache(maxsize=4096)
def _word(raw: str) -> Node | None:
    """The node that spells RAW when the grammar reads RAW as one argument word,
    or None. RAW is parsed after `: `, so `x=1` and `while` read as arguments,
    not as an assignment or a keyword. The node text must be RAW itself, so RAW
    is not folded as `parse` folds it. RAW that crashes the C parser, or that
    `parse` refuses, is None."""
    if _SUPPLEMENTARY.search(raw) is not None or raw.count("|") > _MAX_PIPE_BYTES:
        return None
    root = _parser().parse(f": {raw}".encode("utf-8")).root_node
    if root.has_error or root.named_child_count != 1:
        return None
    command = root.named_children[0]
    if command.type != "command" or command.child_count != 2:
        return None
    argument = command.children[1]
    if (argument.start_byte, argument.end_byte) != (2, 2 + len(raw.encode("utf-8"))):
        return None
    return argument


# Outside quotes a backslash keeps the next character, and `\<newline>` is
# removed. Inside double quotes it does so only before these characters.
_BACKSLASH = re.compile(r"\\(?P<char>.)", re.DOTALL)
_DOUBLE_QUOTED_BACKSLASH = re.compile(r'\\(?P<char>[$`"\\\n])')


def _kept(match: re.Match[str]) -> str:
    return "" if match["char"] == "\n" else match["char"]


def _quote_removed(node: Node) -> str:
    """The text of the word NODE after quote removal. A part that is not quoted
    text, such as an expansion, keeps its text."""
    text = node_text(node)
    if node.type == "word":
        return _BACKSLASH.sub(_kept, text)
    if node.type == "raw_string":
        return text[1:-1]
    # The escapes of `$'…'` are not decoded here, so a word with one keeps its
    # text and matches no bare spelling.
    if node.type == "ansi_c_string" and "\\" not in text:
        return text[2:-1]
    if node.type == "string":
        return _string_removed(node)
    if node.type == "concatenation":
        return "".join(map(_quote_removed, node.children))
    return text


def _string_removed(node: Node) -> str:
    """The text of the `"…"` string NODE after quote removal. The grammar
    leaves a newline and a lone `$` out of every child, so the literal text is
    read from the gaps between the expansions, never from the children."""
    raw = node.text or b""
    base = node.start_byte
    pieces = []
    start = 1
    for child in node.named_children:
        if child.type != "string_content":
            pieces.append(_literal(raw[start : child.start_byte - base]))
            pieces.append(node_text(child))
            start = child.end_byte - base
    pieces.append(_literal(raw[start:-1]))
    return "".join(pieces)


def _literal(text: bytes) -> str:
    """TEXT, a run of double-quoted literal text, after quote removal."""
    return _DOUBLE_QUOTED_BACKSLASH.sub(_kept, text.decode("utf-8", "replace"))


class RedirectParts(NamedTuple):
    """A `file_redirect`'s pieces: the descriptor it points (None for the
    operator's default), the operator token, and the destination after quote
    removal ("" when the redirect names none)."""

    descriptor: "str | None"
    operator: str
    destination: str


# `{name}>file` makes bash pick a free descriptor and store its number in NAME.
_BRACE_DESCRIPTOR = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


def _brace_descriptor(redirect: Node) -> "str | None":
    """`{name}` when REDIRECT is `{name}>…`. The grammar parses `{name}` as the
    command's last argument word, ending where the redirect starts."""
    before = redirect.prev_sibling
    if before is not None and before.type == "command" and before.children:
        before = before.children[-1]
    if before is None or before.end_byte != redirect.start_byte:
        return None
    text = node_text(before)
    return text if _BRACE_DESCRIPTOR.fullmatch(text) else None


def redirect_parts(redirect: Node) -> RedirectParts:
    """(descriptor, operator, destination) of a ``file_redirect``.

    The destination is the token right AFTER the operator, not the node's last
    child: the grammar folds a following argument into the same `file_redirect`
    (`foo >/dev/null "a;b"` parks the string there)."""
    children = redirect.children
    index = next((i for i, c in enumerate(children) if not c.is_named), None)
    descriptor = next(
        (node_text(c) for c in children if c.type == "file_descriptor"), None
    ) or _brace_descriptor(redirect)
    if index is None:
        return RedirectParts(descriptor, "", "")
    after = children[index + 1] if index + 1 < len(children) else None
    destination = "" if after is None else unquote(node_text(after))
    return RedirectParts(descriptor, children[index].type, destination)


def program_name(word: str) -> str:
    """WORD as a program name: quotes removed, directories stripped, so
    `/usr/bin/timeout` reads as `timeout`."""
    return unquote(word).rsplit("/", 1)[-1]


# Programs that only name the command after them (`which docker`).
_LOOKUP_PROGRAMS = frozenset({"type", "which", "hash", "whereis"})
# The builtins of bash 5.2, from `compgen -b`. `builtin` runs only these.
BASH_BUILTINS = frozenset(
    {".", ":", "[", "alias", "bg", "bind", "break", "builtin", "caller", "cd"}
    | {"command", "compgen", "complete", "compopt", "continue", "declare"}
    | {"dirs", "disown", "echo", "enable", "eval", "exec", "exit", "export"}
    | {"false", "fc", "fg", "getopts", "hash", "help", "history", "jobs"}
    | {"kill", "let", "local", "logout", "mapfile", "popd", "printf", "pushd"}
    | {"pwd", "read", "readarray", "readonly", "return", "set", "shift"}
    | {"shopt", "source", "suspend", "test", "times", "trap", "true", "type"}
    | {"typeset", "ulimit", "umask", "unalias", "unset", "wait"}
)


def _prefix_options(words: Sequence[str]) -> tuple[list[str], list[str]]:
    """The options after WORDS[0], and the words after those options. A `--`
    ends the options, and a `-` alone is not one."""
    options: list[str] = []
    rest = list(words[1:])
    while rest and unquote(rest[0]).startswith("-") and unquote(rest[0]) != "-":
        option = unquote(rest.pop(0))
        if option == "--":
            break
        options.append(option)
    return options, rest


def is_lookup(words: Sequence[str]) -> bool:
    """Whether WORDS[0] names the words after it without running them. A
    `command` is a lookup when an option before its command holds `v` or `V`
    (`command -pv docker`). `command -p docker` still RUNS docker."""
    if not words:
        return False
    name = program_name(words[0])
    if name in _LOOKUP_PROGRAMS:
        return True
    options = _prefix_options(words)[0] if name == "command" else []
    return any({"v", "V"} & set(option[1:]) for option in options)


def unprefixed(words: Sequence[str]) -> list[str]:
    """The words of the command that WORDS runs, after each `command` or
    `builtin` word before it: `command cd sub` runs `cd sub`. The result is []
    when WORDS runs nothing:
    - `command -v` and `command -V` only print how the shell reads a name;
    - bash fails `command` and `builtin` on an option that it does not know,
      and `command -p` still runs the command;
    - `builtin` runs only a shell builtin, so `builtin make` fails.

    Only a shell reads a bare `command` or `builtin` word this way. A wrapper
    runs a program with that name (`env command make`, `./builtin cd`), so call
    this on the words of a shell command, never on the words after a wrapper."""
    rest = list(words)
    while rest and unquote(rest[0]) in ("command", "builtin"):
        name = unquote(rest[0])
        options, rest = _prefix_options(rest)
        if any(name == "builtin" or set(o[1:]) - {"p"} for o in options):
            return []
        if name == "builtin" and rest and unquote(rest[0]) not in BASH_BUILTINS:
            return []
    return rest


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


# Statements that run a condition and branch on its exit status. The grammar
# parses `until` as a `while_statement` too, so there is no `until_statement`.
CONDITION_STATEMENTS = frozenset({"if_statement", "elif_clause", "while_statement"})
# The children that end a statement's condition. An `if` or `elif` holds its
# body after a bare `then` token, and a loop holds it in a `do_group` node.
_CONDITION_END_TYPES = frozenset({"then", "do_group"})


def condition_parts(statement: Node) -> list[Node]:
    """The statements that form STATEMENT's condition, or [] when STATEMENT is
    not in `CONDITION_STATEMENTS`.

    Read by position, not by the `condition` field: the grammar sets that field
    on `if` and `while` but not on `elif_clause`."""
    if statement.type not in CONDITION_STATEMENTS:
        return []
    parts: list[Node] = []
    for child in statement.children:
        if child.type in _CONDITION_END_TYPES:
            break
        if child.is_named:
            parts.append(child)
    return parts


def in_condition(node: Node) -> bool:
    """Whether NODE is one of the statements that form its parent's condition."""
    parent = node.parent
    return parent is not None and any(
        part.id == node.id for part in condition_parts(parent)
    )


def condition_commands(root: Node, statements: frozenset[str]) -> list[Node]:
    """Every `command` under ROOT that runs inside the condition of a statement
    whose type is in STATEMENTS, at any depth: under `!`, in a list or a
    pipeline, or in a `$(…)`. A function the condition defines does not run
    its body, so no command inside that body counts."""
    commands: list[Node] = []
    for statement in iter_nodes(root, *statements):
        for part in condition_parts(statement):
            commands.extend(
                command
                for command in iter_nodes(part, "command")
                if not _inside_definition(command, part)
            )
    return commands


def _inside_definition(node: Node, top: Node) -> bool:
    """Whether a `function_definition` sits between NODE and TOP, TOP included.
    NODE lies at or under TOP."""
    current: Node | None = node
    while current is not None and current.id != top.id:
        current = current.parent
        if current is not None and current.type == "function_definition":
            return True
    return False


# Every character `str.splitlines()` treats as a line boundary. A comment blanked
# for a line-oriented lint must keep these intact, or `strip_comments(text)` would
# have a different line count than `text` and desync a caller's line indexing (a
# bash comment ends only at `\n`, so it can legitimately contain a bare `\r`, `\v`,
# or a Unicode LS/PS that Python still splits on).
_LINE_BOUNDARIES = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_NOT_LINE_BOUNDARY = re.compile(
    "[^" + re.escape("".join(sorted(_LINE_BOUNDARIES))) + "]"
)


def comment_spans(script: str) -> list[tuple[int, int]]:
    """(start, end) CHARACTER offsets in SCRIPT of every bash ``comment`` node,
    in source order.

    PROBLEM CLASS — where does a shell comment start and end, as offsets a
    caller can slice with. tree-sitter reports UTF-8 BYTE offsets into the
    astral-folded text ``parse`` lexed, so every caller that blanks or keeps a
    comment by position must map them back. One mapping, here.
    """
    # `parse` folds supplementary-plane chars to U+FFFD before lexing, so the
    # tree's byte offsets index the NEUTRALIZED string, not `script`. The fold
    # is one-char-for-one-char, so character index i in it is index i in
    # `script`. Comments never nest, so with the spans sorted one forward pass
    # decodes each byte gap exactly once.
    safe = _neutralize_supplementary(script)
    encoded = safe.encode("utf-8")
    spans: list[tuple[int, int]] = []
    byte = char = 0
    for start_byte, end_byte in sorted(
        (n.start_byte, n.end_byte) for n in iter_nodes(parse(safe), "comment")
    ):
        start = char + len(encoded[byte:start_byte].decode("utf-8"))
        end = start + len(encoded[start_byte:end_byte].decode("utf-8"))
        spans.append((start, end))
        byte, char = end_byte, end
    return spans


def strip_comments(script: str) -> str:
    """SCRIPT with every bash ``comment`` node blanked to spaces, leaving every
    line-boundary character in place so ``strip_comments(script).splitlines()`` is
    index-aligned with ``script.splitlines()`` (same count, same line numbers).

    This lets a line-oriented lint match a token only in executed code, never in a
    ``#`` comment — and, because the grammar (not a naive ``#`` split) decides what
    a comment is, a ``#`` inside a quoted string or word (``curl -o "a#b" url``) is
    correctly left as code."""
    pieces: list[str] = []
    char = 0
    for start, end in comment_spans(script):
        pieces.append(script[char:start])
        pieces.append(_NOT_LINE_BOUNDARY.sub(" ", script[start:end]))
        char = end
    pieces.append(script[char:])
    return "".join(pieces)

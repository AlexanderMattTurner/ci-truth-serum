#!/usr/bin/env python3
"""Flag a secret file created world-readable and only ``chmod``'d private AFTERWARD.

A credential/secret file created with the process umask (typically 0644 —
world-readable) and only tightened to 0600/0400 on a LATER line leaves a window
between the create and the ``chmod`` in which a co-tenant on the host can read
the secret. The correct idiom creates the file private from the start —
``(umask 077; …)``, ``install -m 600 …``, an ``O_EXCL`` mode-0600 open, or a
``printf … >file`` run under a standing ``umask 077`` — so no readable window
exists.

The script is parsed with the real bash grammar (``_bash_ast``). This lint's whole
question is about ORDER and SCOPE — does a chmod follow this create, and does a
``umask`` cover it? — and the grammar answers both as structure rather than as a
line-distance proxy: "the next few statements" is the create's statement plus its
siblings, and "this ``umask`` guards this create" is the two sharing a statement
(the ``(umask 077; … >file)`` subshell) or the umask standing at file scope. A
redirect's target is the ``file_redirect``'s destination, so nothing has to
reconstruct which ``>`` opens a file and which is part of a ``2>&1``, and a
create written inside a printed message or a heredoc body is not a command at all.

The heuristic, kept deliberately narrow so the false-positive rate is ~zero:
  * A CREATE writes a file at a SECRET-NAMED path — the target's text matches a
    case-insensitive secret keyword (token, secret, cred, key, passwd, password,
    npmrc, auth, pem, refresh, cookie, id_rsa) — via a ``>``/``>>`` redirect,
    ``touch``, ``tee``, or ``install`` WITHOUT a private ``-m 0?[46]00`` mode.
  * The create is UNGUARDED: no ``umask 0?77`` stands at file scope before it and
    none shares its statement, and it is not an ``install -m 0?[46]00``.
  * A VIOLATION is such an unguarded secret create FOLLOWED, within its own
    statement or the next ``_LOOKAHEAD``, by a ``chmod 0?[46]00`` on the SAME
    target path. The later-chmod is the strong signal that the author knew the
    file must be private but created it readable first; requiring the
    create+chmod PAIR on a secret-named path is what keeps this near
    zero-false-positive. A create with no nearby chmod is NOT flagged — an
    unguarded secret create that is never tightened is a different, non-decidable
    class this lint does not attempt.
  * EXEMPT: a create carrying ``# secret-perms-ok: <reason>`` (reason required)
    on one of its own lines.

Invoked by pre-commit with the changed shell files as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    run_line_checks,
)

OPT_OUT = "secret-perms-ok"

MESSAGE = (
    "creates a secret file world-readable and only chmods it private "
    "afterward — a co-tenant can read it in the window between; create it "
    "private from the start (`(umask 077; …)`, `install -m 600 …`, or an "
    f"O_EXCL 0600 open), or annotate `# {OPT_OUT}: <reason>`"
)

# A path token is secret-named when its text contains one of these (case-insensitive).
# Substrings on purpose: `.credentials.json` matches `cred`, `gateway-key.pem`
# matches both `key` and `pem`, `refresh-token` matches `refresh` and `token`.
_SECRET_RE = re.compile(
    r"token|secret|cred|key|passwd|password|npmrc|auth|pem|refresh|cookie|id_rsa",
    re.IGNORECASE,
)

# An owner-only mode (`600`, `0400`) — as a chmod argument or an `install -m` value.
_PRIVATE_MODE_RE = re.compile(r"^0?[46]00$")
# A umask that clears every group/other bit, so files land 0600.
_PRIVATE_UMASK_RE = re.compile(r"^0*77$")

_LOOKAHEAD = 3  # statements after the create's own to search for its chmod

# Commands that create a file at a path given as a positional argument.
_CREATORS = frozenset({"touch", "tee"})
# Prefixes that run the following word as the command.
_WRAPPERS = frozenset({"sudo", "doas", "command", "env", "exec", "nice", "time"})
# Flags that consume the following token as their value, so it is not a path.
_VALUE_FLAGS = frozenset({"-m", "--mode", "-o", "--owner", "-g", "--group", "-t"})

# Child types of a `command` that carry an argument value.
_ARGUMENT_TYPES = frozenset(
    {"word", "string", "raw_string", "concatenation", "number", "simple_expansion"}
)
# Redirect operators that OPEN a file: `>` truncates, `>>` appends, `&>`/`&>>`
# send both streams to it. `>&` is a descriptor dup (`2>&1`) and opens nothing.
_WRITE_OPERATORS = frozenset({">", ">>", "&>", "&>>"})

_ANNOTATION_RE = annotation_re(OPT_OUT)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def _unquote(raw: str) -> str:
    """A quoted token's literal text, for comparing a create's target against a
    chmod's (`"$dir/token"` and `$dir/token` name the same file)."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw.strip("\"'")


def _words(command) -> list[str]:
    """COMMAND's name followed by its argument words, wrapper prefixes and
    environment assignments stripped, so the first entry is the program run."""
    words = [
        _text(child)
        for child in command.children
        if child.type == "command_name" or child.type in _ARGUMENT_TYPES
    ]
    while words and (words[0].rsplit("/", 1)[-1] in _WRAPPERS or "=" in words[0]):
        words = words[1:]
    return words


def _positionals(words: list[str]) -> list[str]:
    """WORDS with flags — and the values of the flags that take one — removed."""
    out: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("-"):
            skip = word in _VALUE_FLAGS
            continue
        out.append(word)
    return out


def _mode_flag(words: list[str]) -> str | None:
    """The value of an ``-m``/``--mode`` flag, however it is spelled."""
    for index, word in enumerate(words):
        if word in ("-m", "--mode") and index + 1 < len(words):
            return words[index + 1]
        for prefix in ("-m", "--mode="):
            if word.startswith(prefix) and len(word) > len(prefix):
                return word[len(prefix) :]
    return None


def _redirect_target(redirect) -> str | None:
    """The path REDIRECT opens for writing, or None when it opens no file — a
    ``<`` reads, and a ``>&`` points one descriptor at another."""
    children = redirect.children
    index = next(
        (i for i, child in enumerate(children) if child.type in _WRITE_OPERATORS),
        None,
    )
    if index is None or index + 1 >= len(children):
        return None
    return _unquote(_text(children[index + 1]))


def _install_targets(words: list[str]) -> list[str]:
    """Path args of an ``install`` that is NOT a private create: a directory
    install (``-d``) and one already setting a private ``-m 0?[46]00`` mode both
    create nothing world-readable."""
    mode = _mode_flag(words)
    directory = any(
        word.startswith("--directory") or (re.match(r"^-[a-zA-Z]*d", word))
        for word in words
        if word.startswith("-")
    )
    if directory or (mode is not None and _PRIVATE_MODE_RE.match(mode)):
        return []
    return _positionals(words)[1:]


def _creates(root) -> list[tuple[object, list[str]]]:
    """(node, secret-named paths it creates) for every create in ROOT.

    Redirects are walked as themselves rather than through the command they
    decorate, because a redirect can belong to a whole group
    (``{ printf x; } > tokenfile``) where no single command owns it. Guarding
    (umask, private install mode) is decided by the caller."""
    found: list[tuple[object, list[str]]] = []
    for redirect in iter_nodes(root, "file_redirect"):
        target = _redirect_target(redirect)
        if target is not None and _SECRET_RE.search(target):
            found.append((redirect, [target]))
    for command in iter_nodes(root, "command"):
        words = _words(command)
        head = words[0].rsplit("/", 1)[-1] if words else ""
        if head in _CREATORS:
            raw = _positionals(words)[1:]
        elif head == "install":
            raw = _install_targets(words)
        else:
            continue
        targets = [_unquote(t) for t in raw if _SECRET_RE.search(t)]
        if targets:
            found.append((command, targets))
    return found


def _chmod_targets(command) -> list[str]:
    """Paths a private ``chmod 0?[46]00`` on COMMAND tightens (empty if it is not
    one)."""
    words = _words(command)
    if not words or words[0].rsplit("/", 1)[-1] != "chmod":
        return []
    positionals = _positionals(words)[1:]
    if not positionals or not _PRIVATE_MODE_RE.match(positionals[0]):
        return []
    return [_unquote(target) for target in positionals[1:]]


def _is_private_umask(command) -> bool:
    """True when COMMAND is a ``umask 0?77`` that makes later creates private."""
    words = _words(command)
    return (
        len(words) == 2
        and words[0].rsplit("/", 1)[-1] == "umask"
        and bool(_PRIVATE_UMASK_RE.match(words[1]))
    )


def _statement(node):
    """The top-level statement NODE belongs to — the ancestor that is a direct
    child of the program. This is the unit "the next few statements" counts."""
    while node.parent is not None and node.parent.type != "program":
        node = node.parent
    return node


def _guarded(create, umasks: list) -> bool:
    """True when a ``umask 0?77`` covers CREATE: one standing at file scope
    earlier in the script, or one sharing CREATE's statement — the
    ``(umask 077; … >file)`` subshell, whose umask applies only inside it."""
    statement = _statement(create).id
    return any(
        umask.start_byte < create.start_byte
        and (umask.parent.type == "program" or _statement(umask).id == statement)
        for umask in umasks
    )


def _window(create, statements: list) -> list:
    """CREATE's own statement and the next ``_LOOKAHEAD`` — where a chmod is
    close enough to read as tightening THIS create rather than as unrelated
    later work."""
    statement = _statement(create).id
    index = next((i for i, node in enumerate(statements) if node.id == statement), None)
    return [] if index is None else statements[index : index + 1 + _LOOKAHEAD]


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT of unguarded secret creates that are tightened
    by a nearby later chmod, without a ``# secret-perms-ok:`` opt-out."""
    lines = text.split("\n")
    root = parse(text)
    statements = [child for child in root.children if child.type != "comment"]
    umasks = [c for c in iter_nodes(root, "command") if _is_private_umask(c)]

    hits = set()
    for create, targets in _creates(root):
        if _guarded(create, umasks):
            continue
        tightened = {
            target
            for statement in _window(create, statements)
            for later in iter_nodes(statement, "command")
            if later.start_byte > create.start_byte
            for target in _chmod_targets(later)
        }
        if not tightened & set(targets):
            continue
        start = create.start_point[0] + 1
        # The create's own lines carry the opt-out; a redirect can push its end
        # past the command word's, so the span covers the whole statement.
        end = _statement(create).end_point[0] + 1
        if not any(_ANNOTATION_RE.search(line) for line in lines[start - 1 : end]):
            hits.add(start)
    return sorted(hits)


def main(argv: list[str]) -> int:
    """Run the detector over ARGV through the shared read/report loop, one path at a
    time so a file the grammar refuses to parse safely fails LOUDLY (naming the
    path, exit 1) instead of being silently skipped, while every remaining path is
    still checked."""
    status = 0
    for path in argv:
        try:
            status = max(status, run_line_checks([path], violations, MESSAGE))
        except PathologicalInputError as err:
            print(f"{path}: {err}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

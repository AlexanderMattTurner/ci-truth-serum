#!/usr/bin/env python3
"""Demand that every downloaded artifact is checksum/signature-verified.

A ``curl``/``wget`` that saves a file to disk and is then run, installed, or
extracted is a supply-chain entry point: without verifying the bytes against a
pinned digest, a compromised mirror or a tampered release silently swaps what
you execute. The same is true of a Dockerfile ``ADD <url> <dest>``, which writes
the remote bytes straight into the image. This check fires on any ``curl``/``wget``
invocation that writes an artifact — an explicit output flag (``-o FILE`` / ``-O`` /
``--output`` / ``--remote-name``), a shell redirect into a file (``> FILE`` /
``>> FILE``), a bare ``wget <url>`` (which saves to disk by default), or a pipe
straight into a shell (``curl … | sh`` / ``curl -fsSL … | sudo bash``, the marquee
one-line installer, which never touches disk but *executes* the unverified bytes) —
and on any ``ADD`` from an ``http(s)://`` URL, unless a verification token appears
close after it:

  * ``sha256sum`` / ``sha512sum`` / ``shasum`` / ``md5sum`` (a ``… -c`` check)
  * ``cosign verify`` or ``gpg --verify`` (signature check)
  * ``_sha256_verify`` (a common verify-helper naming)
  * ``ADD --checksum=sha256:<digest>`` (Docker's own built-in pin)

The scan runs on the REAL bash grammar (``_bash_ast``), walking ``command`` nodes,
so the questions that sink a text scan are answered by structure instead of
guessed at:

  * a download named inside a quoted message (``gb_warn "curl -o t $url"``) is a
    ``string`` argument of the command that prints it — one token, holding no
    commands, so it is text and never a finding;
  * a ``heredoc_body`` written to a file (``cat <<'EOF' > doc.txt``) is data, so
    the grammar yields no commands from it at all — while the same body fed to an
    interpreter (``bash <<'EOF'``) is a script, and is parsed as one;
  * a redirection is a ``file_redirect`` under the command's
    ``redirected_statement``, never an argument — which is what keeps a ``>&2``
    (an ``>&`` operator, or a ``2>`` carrying a ``file_descriptor``) out of the
    output-target list;
  * ``|`` / ``&&`` / ``;`` split a ``pipeline``/``list`` into separate commands,
    and one written inside a string splits nothing — so the interpreter that a
    download is piped INTO is a real downstream stage, matched by token rather
    than by "some shell name appears after a ``|`` character";
  * an inline fetch the interpreter executes (``bash -c "$(curl …)"``,
    ``bash <(curl …)``, ``eval "$(curl …)"``) is a
    ``command_substitution``/``process_substitution`` child carrying a real
    ``curl`` command.

Downloads to ``/dev/null``/``/dev/stdout``/``-`` (reachability probes, piped
API reads to a data reader like ``| jq``) are not artifacts and are ignored — but a
stdout sink piped into a *shell* (``curl -O- … | sh``) still executes, so it fires.
A download that genuinely cannot be pinned opts out with a same-line or
preceding-line ``# pin-exempt: <reason>``.

Invoked by pre-commit with the staged shell + Dockerfile paths as arguments.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ARGUMENT_TYPES,
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
)

OPT_OUT = "pin-exempt"

MESSAGE = (
    "downloaded artifact is not checksum/signature verified — add a "
    f"sha256sum/cosign/gpg check after it, or annotate `# {OPT_OUT}: <reason>`"
)

# How many lines after a download to scan for its verification before giving up.
# The scan also stops at the next download, so one check can't cover two.
_WINDOW = 25

_WGET = "wget"
_DOWNLOADERS = frozenset({"curl", _WGET})

# Any `scheme://` value. A token carrying one is a URL the fetch reads, so it is
# never the name of the command being run.
_URL_SCHEME = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://")

# An output flag that makes the fetch write a file. `-o`/`--output` and wget's
# `-O` take a target (so a /dev/null/stdout/- sink can be excused); curl's `-O`
# and `--remote-name` derive the name from the URL and take none. The target may
# be the next token (`-o f`), `=`-joined (`--output=f`), or the `-` glued after a
# short-flag cluster (`wget -qO-`, write to stdout). `o`/`O` is recognized at the
# END of a cluster (`-qO-`, `-sSLo f`, `-fsSLO`): a `-q` on wget is quiet mode,
# not an output flag, and misreading `-qO-` as flag-less makes a piped stdout read
# look like a "bare wget artifact download". A cluster whose target is GLUED after
# a non-final `o`/`O` (`-Oq`) is not an output flag and conservatively leaves the
# fetch an artifact download.
_OUTPUT_FLAG = re.compile(
    r"^(?:-[A-Za-z]*[oO]|--output|--remote-name(?:-all)?)(?P<glued>|=\S*|-)$"
)

_NULL_TARGETS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "-"})

# The shells whose `-c` argument is a script, plus the commands whose arguments are
# code by definition (`eval`'s every argument, `ssh`'s remote command). A string or
# heredoc body reached this way is parsed as its own script — the one place where
# quoted text and heredoc data are treated as commands. The shell names also
# identify the interpreter a download is piped into, or the one running an inline
# `$(curl …)`.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "busybox"})
_EVAL = "eval"
_SSH = "ssh"

# A remote source for a Dockerfile `ADD`. `ADD` of a build-context path fetches
# nothing; only an http(s) URL pulls bytes into the image.
_HTTP_URL = re.compile(r"https?://")
_ADD = "ADD"

# Verification a download can be held to. Each is matched as a COMMAND TOKEN, so a
# `sha256sum` named inside a message string (one `string` token) verifies nothing.
_VERIFY_COMMANDS = frozenset(
    {
        "sha256sum",
        "sha512sum",
        "sha384sum",
        "sha1sum",
        "shasum",
        "md5sum",
        "_sha256_verify",
    }
)
_CHECKSUM_FLAG = "--checksum=sha256:"  # Docker `ADD --checksum=sha256:<digest> <url>`

# The redirection operators that write the fetched bytes into a file. `>&`/`&>`
# (an FD dup, `>&2`) and `>|` are excluded, as is any redirect carrying a
# `file_descriptor` (`2>/dev/null` routes diagnostics, not the artifact).
_WRITE_OPERATORS = frozenset({">", ">>"})

# Nodes that pass a redirection down to the commands inside them: `{ curl …; } > f`
# and `( curl …; ) > f` send each grouped command's stdout to the file. A
# `pipeline` is deliberately absent — inside `{ curl … | jq .; } > f` the fetch
# writes to `jq`, not to the file.
_REDIRECT_WRAPPERS = frozenset({"compound_statement", "subshell", "list"})


def _tokens(command) -> list[str]:
    """A `command` node's argument tokens as literal text. A quoted message is ONE
    token, so a command name spelled inside it never matches a command word."""
    return [unquote(node_text(node)) for node in command_arguments(command)]


def _names(tokens: list[str]) -> set[str]:
    """The command names TOKENS could be invoking: each token's basename, so
    `/bin/sh` reads as `sh` and a leading wrapper (`sudo`, `retry`, `RUN`) leaves
    the real name matchable at any position. A URL is an argument value and never
    a name, so its last path segment stays out (`https://cdn/curl` invokes
    nothing)."""
    return {
        token.rsplit("/", 1)[-1] for token in tokens if not _URL_SCHEME.search(token)
    }


def _output_target(tokens: list[str]) -> tuple[bool, str | None]:
    """(an output flag is present, the file it names) for a fetch's TOKENS.

    The target is None when the flag derives the name from the URL (curl's `-O`,
    `--remote-name`) — still a real artifact on disk."""
    for index, token in enumerate(tokens):
        flag = _OUTPUT_FLAG.match(token)
        if not flag:
            continue
        glued = flag.group("glued")
        if glued == "-":
            return True, "-"  # `-O-` writes to stdout
        if glued.startswith("="):
            return True, glued[1:]
        following = tokens[index + 1 :]
        return True, following[0] if following else None
    return False, None


def _writes_a_file(redirect) -> bool:
    """True when a `file_redirect` node sends stdout into a real file.

    An FD dup (`>&2`, whose operator is `>&`) and an FD-qualified route
    (`2>/dev/null`, which carries a `file_descriptor`) move diagnostics, not the
    artifact, so neither counts as a saved download."""
    kinds = {child.type for child in redirect.children}
    if "file_descriptor" in kinds or not kinds & _WRITE_OPERATORS:
        return False
    return any(
        unquote(node_text(child)) not in _NULL_TARGETS
        for child in redirect.children
        if child.type in ARGUMENT_TYPES
    )


def _redirects_to_file(command) -> bool:
    """True when a redirection covering COMMAND writes its stdout into a real file.

    Redirections belong to the enclosing `redirected_statement`, never to the
    command's argument list — which is why a `>&2` cannot be read as an output
    file here. A group that redirects as a whole (`{ curl …; } > f`) covers the
    commands inside it, so the search climbs the grouping nodes."""
    node = command
    while node.parent is not None:
        parent = node.parent
        if parent.type == "redirected_statement":
            return any(
                _writes_a_file(child)
                for child in parent.children
                if child.type == "file_redirect"
            )
        if parent.type not in _REDIRECT_WRAPPERS:
            return False
        node = parent
    return False


def _stage_command(stage):
    """The command a `pipeline` child runs, or None for the `|` tokens between
    them. A stage carrying its own redirection is a `redirected_statement`
    wrapping the command."""
    if stage.type == "command":
        return stage
    if stage.type == "redirected_statement":
        return next((c for c in stage.children if c.type == "command"), None)
    return None


def _piped_into_shell(command) -> bool:
    """True when COMMAND's output is piped into an interpreter that runs it.

    The interpreter has to be a real downstream STAGE of the `pipeline` — so a
    `|` written inside a quoted string separates nothing, `ssh` is a different
    token than `sh`, and a shell named deep inside a later stage's argument
    (`| tee "$(bash -c name)"`) is not the thing being piped to."""
    stage = command
    while stage.parent is not None and stage.parent.type != "pipeline":
        stage = stage.parent
    pipeline = stage.parent
    if pipeline is None:
        return False
    downstream = [
        _stage_command(child)
        for child in pipeline.children
        if child.start_byte >= stage.end_byte
    ]
    return any(_names(_tokens(sink)) & _SHELLS for sink in downstream if sink)


def _names_a_downloader(tokens: list[str]) -> bool:
    return bool(_names(tokens) & _DOWNLOADERS)


def _executes_an_inline_fetch(command, tokens: list[str]) -> bool:
    """True when COMMAND is an interpreter running bytes fetched inline —
    ``bash -c "$(curl …)"``, ``bash <(curl …)``, ``eval "$(curl …)"``.

    The fetch has to sit inside the substitution the interpreter consumes, so a
    `bash -c "$(build_cfg)"` sharing a line with an unrelated (already verified)
    curl is not swept in."""
    names = _names(tokens)
    if not (names & _SHELLS or _EVAL in names):
        return False
    return any(
        _names_a_downloader(_tokens(fetch))
        for subst in iter_nodes(command, "command_substitution", "process_substitution")
        for fetch in iter_nodes(subst, "command")
    )


def _adds_from_url(tokens: list[str]) -> bool:
    """True for a Dockerfile `ADD <url> <dest>`, which writes the remote bytes
    straight into the image."""
    return bool(
        tokens
        and tokens[0].upper() == _ADD
        and any(_HTTP_URL.search(token) for token in tokens[1:])
    )


def _is_download(command, tokens: list[str]) -> bool:
    """True when COMMAND fetches remote bytes that then get saved or executed.

    Executing the fetched bytes (piped into a shell, or run from a `$(…)`/`<(…)`
    substitution) counts regardless of any stdout sink, and a redirect into a real
    file saves them regardless of one (`wget -qO- url > tool` writes `tool`) — so
    both are decided before the `-O-`/`-o -` sinks are excused."""
    if _executes_an_inline_fetch(command, tokens) or _adds_from_url(tokens):
        return True
    if not _names_a_downloader(tokens):
        return False
    if _piped_into_shell(command) or _redirects_to_file(command):
        return True
    flagged, target = _output_target(tokens)
    if flagged:
        return target not in _NULL_TARGETS
    # wget (unlike curl, which defaults to stdout) writes to disk by default, so a
    # bare `wget <url>` with no output flag or redirect is still an artifact.
    return _WGET in _names(tokens)


def _verifies(tokens: list[str]) -> bool:
    """True when TOKENS check fetched bytes against a digest or a signature."""
    names = _names(tokens)
    return bool(
        names & _VERIFY_COMMANDS
        or ("cosign" in names and "verify" in tokens)
        or ("gpg" in names and "--verify" in tokens)
        or any(token.startswith(_CHECKSUM_FLAG) for token in tokens)
    )


def _executed_strings(tokens: list[str], args: list) -> list:
    """String arguments whose contents something RUNS, so they hold commands.

    A shell's `-c` script, `eval`'s arguments and `ssh`'s remote command are code;
    an interpreter reached through a wrapper (`xargs … sh -c '…'`) is covered
    because the shell is found at any token position. Every other string is text a
    command prints or passes on."""
    quoted = [node for node in args if node.type in ("string", "raw_string")]
    if not (quoted and tokens):
        return []
    head = tokens[0].rsplit("/", 1)[-1]
    if head in (_EVAL, _SSH):
        return quoted[-1:] if head == _SSH else quoted
    for index, token in enumerate(tokens):
        if token.rsplit("/", 1)[-1] in _SHELLS and "-c" in tokens[index + 1 :]:
            return quoted[:1]
    return []


def _executed_scripts(
    command, tokens: list[str], args: list
) -> list[tuple[object, str]]:
    """(node, script) for every body COMMAND runs as shell code of its own.

    Two shapes carry a script the grammar does not parse as commands on its own:
    a quoted argument something executes (see `_executed_strings`), and a heredoc
    fed to an interpreter (`bash <<'EOF' … EOF`) — whose body IS the script, unlike
    a heredoc written to a file (`cat <<'EOF' > doc.txt`), which is data."""
    scripts = [
        (node, unquote(node_text(node))) for node in _executed_strings(tokens, args)
    ]
    # Only the command's OWN redirections can carry its heredoc: a `heredoc_body`
    # found anywhere else belongs to another command's redirect.
    statement = command.parent
    if _names(tokens) & _SHELLS and statement is not None:
        scripts += [
            (node, node_text(node))
            for redirect in statement.children
            if redirect.type == "heredoc_redirect"
            for node in iter_nodes(redirect, "heredoc_body")
        ]
    return scripts


def _findings(root) -> tuple[list[tuple[int, int]], set[int]]:
    """((first line, last line) of every download, lines carrying a verification)
    under ROOT, all 1-based.

    Recurses into the bodies something executes, so a download inside
    `bash -c "…"` or a `bash <<'EOF'` heredoc is judged as the command it becomes;
    its lines are reported relative to the enclosing script."""
    downloads: list[tuple[int, int]] = []
    verified: set[int] = set()
    for command in iter_nodes(root, "command"):
        args = command_arguments(command)
        tokens = [unquote(node_text(node)) for node in args]
        for node, script in _executed_scripts(command, tokens, args):
            offset = node.start_point[0]
            inner_downloads, inner_verified = _findings(parse(script))
            downloads += [
                (offset + first, offset + last) for first, last in inner_downloads
            ]
            verified |= {offset + line for line in inner_verified}
        if _verifies(tokens):
            verified.add(command.start_point[0] + 1)
        if _is_download(command, tokens):
            downloads.append((command.start_point[0] + 1, command.end_point[0] + 1))
    return downloads, verified


def violations(text: str) -> list[int]:
    """1-based line numbers of artifact downloads with no nearby verification.

    Detection walks the bash grammar, so a `\\`-continued download is ONE node
    (reported at its first line), a download written in a comment is a `comment`
    node rather than a command, and one written inside a message string or a
    heredoc body is text the grammar yields no command from. A verification is
    likewise a real command, so a ``# TODO: verify with sha256sum`` cannot satisfy
    the gate. The raw physical lines are kept for the ``# pin-exempt:`` opt-out,
    which by definition lives in a comment (accepted on any physical line of the
    flagged command, or the line directly above it)."""
    raw = text.splitlines()
    downloads, verified = _findings(parse(text))
    # One finding per line, carrying the WIDEST span that starts there: two
    # downloads can begin on the same row (`curl -o a x; curl -o b y`, or a
    # `bash -c` string whose fetch sits on the line that opened it), and a line
    # reported twice would be a duplicate finding — plus the wider span gives the
    # `pin-exempt` lookup every physical line the reader would put it on.
    widest: dict[int, int] = {}
    for start, end in downloads:
        widest[start] = max(widest.get(start, end), end)
    starts = sorted(widest)
    hits = []
    for index, start in enumerate(starts):
        if any(
            annotated(physical, OPT_OUT) for physical in raw[start - 1 : widest[start]]
        ) or (start >= 2 and annotated(raw[start - 2], OPT_OUT)):
            continue
        # Each download must carry its OWN check: the scan reaches _WINDOW lines
        # past it and stops before the next download, so one checksum can't cover
        # two fetches.
        limit = start + _WINDOW
        if index + 1 < len(starts):
            limit = min(limit, starts[index + 1] - 1)
        if not any(start <= line <= limit for line in verified):
            hits.append(start)
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for arg in argv:
        try:
            hits = violations(Path(arg).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails the check LOUDLY (the
            # same posture as check_untrusted_exec): skipping it would false-green
            # exactly the input an adversary controls.
            print(f"{arg}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno in hits:
            print(f"{arg}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

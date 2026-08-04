#!/usr/bin/env python3
"""Ban a fail-open idiom: a structured-data producer feeding a shell loop through a
construct that DISCARDS the producer's exit status.

Both ``done < <(PRODUCER …)`` (process substitution) and ``PRODUCER … | while read``
(pipeline) throw away PRODUCER's exit code: the ``while``/``mapfile``/``read`` consumer
reports on the redirect or the pipe, not on the command that filled it. So when the
producer errors — malformed input, a renamed key that makes ``jq`` emit ``null`` and
exit 5, an unreadable file — it prints nothing, the loop iterates ZERO times, and any
guard/allowlist the loop was building silently no-ops while the surrounding function
still returns 0.

Correct pattern — capture then iterate, so the producer's failure is observed:

    out="$(jq -r '.providers[]' "$file")" || die "jq failed"
    while IFS= read -r d; do …; done <<<"$out"

The script is parsed with the real bash grammar (``_bash_ast``), so both constructs
are node shapes rather than text: the redirect form is a ``process_substitution``
sitting inside an INPUT ``file_redirect`` — which is what tells it apart from
``diff <(jq a) <(jq b)``, where the same substitutions are the command's arguments —
and the pipe form is a ``pipeline`` whose producer stage is followed by the consumer.
No regex has to assert where a command begins, and none has to stay inside one
pipeline segment by excluding ``|``/``;``/``&`` characters: the ``pipeline`` node IS
that segment. A construct quoted inside a printed message is ``string`` content
holding no commands, and one inside a heredoc body is data, so neither is reachable.

PRODUCER SET (deliberately small): ``jq`` and ``yq`` only. These are structured-data
extractors whose nonzero exit means "your query/data was wrong" — a fail-CLOSED signal
that must not be swallowed. ``grep``/``cat``/``find``/``sed`` are intentionally NOT in
the set: their nonzero exits (grep-no-match, find-permission) are routinely expected and
best-effort, so flagging ``done < <(grep …)`` would be noise. Widen the set only for a
producer whose empty-output-on-error is a genuine fail-open.

A site that must keep the construct opts out with a ``# allow-substitution-exit:
<reason>`` on any line of the flagged construct or the line above it — the reason is
REQUIRED; a bare annotation with no reason does not suppress.

Invoked by pre-commit with the staged shell files as arguments.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_words,
    iter_nodes,
    parse,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    run_file_cli,
    run_line_checks,
)

OPT_OUT = "allow-substitution-exit"

MESSAGE = (
    "jq/yq exit status is discarded by this construct — capture then iterate "
    '(`out="$(jq …)" || die …; while read; do …; done <<<"$out"`) so the '
    "producer's failure is not silently swallowed, or annotate "
    f"`# {OPT_OUT}: <reason>`"
)

# The curated producer set (see module docstring for why these two and not grep/cat).
_PRODUCERS = frozenset({"jq", "yq"})

# Prefixes that run the following word as the command, so the producer behind one
# is still the producer whose status is swallowed.
_PREFIXES = frozenset({"command", "env", "exec", "nice", "sudo", "doas"})

# The consumers that read the producer's output while reporting their own status:
# a `while`/`until` loop, or a bare read/mapfile stage.
_CONSUMER_STATEMENTS = frozenset({"while_statement", "until_statement"})
_CONSUMER_COMMANDS = frozenset({"read", "mapfile", "readarray"})

# The operator tokens the grammar puts between a `pipeline`'s stages; dropping
# them leaves the stages themselves.
_OPERATORS = frozenset({"||", "&&", "|", "|&", ";", ";;", "&", "\n"})

_ALLOW_WITH_REASON = annotation_re(OPT_OUT)


def _program(command) -> str:
    """The program COMMAND runs, as a bare name: wrapper prefixes stripped
    (`command jq …`) and the leading path dropped (`/usr/bin/jq` → `jq`)."""
    words = command_words(command)
    while len(words) > 1 and words[0].rsplit("/", 1)[-1] in _PREFIXES:
        words = words[1:]
    return words[0].rsplit("/", 1)[-1] if words else ""


def _operands(node) -> list:
    """NODE's branches, with the operator tokens between them dropped."""
    return [child for child in node.children if child.type not in _OPERATORS]


def _producer(node):
    """The producer command NODE runs, or None. A `pipeline`'s first stage counts:
    `jq … | head` still has jq's status discarded by the enclosing construct."""
    while node.type == "pipeline" and _operands(node):
        node = _operands(node)[0]
    if node.type != "command":
        return None
    return node if _program(node) in _PRODUCERS else None


def _is_consumer(node) -> bool:
    """True when NODE is a loop or read stage that reports its own status rather
    than the producer's."""
    if node.type in _CONSUMER_STATEMENTS:
        return True
    return node.type == "command" and _program(node) in _CONSUMER_COMMANDS


def _redirect_producers(root) -> list:
    """Every producer feeding a consumer through an INPUT redirect from a process
    substitution (`done < <(jq …)`).

    The `file_redirect` parent is the whole distinction: the same
    `<(jq …)` written as a command ARGUMENT (`diff <(jq a) <(jq b)`) compares two
    outputs and swallows nothing, and it is a direct child of the `command` there.
    """
    found = []
    for redirect in iter_nodes(root, "file_redirect"):
        if not any(child.type == "<" for child in redirect.children):
            continue
        for substitution in redirect.children:
            if substitution.type != "process_substitution":
                continue
            producer = next(
                (p for child in substitution.children if (p := _producer(child))), None
            )
            if producer is not None:
                found.append(producer)
    return found


def _pipe_producers(root) -> list:
    """Every producer piped DIRECTLY into a consumer stage (`jq … | while read`).

    The stages come from the `pipeline` node, so "did some other command feed the
    loop?" is answered by which stage precedes the consumer — never by a character
    class excluding `|`/`;`/`&` to stay inside one segment.
    """
    found = []
    for pipeline in iter_nodes(root, "pipeline"):
        stages = _operands(pipeline)
        for producer, consumer in zip(stages, stages[1:]):
            if _is_consumer(consumer) and (found_producer := _producer(producer)):
                found.append(found_producer)
    return found


def _suppressed(lines: list[str], start: int, end: int) -> bool:
    """True when a reason-bearing opt-out sits on a line of the flagged construct
    (1-based ``start``..``end``) or the line directly above it."""
    span = lines[max(start - 2, 0) : end]
    return any(_ALLOW_WITH_REASON.search(line) for line in span)


def violations(text: str) -> list[int]:
    """1-based line numbers in TEXT where a structured-data producer feeds a loop
    through an exit-swallowing construct without a reason-bearing annotation.

    The finding is anchored on the PRODUCER, which is the command whose status is
    lost and the line a reader writes the annotation on."""
    lines = text.split("\n")
    root = parse(text)
    hits = set()
    for producer in _redirect_producers(root) + _pipe_producers(root):
        start = producer.start_point[0] + 1
        if not _suppressed(lines, start, producer.end_point[0] + 1):
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
    raise SystemExit(run_file_cli(main))

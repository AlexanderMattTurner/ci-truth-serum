#!/usr/bin/env python3
"""Ban the ``gh api --slurp`` flag combinations that can never succeed.

``gh`` rejects two ``--slurp`` combinations while PARSING ITS ARGUMENTS, before a
single request goes out:

* ``--slurp`` with ``--jq``/``--template`` (either spelling, long or short) —
  "the `--slurp` option is not supported with `--jq` or `--template`".
* ``--slurp`` without ``--paginate`` — "`--paginate` required when passing
  `--slurp`".

Verified against gh 2.86.0 with a token set and ``--hostname 127.0.0.1``: both
shapes print the message above and exit 1, while the valid-flag controls
(``--paginate --slurp``, plain ``--jq``) instead report a dial error against
127.0.0.1 — so the rejection provably precedes the HTTP request.

Because these are hard mutual/required-with exclusions, such a call exits
non-zero on EVERY run: it is not a flaky call, it is a call that has never
worked. That makes it worse than a loud bug — a site that swallows the failure
(``|| true``, a ``2>/dev/null`` capture feeding a ``// empty`` default) reads as
a permanent vacuous green, and one that does not reds a scheduled job 100% of
the time.

The remedy is to capture ``gh api … --paginate --slurp`` output and apply the
filter in a SEPARATE ``jq`` invocation over the captured document — which is
also what the surrounding code usually wants, since ``--slurp`` yields one array
element per page and the filter must flatten (``.[][]``) before it selects.

Scanning runs on the REAL bash grammar (``_cts_bash_ast``), walking ``command``
nodes: the question "which flags belong to THIS ``gh api``?" is a structural one,
and the grammar answers it exactly. One ``command`` node spans a
backslash-continued invocation whole, so a ``--jq`` on a later line is judged as
the flag it is; a downstream ``| jq -r …`` or ``| column -t`` is a separate
``command`` in the ``pipeline``, so its short flags can never be read as gh's; a
``|`` or ``;`` inside an argument value is part of a ``string`` and bounds
nothing; and a ``heredoc_body`` or a quoted message is data rather than a
command, so the idiom written into a generated document or an error string is
not a call at all. Flags are read off the command's own argument children, so a
``file_redirect`` (``>/tmp/x.json``) is never mistaken for one.

A call reached through a variable (``GH=gh`` … ``$GH api --slurp --jq .x``) is
judged as the call it is, but only where the source PROVES the command word:
the name must resolve literally to the ``gh`` binary at every assignment in the
file. One assignment the source does not fix drops the name entirely.

A call that must keep the combination — there is no legitimate one, since gh
refuses it — opts out with a same-line or immediately-preceding-line
``# allow-gh-slurp-jq: <reason>``. The reason is REQUIRED, matching the sibling
shell lints.

Invoked by pre-commit with the staged shell paths as arguments; a
``.github/{workflows,actions}`` YAML path among them has each inline ``run:``
block scanned instead (reported at the step's line).
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    MESSAGE_PREFIX,
    annotation_re,
    run_file_cli,
)

OPT_OUT = "allow-gh-slurp-jq"

MESSAGE = (
    "`gh api --slurp` is rejected at argument validation with `--jq`/"
    '`--template` ("the `--slurp` option is not supported with `--jq` or '
    '`--template`") and requires `--paginate` ("`--paginate` required when '
    'passing `--slurp`"), so this call can NEVER succeed — capture '
    "`gh api … --paginate --slurp` output and apply the filter in a SEPARATE "
    f"`jq` invocation, or annotate `# {OPT_OUT}: <reason>`."
)

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

# The two words that open the subcommand under scrutiny. Searched at any token
# position, so a wrapper in front (`retry_stdout gh api …`) needs no enumeration.
_GH_API = ("gh", "api")

_SLURP = "--slurp"
_PAGINATE = "--paginate"
# gh api's jq/template filters: the long spellings, plus any short-flag cluster
# whose LAST letter is `q` or `t` — cobra lets shorts bundle (`-iq '.x'`), the
# value-taking short must end its cluster, and no other `gh api` short flag uses
# either letter, so the cluster form is unambiguous.
_LONG_FILTERS = frozenset({"--jq", "--template"})
_SHORT_FILTER = re.compile(r"-(?!-)[A-Za-z]*[qt]")

_ALLOW = annotation_re(OPT_OUT)

# A bare read of one variable — `$GH` or `${GH}` — and nothing else. A token that
# only CONTAINS an expansion is a different word at run time, so it never matches.
_EXPANSION = re.compile(r"\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?")


def _literal_value(node) -> str | None:
    """An assignment value's literal text, or None when it carries an expansion,
    an escape or any other shape whose run-time value the source does not fix."""
    raw = node_text(node)
    if node.type == "word":
        return None if "$" in raw or "\\" in raw else raw
    if node.type == "raw_string":
        return raw[1:-1]
    if node.type == "string":
        content = [child for child in node.children if child.type != '"']
        if len(content) == 1 and content[0].type == "string_content":
            body = node_text(content[0])
            return None if "\\" in body else body
        return "" if not content else None
    return None


def _gh_aliases(root) -> set[str]:
    """Variable names that hold the `gh` binary — `GH=gh`, `GH=/usr/bin/gh`.

    A name is an alias only when EVERY assignment to it in the file resolves
    literally to `gh`; one assignment the source does not fix (a `$(…)`, a
    parameter default, a loop variable) drops the name, so a `$X api --slurp`
    whose `X` merely might be gh is never rewritten. That is what keeps the recall
    gain free of false positives: the rewrite happens only where the source itself
    proves the command word."""
    all_gh: dict[str, bool] = {}
    for assign in iter_nodes(root, "variable_assignment"):
        name = assign.child_by_field_name("name")
        value = assign.child_by_field_name("value")
        if name is None:
            continue
        literal = None if value is None else _literal_value(value)
        is_gh = literal is not None and literal.rsplit("/", 1)[-1] == "gh"
        key = node_text(name)
        all_gh[key] = all_gh.get(key, True) and is_gh
    return {name for name, is_gh in all_gh.items() if is_gh}


def _resolve(token: str, aliases: set[str]) -> str:
    """TOKEN with a read of a `gh` alias (`$GH`, `${GH}`) replaced by `gh`, so a
    call reached through a variable is judged as the call it is."""
    match = _EXPANSION.fullmatch(token)
    return "gh" if match and match.group("name") in aliases else token


def _flag_names(tokens: list[str]) -> set[str]:
    """The flag names among TOKENS, with any `--flag=value` value dropped. A
    non-flag token is a value or an endpoint and names no flag."""
    return {
        token.split("=", 1)[0]
        for token in tokens
        if token.startswith("-") and token not in ("-", "--")
    }


def _is_filter(flag: str) -> bool:
    """True when FLAG is one of gh api's jq/template filters."""
    return flag in _LONG_FILTERS or bool(_SHORT_FILTER.fullmatch(flag))


def _rejects(tokens: list[str]) -> bool:
    """True when TOKENS spell a ``gh api`` call carrying a ``--slurp``
    combination gh refuses at argument validation. The word pair is searched at
    every position, so a wrapper (`retry_stdout gh api …`) is transparent."""
    for index in range(len(tokens) - 1):
        if (tokens[index], tokens[index + 1]) != _GH_API:
            continue
        flags = _flag_names(tokens[index + 2 :])
        if _SLURP not in flags:
            continue
        if any(_is_filter(flag) for flag in flags) or _PAGINATE not in flags:
            return True
    return False


def violations(text: str) -> list[int]:
    """1-based line numbers of ``gh api`` calls carrying a ``--slurp``
    combination gh rejects outright — ``--slurp`` plus ``--jq``/``--template``,
    or ``--slurp`` without ``--paginate`` — absent a reason-bearing
    ``# allow-gh-slurp-jq:`` annotation.

    Detection walks the bash grammar, so a `\\`-continued call is ONE node
    (reported at its first line), a call written in a comment is a `comment`
    node, and one written inside a message string or a heredoc body is that
    string or body rather than a command. The raw physical lines are kept for the
    opt-out, which by definition lives in a comment (accepted on any physical
    line of the flagged command, or the line directly above it)."""
    raw = text.splitlines()
    # One finding per line, carrying the WIDEST span that starts there: two calls
    # can begin on the same row (`gh api "$A" --slurp ; gh api "$B" --slurp`), a
    # line reported twice would be a duplicate finding, and the wider span gives
    # the annotation lookup every physical line the reader would put it on.
    widest: dict[int, int] = {}
    root = parse(text)
    aliases = _gh_aliases(root)
    for command in iter_nodes(root, "command"):
        tokens = [
            _resolve(unquote(node_text(node)), aliases)
            for node in command_arguments(command)
        ]
        if tokens and MESSAGE_PREFIX.match(tokens[0]):
            continue  # a command that only prints; its arguments are text
        if not _rejects(tokens):
            continue
        start, end = command.start_point[0] + 1, command.end_point[0] + 1
        widest[start] = max(widest.get(start, end), end)
    hits = []
    for start, end in sorted(widest.items()):
        if any(_ALLOW.search(physical) for physical in raw[start - 1 : end]) or (
            start >= 2 and _ALLOW.search(raw[start - 2])
        ):
            continue
        hits.append(start)
    return hits


def _run_scripts(path: Path) -> list[tuple[int, str]]:
    """(step line, script) for every inline `run:` block in a workflow or
    composite-action file. An unparseable file yields no scripts — YAML syntax is
    actionlint's job, and the shell files this lint owns are its argv."""
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


def main(argv: list[str]) -> int:
    status = 0
    for arg in argv:
        path = Path(arg)
        try:
            if _WORKFLOW_PATH.search(arg.replace("\\", "/")):
                hits = [
                    step_line
                    for step_line, script in _run_scripts(path)
                    if violations(script)
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
        for lineno in hits:
            print(f"{arg}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

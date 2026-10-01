#!/usr/bin/env python3
"""
Make every job a required check depends on run on every event that check gates.

GitHub counts a ``skipped`` check run as a satisfied required check. A job in a
required check's ``needs`` closure whose ``if:`` shuts it off on some trigger
event therefore lets the check report green on that event with none of its work
done. The shape has two live instances: a required job whose own ``if:``
excludes activity types its workflow fires on, and a ``decide`` job that skips
in the merge queue while its ``always()`` reporter still reports there — the
reporter reads the empty gate outputs as "nothing relevant changed" and passes
the batch unexamined.

For each job that carries ``# required-check: true`` (the same marker
``check_required_reporter`` demands and ``sync_required_checks`` applies), this
lint walks the job's transitive ``needs`` and evaluates each closure job's
``if:`` against every (event, activity type) the workflow declares. Only the
three events GitHub consults when it gates a merge are checked —
``pull_request``, ``pull_request_target``, ``merge_group`` — because a
``push``/``schedule``/``workflow_dispatch`` run's conclusion satisfies no
required check, so a skip there cannot fail open.

Evaluation is three-valued. The ``if:`` expression is parsed with a
recursive-descent parser over GitHub's documented expression grammar (no
published Python parser exists for it). Only the event facts are bound:
``github.event_name``, ``github.event.action``, and the ABSENT
``github.event.pull_request`` of a merge-queue run, which GitHub reads as null.
Every other context is unknown. Only a DEFINITELY-false verdict fires: a fork
guard, a ``needs.decide.outputs.*`` gate, or a title-keyword condition
evaluates to unknown and passes, which is what keeps the false-positive rate at
zero on real trees.

A second rule covers a check that never runs in the merge queue at all. GitHub
requires the same checks of a queue batch as of a pull request. So a marked job
in a workflow that fires on a pull request but not on ``merge_group`` leaves the
queue waiting for a context that never arrives. A lint cannot read the ruleset,
so it reads the queue from the tree. The repo uses one when another workflow
fires on both a pull request event and ``merge_group``. A workflow that fires on
``merge_group`` alone can be a leg written before the queue is turned on, so it
proves nothing.

Opt out per job with ``# event-scoped-ok: <reason>`` on or above the job when
the skip is deliberate and the reporter below it is honest about it.
"""

import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _job_blocks,
    _marked_jobs,
    annotated_near,
    declared_events,
    unwrap_expression,
    workflow_triggers,
    yaml_comment_view,
)
from _cts_linecheck import WORKFLOW_GLOBS  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "event-scoped-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# The events whose runs GitHub consults when it gates a merge. A skip on any
# other event cannot satisfy (or fail) a required check, so exclusion there is
# not a defect. workflow_call is unanalyzable per-file besides: its event_name
# is the calling workflow's.
GATING_EVENTS = ("pull_request", "pull_request_target", "merge_group")
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})

# Activity types GitHub fires when a pull_request-family trigger declares none.
DEFAULT_TYPES = {
    "pull_request": ("opened", "synchronize", "reopened"),
    "pull_request_target": ("opened", "synchronize", "reopened"),
}

# One token of GitHub's expression grammar. A path segment admits `.` parts,
# `*` object filters, and `[...]` index steps so real contexts
# (`github.event.commits[0].message`, `needs.*.result`) tokenize instead of
# failing the whole expression.
_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')"
    r"|(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<op>\(|\)|,|!=|==|<=|>=|<|>|!|&&|\|\|)"
    r"|(?P<path>[A-Za-z_][\w-]*(?:\.[\w*-]+|\[[^\]]*\])*))"
)

# A sentinel no expression value can equal, so the string literal 'unknown'
# stays an ordinary string.
_UNKNOWN = object()

# GitHub's literal keywords. The tokenizer reads them as context paths.
_KEYWORDS = {"true": True, "false": False, "null": None}

# A legal JSON number, the only string form GitHub casts to a number.
_JSON_NUMBER = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")

# The events whose payload carries no pull request object. GitHub reads every
# path under the absent object as null.
_NO_PULL_REQUEST = frozenset({"merge_group"})

# The activity type GitHub sends with every merge_group event.
_MERGE_GROUP_ACTION = "checks_requested"

# The functions truth_of reads, each over two string casts.
_STRING_CALLS = ("contains", "startswith", "endswith")


class ExpressionError(ValueError):
    """The ``if:`` text is not a GitHub expression this parser recognizes."""


def _tokenize(expr: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    while pos < len(expr):
        match = _TOKEN.match(expr, pos)
        if not match or match.end() == pos:
            if expr[pos:].strip():
                raise ExpressionError(f"cannot tokenize {expr[pos:]!r}")
            break
        pos = match.end()
        kind = match.lastgroup or ""
        out.append((kind, match.group(kind)))
    return out


class _Parser:
    """Recursive descent over GitHub's expression grammar, precedence low-to-
    high: ``||``, ``&&``, comparison, ``!``, atom. Produces a tuple tree
    ("or"/"and"/"not"/"cmp"/"call"/"path"/"lit", ...)."""

    def __init__(self, expr: str):
        self.toks = _tokenize(expr)
        self.pos = 0

    def _peek(self) -> str | None:
        return self.toks[self.pos][1] if self.pos < len(self.toks) else None

    def _take(self, expected: str | None = None) -> tuple[str, str]:
        if self.pos >= len(self.toks):
            raise ExpressionError("unexpected end of expression")
        kind, tok = self.toks[self.pos]
        if expected is not None and tok != expected:
            raise ExpressionError(f"expected {expected!r}, got {tok!r}")
        self.pos += 1
        return kind, tok

    def parse(self) -> tuple:
        node = self._or()
        if self.pos != len(self.toks):
            raise ExpressionError(f"trailing tokens {self.toks[self.pos :]!r}")
        return node

    def _or(self) -> tuple:
        node = self._and()
        while self._peek() == "||":
            self._take()
            node = ("or", node, self._and())
        return node

    def _and(self) -> tuple:
        node = self._cmp()
        while self._peek() == "&&":
            self._take()
            node = ("and", node, self._cmp())
        return node

    def _cmp(self) -> tuple:
        left = self._unary()
        if self._peek() in ("==", "!=", "<", "<=", ">", ">="):
            _, op = self._take()
            return ("cmp", op, left, self._unary())
        return left

    def _unary(self) -> tuple:
        if self._peek() == "!":
            self._take()
            return ("not", self._unary())
        return self._atom()

    def _atom(self) -> tuple:
        if self.pos >= len(self.toks):
            raise ExpressionError("unexpected end of expression")
        kind, tok = self.toks[self.pos]
        if tok == "(":
            self._take()
            node = self._or()
            self._take(")")
            return node
        if kind == "str":
            self._take()
            return ("lit", tok[1:-1].replace("''", "'"))
        if kind == "num":
            self._take()
            return ("lit", float(tok))
        if kind == "path":
            self._take()
            if self._peek() != "(" and tok.lower() in _KEYWORDS:
                return ("lit", _KEYWORDS[tok.lower()])
            if self._peek() == "(":
                self._take()
                args = []
                if self._peek() != ")":
                    args.append(self._or())
                    while self._peek() == ",":
                        self._take()
                        args.append(self._or())
                self._take(")")
                return ("call", tok.lower(), args)
            return ("path", tok)
        raise ExpressionError(f"unexpected token {tok!r}")


def event_env(event: str, action: str | None = None) -> dict:
    """The context values one gating event fixes, for ``truth_of``.

    A merge-queue run carries no pull request, so every path under
    ``github.event.pull_request`` reads as null there. Its activity type is
    always ``checks_requested``.
    """
    env: dict = {"github.event_name": event}
    if event == "merge_group" and action is None:
        action = _MERGE_GROUP_ACTION
    if action is not None:
        env["github.event.action"] = action
    if event in _NO_PULL_REQUEST:
        env["github.event.pull_request"] = None
    return env


def _lookup(path: str, env: dict) -> object:
    """PATH's value under ENV. A path below a null object is itself null."""
    key = path.lower()
    if key in env:
        return env[key]
    for bound, value in env.items():
        if value is None and key.startswith((f"{bound}.", f"{bound}[")):
            return None
    return _UNKNOWN


def _value_of(node: tuple, env: dict) -> object:
    """NODE's value where ENV determines it, else the _UNKNOWN sentinel."""
    if node[0] == "lit":
        return node[1]
    if node[0] == "path":
        return _lookup(node[1], env)
    if node[0] == "call" and node[1] == "fromjson" and len(node[2]) == 1:
        inner = _value_of(node[2][0], env)
        if isinstance(inner, str):
            try:
                return json.loads(inner)
            except ValueError:
                return _UNKNOWN
    return _UNKNOWN


def _to_number(value: object) -> float:
    """VALUE cast the way GitHub casts before it compares two types."""
    if value is None:
        return 0.0
    if isinstance(value, (bool, int, float)):
        return float(value)
    if isinstance(value, str):
        if not value:
            return 0.0
        if _JSON_NUMBER.fullmatch(value):
            return float(value)
    return float("nan")


def _to_string(value: object) -> str | None:
    """VALUE cast the way GitHub casts a function's string argument. An array
    or an object gets None, because this reader does not model that cast."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value
    return None


def _loose_equal(left: object, right: object) -> bool:
    """GitHub's ``==``. Two strings compare without regard to case. Two values
    of different types compare as numbers, so ``null == ''`` is true. An array
    or an object equals only itself."""
    if isinstance(left, str) and isinstance(right, str):
        return left.casefold() == right.casefold()
    if isinstance(left, (list, dict)) or isinstance(right, (list, dict)):
        return left is right
    if type(left) is type(right):
        return left == right
    return _to_number(left) == _to_number(right)


def _truthy(value: object) -> bool:
    """GitHub's truthiness: false, null, 0, NaN and '' are false."""
    if isinstance(value, (list, dict)):
        return True
    if isinstance(value, str):
        return value != ""
    number = _to_number(value)
    return number == number and number != 0


def _string_call(name: str, args: list, env: dict) -> object:
    """startsWith / endsWith / contains over string casts, without case."""
    hay, needle = (_value_of(arg, env) for arg in args)
    if hay is _UNKNOWN or needle is _UNKNOWN:
        return _UNKNOWN
    if name == "contains" and isinstance(hay, list):
        return any(_loose_equal(item, needle) for item in hay)
    hay_text, needle_text = _to_string(hay), _to_string(needle)
    if hay_text is None or needle_text is None:
        return _UNKNOWN
    hay_text, needle_text = hay_text.casefold(), needle_text.casefold()
    if name == "startswith":
        return hay_text.startswith(needle_text)
    if name == "endswith":
        return hay_text.endswith(needle_text)
    return needle_text in hay_text


def truth_of(node: tuple, env: dict) -> object:
    """True / False / _UNKNOWN for NODE as a job condition under ENV.

    Unknown is contagious except where logic decides without it: False wins an
    ``and``, True wins an ``or``. Status functions (always()/success()/…) and
    every context ENV does not bind are unknown, so only an exclusion PROVABLE
    from the event facts alone ever reaches a False verdict.
    """
    if node[0] == "or":
        left, right = truth_of(node[1], env), truth_of(node[2], env)
        if left is True or right is True:
            return True
        if left is False and right is False:
            return False
        return _UNKNOWN
    if node[0] == "and":
        left, right = truth_of(node[1], env), truth_of(node[2], env)
        if left is False or right is False:
            return False
        if left is True and right is True:
            return True
        return _UNKNOWN
    if node[0] == "not":
        inner = truth_of(node[1], env)
        return _UNKNOWN if inner is _UNKNOWN else not inner
    if node[0] == "cmp" and node[1] in ("==", "!="):
        left, right = _value_of(node[2], env), _value_of(node[3], env)
        if left is _UNKNOWN or right is _UNKNOWN:
            return _UNKNOWN
        equal = _loose_equal(left, right)
        return equal if node[1] == "==" else not equal
    if node[0] == "call" and node[1] in _STRING_CALLS and len(node[2]) == 2:
        return _string_call(node[1], node[2], env)
    if node[0] in ("lit", "path"):
        value = _value_of(node, env)
        return _UNKNOWN if value is _UNKNOWN else _truthy(value)
    return _UNKNOWN


def gating_pairs(triggers: object) -> list[tuple[str, str | None]]:
    """Every (event, activity type) pair the workflow fires on among the
    merge-gating events; the type is None for merge_group (it has none)."""
    if isinstance(triggers, str):
        declared: dict = {triggers: None}
    elif isinstance(triggers, list):
        declared = {t: None for t in triggers if isinstance(t, str)}
    elif isinstance(triggers, dict):
        declared = {k: v for k, v in triggers.items() if isinstance(k, str)}
    else:
        return []
    pairs: list[tuple[str, str | None]] = []
    for event in GATING_EVENTS:
        if event not in declared:
            continue
        cfg = declared[event]
        types: tuple = DEFAULT_TYPES.get(event, ())
        if isinstance(cfg, dict) and isinstance(cfg.get("types"), list):
            types = tuple(cfg["types"])
        if types:
            pairs += [(event, str(t)) for t in types]
        else:
            pairs.append((event, None))
    return pairs


def needs_closure(jobs: dict, root: str) -> set[str]:
    """ROOT plus every job it transitively `needs`."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in seen or name not in jobs or not isinstance(jobs[name], dict):
            continue
        seen.add(name)
        needs = jobs[name].get("needs") or []
        stack += (
            [needs]
            if isinstance(needs, str)
            else [n for n in needs if isinstance(n, str)]
        )
    return seen


def _excluded_on(cond: str, pairs: list[tuple[str, str | None]]) -> list[str]:
    """The gating (event, type) labels on which COND is definitely false."""
    tree = _Parser(cond).parse()
    excluded = []
    for event, action in pairs:
        if truth_of(tree, event_env(event, action)) is False:
            excluded.append(event if action is None else f"{event}:{action}")
    return excluded


def gates_the_queue(path: Path) -> bool:
    """True when the workflow at PATH fires on a pull request event and on
    ``merge_group``.

    A file that does not parse answers False. Its own ``check_file`` run
    reports it, so the answer here only loses evidence of a queue.
    """
    try:
        doc = safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return False
    events = declared_events(doc)
    return "merge_group" in events and bool(events & PR_EVENTS)


def uses_merge_queue(paths: list[Path]) -> bool:
    """True when a workflow in PATHS gates both pull requests and the queue.

    The ruleset that turns the queue on lives outside the tree. A gate that
    already answers the queue's event is the tree's own record that a queue runs.
    """
    return any(gates_the_queue(path) for path in paths)


def _siblings(path: Path) -> list[Path]:
    """The workflow files in PATH's directory, PATH included."""
    return sorted(p for glob in WORKFLOW_GLOBS for p in path.parent.glob(glob))


def check_file(
    path: Path, merge_queue: bool | None = None
) -> list[tuple[int | None, str]]:
    """Return (line, message) for every closure job provably skipped on a
    gating event, and for every required job the merge queue never runs.

    MERGE_QUEUE says whether the repo uses a merge queue. None reads it from
    the workflow files beside PATH.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_required_reporter &c.). An ``if:`` this parser cannot
    read is reported the same way: an unreadable condition on a required check's
    dependency could hide exactly the exclusion this lint exists to see.
    """
    text = path.read_text(encoding="utf-8")
    try:
        doc = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify the "
                "event coverage of required-check dependencies — fix the syntax "
                "(or run actionlint) and re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []
    pairs = gating_pairs(workflow_triggers(doc))
    if not pairs:
        return []

    blocks = _job_blocks(text)
    lines = text.splitlines()
    comments = yaml_comment_view(text)
    required = _marked_jobs(blocks, jobs)

    violations: list[tuple[int | None, str]] = []
    events = {event for event, _ in pairs}
    if required and events & PR_EVENTS and "merge_group" not in events:
        if merge_queue is None:
            merge_queue = uses_merge_queue(_siblings(path))
        if merge_queue:
            for root in required:
                start, block = blocks.get(root, (1, ""))
                span_end = start + len(block.splitlines()) - 1
                if not annotated_near(
                    lines, start, OPT_OUT, span_end=span_end, comments=comments
                ):
                    violations.append((start, _no_queue_trigger(root)))

    judged: set[str] = set()
    for root in required:
        for name in sorted(needs_closure(jobs, root)):
            if name in judged:
                continue
            judged.add(name)
            cond = unwrap_expression(jobs[name].get("if", ""))
            if not cond:
                continue
            start, block = blocks.get(name, (1, ""))
            span_end = start + len(block.splitlines()) - 1
            # LINES shapes the window; COMMENTS says what each line holds, so a
            # `#` inside a quoted scalar in the job's block opts nobody out.
            if annotated_near(
                lines, start, OPT_OUT, span_end=span_end, comments=comments
            ):
                continue
            try:
                excluded = _excluded_on(cond, pairs)
            except ExpressionError as err:
                violations.append((start, _unreadable(name, cond, err)))
                continue
            if excluded:
                violations.append((start, _excluded(name, root, excluded, cond)))
    return violations


def _excluded(name: str, root: str, excluded: list[str], cond: str) -> str:
    return (
        f"job '{name}', which required check '{root}' depends on, is skipped on "
        f"{', '.join(excluded)} — its `if:` is {cond!r}, and GitHub counts the "
        "skip as a satisfied required check, so the check reports green there "
        "with none of this job's work done. Widen the `if:` to admit the event, "
        f"or annotate the job with '# {OPT_OUT}: <reason>' if the skip is "
        "deliberate and honestly reported."
    )


def _no_queue_trigger(root: str) -> str:
    return (
        f"required check '{root}' runs on pull requests, but its workflow does "
        "not fire on merge_group. Another pull request gate here does, so this "
        "repo uses a merge queue. GitHub requires the same checks of a queue batch, "
        "so every batch waits for this context until the queue times it out. "
        "Add `merge_group:` to the workflow's `on:` block. If the check only "
        "judges the pull request, skip its steps there, so the job still reports. "
        "Annotate the job "
        f"with '# {OPT_OUT}: <reason>' if no queue gates this branch."
    )


def _unreadable(name: str, cond: str, err: ExpressionError) -> str:
    return (
        f"job '{name}' is in a required check's needs closure but its `if:` "
        f"({cond!r}) could not be parsed as a GitHub expression ({err}) — an "
        "unreadable condition could hide an event exclusion. Simplify the "
        f"expression, or annotate the job with '# {OPT_OUT}: <reason>'."
    )


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    files = workflow_files()
    merge_queue = uses_merge_queue(files)
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path, merge_queue):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        print(
            "A job a required check depends on skips on an event the check "
            "gates, so the check reports green there without its work."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

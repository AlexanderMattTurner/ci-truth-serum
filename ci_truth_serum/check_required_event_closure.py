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
published Python parser exists for it) and evaluated with ``github.event_name``
and ``github.event.action`` bound and every other context unknown. Only a
DEFINITELY-false verdict fires: a fork guard, a ``needs.decide.outputs.*``
gate, or a title-keyword condition evaluates to unknown and passes, which is
what keeps the false-positive rate at zero on real trees.

Opt out per job with ``# event-scoped-ok: <reason>`` on or above the job when
the skip is deliberate and the reporter below it is honest about it.
"""

import json
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    _job_blocks,
    _marked_jobs,
    annotated_near,
    unwrap_expression,
    workflow_triggers,
)
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

OPT_OUT = "event-scoped-ok"
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# The events whose runs GitHub consults when it gates a merge. A skip on any
# other event cannot satisfy (or fail) a required check, so exclusion there is
# not a defect. workflow_call is unanalyzable per-file besides: its event_name
# is the calling workflow's.
GATING_EVENTS = ("pull_request", "pull_request_target", "merge_group")

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

_UNKNOWN = "unknown"


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
            return ("lit", tok)
        if kind == "path":
            self._take()
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


def _value_of(node: tuple, env: dict) -> object:
    """NODE's value where ENV determines it, else the _UNKNOWN sentinel."""
    if node[0] == "lit":
        return node[1]
    if node[0] == "path":
        return env.get(node[1].lower(), _UNKNOWN)
    if node[0] == "call" and node[1] == "fromjson" and len(node[2]) == 1:
        inner = _value_of(node[2][0], env)
        if inner is not _UNKNOWN:
            try:
                return json.loads(str(inner))
            except ValueError:
                return _UNKNOWN
    return _UNKNOWN


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
        if _UNKNOWN in (left, right):
            return _UNKNOWN
        equal = str(left) == str(right)
        return equal if node[1] == "==" else not equal
    if node[0] == "call" and node[1] == "contains" and len(node[2]) == 2:
        hay, needle = _value_of(node[2][0], env), _value_of(node[2][1], env)
        if _UNKNOWN in (hay, needle):
            return _UNKNOWN
        if isinstance(hay, list):
            return str(needle) in [str(item) for item in hay]
        return str(needle) in str(hay)
    if node[0] == "lit":
        return node[1] not in ("", "false", "0")
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
        env = {"github.event_name": event}
        if action is not None:
            env["github.event.action"] = action
        if truth_of(tree, env) is False:
            excluded.append(event if action is None else f"{event}:{action}")
    return excluded


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every closure job provably skipped on a
    gating event.

    A file that cannot be parsed as YAML is itself reported as a violation
    (line ``None``) rather than silently passed as clean — matching the sibling
    workflow lints (check_required_reporter &c.). An ``if:`` this parser cannot
    read is reported the same way: an unreadable condition on a required check's
    dependency could hide exactly the exclusion this lint exists to see.
    """
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
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
    required = _marked_jobs(blocks, jobs)

    violations: list[tuple[int | None, str]] = []
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
            if annotated_near(lines, start, OPT_OUT, span_end=span_end):
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
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
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

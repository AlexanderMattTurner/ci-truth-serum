#!/usr/bin/env python3
"""Fail a consumer that classifies a workflow run as red on a subset of the
conclusions GitHub actually returns.

A completed workflow run carries a `conclusion`. Most repositories write the
word `failure` and stop, because that is the conclusion they have seen. GitHub
returns four that mean the run went red:

  * `failure` — a job failed;
  * `timed_out` — the run passed its time limit;
  * `startup_failure` — the loader rejected the workflow file, so no job ran;
  * `action_required` — the run stopped and waits for a human.

The measured defect came from ONE repository with three answers to that
question. A `workflow_run` listener matched `conclusion == 'failure'`. A shell
router kept `failure` and dropped every other live conclusion. A Python
main-branch scanner counted `failure` as breakage. Their authors wrote them
months apart, and all three read as complete. A run that ended `timed_out` or
`startup_failure` reached nobody through any of them. Nothing went red, because
the code that decides what red means is the code that skipped it.

`cancelled` stays OUT of the required set. A run that a newer push supersedes
ends `cancelled` in normal operation, and so does a run a `concurrency:` group
cancels. A repository that pages on it pages on every rebase. A consumer may
still name `cancelled`; this check neither asks for it nor objects to it.
`stale` and `skipped` get the same treatment: recognized, never required.

WHAT COUNTS AS A CONSUMER. This check judges a site only when the site
recognizes `failure` itself. `failure` is the conclusion that means red with no
further cause, so a comparison against it asks the red question. A site that
names one SPECIFIC cause asks a different question, and this check leaves it
alone — `conclusion == 'startup_failure'` in a scan for runs that hold no jobs.
A test for `success` alone, or for `cancelled` alone, passes for the same
reason. So does a negation such as `!= 'success'`, which already recognizes
every conclusion there is.

THREE SURFACES, THREE PARSERS:

  * GitHub Actions workflows — `yaml.compose`. A finding then lands on the
    scalar the parser found, not on a line a text scan guessed at. Every scalar
    goes through the same reader, so an `if:` gate, a job-level `uses:` input
    and an `env:` value all count. A GitHub expression is not YAML, so a regex
    reads the comparison INSIDE the scalar. `check_multi_cron_gating` treats an
    `if:` the same way, and for the same reason: GitHub ships no grammar for it.
  * Shell — `_bash_ast`. Is this a test, or a string a command prints? Does this
    `elif` continue the same decision? Is this word a case pattern, or the body
    behind it? Each question is about shell STRUCTURE, and a text scan gets its
    own subset of them wrong (see `.claude/rules/shell-lint-parsing.md`).
  * Python — `_py_ast`. An `if`/`elif` chain is ONE decision, so this check
    judges the chain whole. It also resolves a set of conclusions bound to a
    constant, so `conclusion in FAILING` is read against what `FAILING` holds.
    That resolution follows Python's own scoping: two functions may each bind
    their own `RED`, and one function's value never answers for the other's.

THE REPOSITORY OVERRIDE. A repository that treats more conclusions as red
declares them once, in `.github/conclusion-coverage.yml`::

    extra: [stale]

Every consumer in the tree then owes that widened set. The file is optional, and
it is the ONE place that can widen the set. No consumer can narrow it, which is
the property this check holds. A malformed file, or a name that is not a
conclusion GitHub returns, is a hard error rather than an ignored line.

A commit that widens the set changes no consumer, so a changed-file run would
scan none of them and report a clean pass. This check therefore re-verifies
every tracked consumer whenever the override file is among the paths it gets.

Opt out with `# allow-conclusion-subset: <reason>` on the finding's line or the
comment block above it. The reason is required.

Fails closed on the artifact under test, in both directions. A workflow this
cannot parse is a finding, because "no findings" would be a false green on the
file under test. A shell file the bash grammar cannot read gets the same answer
through `unparseable_shell_reason`. An unreadable path in pre-commit's own file
list is skipped, because that path was already classified as text and a read
failure means it vanished.

KNOWN GAP: a conclusion test written in bash INSIDE a workflow's `run:` block is
read only for `${{ }}` expressions, not as shell. `check-inline-run-length`
pushes a block that large into `.github/scripts/`, where this check reads it
with the bash grammar.
"""

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    iter_nodes,
    node_text,
    parse as bash_parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    is_python_source,
    is_shell_source,
    unparseable_shell_reason,
)
from _py_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    lines as py_lines,
    name_of,
    trees,
)

OPT_OUT = "allow-conclusion-subset"

# The conclusions that mean the run went red and needs an answer. This is the
# declared set a repository's override widens; no consumer may narrow it.
TERMINAL_RED = frozenset({"failure", "timed_out", "startup_failure", "action_required"})

# The conclusion whose presence says a site is answering "did this run go red?".
# See the module docstring: the other three name a specific cause.
TRIGGER = "failure"

# The rest of GitHub's conclusion vocabulary. Recognized so an override naming
# one is accepted and a typo is not, never required of a consumer. `cancelled`
# sits here on purpose: a superseded run is not a failure.
OPTIONAL = frozenset({"success", "neutral", "skipped", "cancelled", "stale"})
VOCABULARY = TERMINAL_RED | OPTIONAL

# The repository's one machine-readable widening of the declared set.
CONFIG_PATH = Path(".github") / "conclusion-coverage.yml"
CONFIG_KEY = "extra"

# A name, key or expression path that holds a run conclusion. Matched against the
# last component only (`run_conclusion`, `CONCLUSION`,
# `github.event.workflow_run.conclusion`), so a `needs.build.result` — whose
# vocabulary is a different, smaller one — is never read as a conclusion.
_CONCLUSION = re.compile(r"conclusion", re.IGNORECASE)

# The workflow and composite-action files whose scalars carry GitHub expressions.
_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")


class ConfigError(ValueError):
    """Raised when `.github/conclusion-coverage.yml` cannot be read as a
    declaration. Loud on purpose: a silently ignored override would hold every
    consumer to the default set while the file says otherwise."""


@dataclass(frozen=True)
class Group:
    """One decision about a run conclusion, and where a reader fixes it.

    A decision is not a comparison: an `if`/`elif` chain, a `case`, and a
    `[[ a || b ]]` test each spread one classification over several comparisons,
    and judging a comparison alone would report a complete classifier as a
    partial one. `line` is where the first recognized conclusion is written.
    """

    line: int
    literals: frozenset[str]
    negates_success: bool


def load_extra(config_path: Path) -> frozenset[str]:
    """The conclusions CONFIG_PATH adds to the declared set; empty when the file
    is absent.

    Every failure here raises. A repository that wrote the file meant to widen
    the set, and a typo that widened nothing would leave the tree passing a check
    it believes is stricter than it is.
    """
    if not config_path.is_file():
        return frozenset()
    text = config_path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        raise ConfigError(
            f"{config_path}: could not parse as YAML "
            f"({str(err).partition(chr(10))[0]})."
        ) from err
    if doc is None:
        return frozenset()
    if not isinstance(doc, dict):
        raise ConfigError(
            f"{config_path}: the file must be a mapping with one `{CONFIG_KEY}:` "
            f"key, not {type(doc).__name__}."
        )
    unknown_keys = sorted(set(doc) - {CONFIG_KEY})
    if unknown_keys:
        raise ConfigError(
            f"{config_path}: unknown key(s) {unknown_keys}; the only key this "
            f"file takes is `{CONFIG_KEY}:`."
        )
    extra = doc.get(CONFIG_KEY, [])
    if not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        raise ConfigError(
            f"{config_path}: `{CONFIG_KEY}:` must be a list of conclusion names."
        )
    bad = sorted(set(extra) - VOCABULARY)
    if bad:
        raise ConfigError(
            f"{config_path}: {bad} are not run conclusions GitHub returns. "
            f"The vocabulary is {sorted(VOCABULARY)}."
        )
    return frozenset(extra)


def required_set(config_path: Path = CONFIG_PATH) -> frozenset[str]:
    """The terminal-red set every consumer in this repository must recognize."""
    return TERMINAL_RED | load_extra(config_path)


# ── judging one group ────────────────────────────────────────────────────


def missing_from(group: Group, required: frozenset[str]) -> frozenset[str]:
    """The conclusions GROUP fails to recognize, or an empty set when GROUP is
    not classifying red at all."""
    if TRIGGER not in group.literals or group.negates_success:
        return frozenset()
    return frozenset(required - group.literals)


def message(group: Group, missing: frozenset[str], remedy: str) -> str:
    """The finding a reader fixes from, with no other context to hand."""
    return (
        "this consumer classifies a workflow-run conclusion but recognizes only "
        f"{sorted(group.literals & VOCABULARY)}. GitHub also returns "
        f"{sorted(missing)}, and a run that ends in one of those is red and "
        f"reaches nobody here. {remedy} Widen the whole repository's set in "
        f"`{CONFIG_PATH}` if it is the set that is wrong, or annotate "
        f"`# {OPT_OUT}: <reason>` if this consumer must judge fewer."
    )


def _remedy(required: frozenset[str]) -> dict[str, str]:
    """The rewrite that recognizes REQUIRED, one spelling per surface."""
    names = sorted(required)
    array = json.dumps(names)
    return {
        "workflow": (
            "Test the whole set: "
            f"`contains(fromJSON('{array}'), github.event.workflow_run.conclusion)`."
        ),
        "shell": (
            "Test the whole set: "
            f'`case "$conclusion" in {"|".join(names)}) ... ;; esac`.'
        ),
        "python": (
            "Test the whole set: `conclusion in "
            f"{{{', '.join(repr(name) for name in names)}}}`."
        ),
    }


# ── workflow YAML ────────────────────────────────────────────────────────

# A GitHub expression path: what the left or right side of a conclusion
# comparison is spelled as. Quotes are excluded so the operand can never absorb
# the literal it is compared against.
_PATH = r"[A-Za-z_][A-Za-z0-9_.]*"
_COMPARISON = re.compile(
    rf"(?P<left>{_PATH})\s*(?P<op>==|!=)\s*(?P<rq>['\"])(?P<rlit>[^'\"]*)(?P=rq)"
    rf"|(?P<lq>['\"])(?P<llit>[^'\"]*)(?P=lq)\s*(?P<op2>==|!=)\s*(?P<right>{_PATH})"
)
# `contains(<haystack>, <needle>)` — the membership spelling of the same test.
_CONTAINS = re.compile(
    rf"contains\s*\(\s*(?P<haystack>.+?)\s*,\s*(?P<needle>{_PATH})\s*\)", re.DOTALL
)
_FROM_JSON = re.compile(r"^fromJSON\s*\(\s*(?P<q>['\"])(?P<json>.*)(?P=q)\s*\)$", re.S)
_QUOTED = re.compile(r"^(?P<q>['\"])(?P<body>.*)(?P=q)$", re.DOTALL)


def _haystack_literals(haystack: str) -> set[str]:
    """The conclusion names a `contains(...)` haystack holds.

    Two spellings: a `fromJSON('[...]')` array, and a plain quoted string the
    author separates by spaces or commas.
    """
    from_json = _FROM_JSON.match(haystack.strip())
    if from_json:
        try:
            parsed = json.loads(from_json.group("json"))
        except json.JSONDecodeError:
            return set()
        return {item for item in parsed if isinstance(item, str)}
    quoted = _QUOTED.match(haystack.strip())
    if quoted:
        return set(re.split(r"[,\s]+", quoted.group("body").strip()))
    return set()


def expression_facts(expression: str) -> list[tuple[str, str]]:
    """(conclusion name, `==` or `!=`) for every conclusion test in EXPRESSION.

    Matched as text, because a GitHub expression is not YAML and GitHub ships no
    grammar for it — the same decision `check_multi_cron_gating` records for
    `if:`. What keeps it narrow is that one side must be a path whose last
    component names a conclusion, so a bare `'failure'` in prose matches nothing.
    """
    facts: list[tuple[str, str]] = []
    for match in _COMPARISON.finditer(expression):
        path = match.group("left") or match.group("right") or ""
        literal = match.group("rlit")
        if literal is None:
            literal = match.group("llit")
        operator = match.group("op") or match.group("op2")
        if _CONCLUSION.search(path.rsplit(".", 1)[-1]) and literal is not None:
            facts.append((literal, operator))
    for match in _CONTAINS.finditer(expression):
        if not _CONCLUSION.search(match.group("needle").rsplit(".", 1)[-1]):
            continue
        facts += [(name, "==") for name in _haystack_literals(match.group("haystack"))]
    return facts


def _scalar_nodes(text: str):
    """Every scalar node in a YAML document, marks included."""
    stack = [yaml.compose(text, Loader=yaml.SafeLoader)]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, yaml.ScalarNode):
            yield node
        elif isinstance(node, yaml.SequenceNode):
            stack.extend(node.value)
        elif isinstance(node, yaml.MappingNode):
            stack.extend([part for pair in node.value for part in pair])


def _scalar_line(physical: list[str], node: yaml.ScalarNode, needle: str) -> int:
    """The 1-based line inside NODE's span where NEEDLE is written.

    A folded or literal block scalar spans many lines and PyYAML reports only
    where the block starts, so a finding anchored on the mark can land on the
    `if: >-` line rather than on the comparison a reader has to change.
    """
    start, end = node.start_mark.line, node.end_mark.line
    for index in range(start, min(end + 1, len(physical))):
        if needle in physical[index]:
            return index + 1
    return start + 1


def workflow_groups(text: str) -> list[Group]:
    """One group per YAML scalar that tests a run conclusion."""
    physical = text.split("\n")
    groups = []
    for node in _scalar_nodes(text):
        if not _CONCLUSION.search(node.value):
            continue
        facts = expression_facts(node.value)
        group = _group(facts, lambda needle: _scalar_line(physical, node, needle))
        if group:
            groups.append(group)
    return groups


def _group(facts: list[tuple[str, str]], line_of) -> "Group | None":
    """FACTS as one Group, or None when they name no conclusion at all."""
    literals = {name for name, _ in facts if name in VOCABULARY}
    if not literals:
        return None
    negates = any(name == "success" and op == "!=" for name, op in facts)
    anchor = TRIGGER if TRIGGER in literals else sorted(literals)[0]
    return Group(line_of(anchor), frozenset(literals), negates)


# ── shell ────────────────────────────────────────────────────────────────

# The `[[ ]]` / `[ ]` operators that compare a value against a fixed string.
_TEST_OPS = frozenset({"==", "=", "!=", "=~"})
# The nodes whose text IS the string they spell, with no shell computation.
_FIXED = frozenset({"word", "extglob_pattern", "raw_string", "number"})


def _shell_literal(node) -> "str | None":
    """The fixed string NODE spells, or None when the shell computes it.

    A `"$c"` is not a literal, and neither is `"pre$suffix"`: reading either as
    one would put a value this check cannot see into a consumer's recognized set.
    """
    if node.type in _FIXED:
        return unquote(node_text(node))
    if node.type == "string":
        parts = [child for child in node.children if child.type != '"']
        if all(child.type == "string_content" for child in parts):
            return "".join(node_text(child) for child in parts)
    return None


def _shell_names_conclusion(node) -> bool:
    """True when NODE reads a value a conclusion lives in — an expansion of a
    conclusion-named variable, or a substitution that asks the API for one."""
    for read in iter_nodes(
        node, "variable_name", "simple_expansion", "expansion", "command_substitution"
    ):
        if _CONCLUSION.search(node_text(read)):
            return True
    return False


def _condition_facts(node) -> list[tuple[str, str, int]]:
    """(conclusion, operator, 1-based line) for every comparison under NODE."""
    facts = []
    for expression in iter_nodes(node, "binary_expression"):
        children = expression.children
        if len(children) != 3 or children[1].type not in _TEST_OPS:
            continue
        left, operator, right = children
        if _shell_names_conclusion(left):
            value = right
        elif _shell_names_conclusion(right):
            value = left
        else:
            continue
        literal = _shell_literal(value)
        if literal is not None:
            operator_text = "!=" if operator.type == "!=" else "=="
            facts.append((literal, operator_text, expression.start_point[0] + 1))
    return facts


def _if_conditions(statement) -> list:
    """The nodes an `if`/`elif` chain decides on — its conditions, never its
    bodies. One chain is one decision, so a classifier split across `elif`
    branches is judged whole rather than branch by branch."""
    conditions = []
    for clause in [statement, *iter_nodes(statement, "elif_clause")]:
        # `.id`, never Python's `id()`: tree-sitter builds a NEW wrapper object
        # per access, so two reads of the same node are different Python objects
        # at addresses the allocator may even reuse. `.id` is the node's own
        # identity in the tree.
        if clause.id != statement.id and clause.parent.id != statement.id:
            continue
        collecting = False
        for child in clause.children:
            if child.type in ("if", "elif"):
                collecting = True
                continue
            if child.type == "then":
                break
            if collecting and child.type not in (";", "\n"):
                conditions.append(child)
    return conditions


def _case_facts(statement) -> list[tuple[str, str, int]]:
    """(conclusion, `==`, line) for a `case` whose subject is a conclusion."""
    subject = []
    for child in statement.children:
        if child.type == "in":
            break
        if child.type != "case":
            subject.append(child)
    if not any(_shell_names_conclusion(node) for node in subject):
        return []
    facts = []
    for item in iter_nodes(statement, "case_item"):
        for child in item.children:
            if child.type == ")":
                break
            literal = _shell_literal(child)
            if literal is not None:
                facts.append((literal, "==", child.start_point[0] + 1))
    return facts


def shell_groups(text: str) -> list[Group]:
    """One group per shell decision about a run conclusion."""
    root = bash_parse(text)
    groups: list[Group] = []
    claimed: set[int] = set()
    for statement in iter_nodes(root, "if_statement"):
        conditions = _if_conditions(statement)
        facts: list[tuple[str, str, int]] = []
        for condition in conditions:
            claimed.update(test.id for test in iter_nodes(condition, "test_command"))
            facts += _condition_facts(condition)
        group = _facts_to_group(facts)
        if group:
            groups.append(group)
    for test in iter_nodes(root, "test_command"):
        if test.id in claimed:
            continue
        group = _facts_to_group(_condition_facts(test))
        if group:
            groups.append(group)
    for statement in iter_nodes(root, "case_statement"):
        group = _facts_to_group(_case_facts(statement))
        if group:
            groups.append(group)
    return sorted(groups, key=lambda found: found.line)


def _facts_to_group(facts: list[tuple[str, str, int]]) -> "Group | None":
    """Facts that already carry their own line numbers, as one Group."""
    lines = {name: line for name, _, line in facts}
    return _group(
        [(name, operator) for name, operator, _ in facts],
        lambda needle: lines.get(needle, min(lines.values(), default=1)),
    )


# ── Python ───────────────────────────────────────────────────────────────

_SET_BUILDERS = frozenset({"frozenset", "set", "list", "tuple"})


def _py_names_conclusion(node: ast.AST) -> bool:
    """True when NODE reads a run conclusion: a conclusion-named variable or
    attribute, a `run["conclusion"]` subscript, or a `.get("conclusion")`."""
    name = name_of(node)
    if name and _CONCLUSION.search(name.rsplit(".", 1)[-1]):
        return True
    if isinstance(node, ast.Subscript):
        return _is_conclusion_key(node.slice)
    if isinstance(node, ast.Call):
        called = (name_of(node.func) or "").rsplit(".", 1)[-1]
        return called == "get" and bool(node.args) and _is_conclusion_key(node.args[0])
    return False


def _is_conclusion_key(node: ast.AST) -> bool:
    """True when NODE is the string constant a run's conclusion is read out of a
    parsed API response by: `run["conclusion"]`, `run.get("conclusion")`."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(_CONCLUSION.search(node.value))
    )


# The nodes that open a new Python scope. A name bound inside one is invisible
# outside it, so a binding walk that crosses these boundaries answers a question
# Python never asks.
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _own_nodes(scope: ast.AST):
    """Every descendant of SCOPE that belongs to SCOPE itself.

    A nested function, class or lambda is yielded, but this does not descend into
    its body: the names it binds are its own.
    """
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _constant_bindings(scope: ast.AST) -> dict[str, set[str]]:
    """The names SCOPE itself binds to a fixed collection of strings.

    Without this, `conclusion in FAILING_CONCLUSIONS` shows the checker a bare
    name and no conclusions at all. That is the exact shape of the defective
    scanner in the originating repository.

    Scoped, not flattened over the module: two functions may each bind their own
    `RED`, and reading the second one's value at the first one's comparison would
    call a partial classifier complete. Both the plain and the annotated
    spellings count — a `RED: frozenset[str] = frozenset({"failure"})` is an
    `ast.AnnAssign`, and skipping it would hide exactly the subset this check
    rejects.
    """
    constants: dict[str, set[str]] = {}
    for node in _own_nodes(scope):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        values = _py_literals(value, {})
        if values:
            constants[target.id] = values
    return constants


def _py_literals(node: ast.AST, constants: dict[str, set[str]]) -> set[str]:
    """The fixed strings NODE holds — a literal, a collection of literals, a
    builder call around one, or a name bound to one."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    if isinstance(node, ast.Call):
        builder = (name_of(node.func) or "").rsplit(".", 1)[-1]
        if builder in _SET_BUILDERS and len(node.args) == 1:
            return _py_literals(node.args[0], constants)
        return set()
    if isinstance(node, ast.Name):
        return set(constants.get(node.id, set()))
    return set()


def _compare_facts(
    compare: ast.Compare, constants: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """(conclusion, `==` or `!=`) for every conclusion test in one comparison."""
    operands = [compare.left, *compare.comparators]
    facts: list[tuple[str, str]] = []
    for index, operator in enumerate(compare.ops):
        left, right = operands[index], operands[index + 1]
        if isinstance(operator, (ast.Eq, ast.NotEq)):
            if _py_names_conclusion(left):
                value = right
            elif _py_names_conclusion(right):
                value = left
            else:
                continue
            sign = "!=" if isinstance(operator, ast.NotEq) else "=="
            facts += [(name, sign) for name in _py_literals(value, constants)]
        elif isinstance(operator, (ast.In, ast.NotIn)) and _py_names_conclusion(left):
            sign = "!=" if isinstance(operator, ast.NotIn) else "=="
            facts += [(name, sign) for name in _py_literals(right, constants)]
    return facts


def _chain_tests(node: ast.If) -> list[ast.expr]:
    """The tests of an `if`/`elif` chain that starts at NODE — one decision."""
    tests, current = [node.test], node
    while len(current.orelse) == 1 and isinstance(current.orelse[0], ast.If):
        current = current.orelse[0]
        tests.append(current.test)
    return tests


def _scope_groups(
    scope: ast.AST, inherited: dict[str, set[str]], out: list[Group]
) -> None:
    """Append every group SCOPE's own statements hold, then recurse.

    A nested scope inherits the names above it and may shadow any of them, which
    is what `{**inherited, **own}` says.
    """
    own = _constant_bindings(scope)
    constants = {**inherited, **own}
    nodes = list(_own_nodes(scope))
    continuations = {
        id(node.orelse[0])
        for node in nodes
        if isinstance(node, ast.If)
        and len(node.orelse) == 1
        and isinstance(node.orelse[0], ast.If)
    }
    claimed: set[int] = set()
    for node in nodes:
        if not isinstance(node, ast.If) or id(node) in continuations:
            continue
        facts: list[tuple[str, str]] = []
        for test in _chain_tests(node):
            for compare in ast.walk(test):
                if isinstance(compare, ast.Compare):
                    claimed.add(id(compare))
                    facts += _compare_facts(compare, constants)
        group = _group(facts, lambda _needle, at=node.lineno: at)
        if group:
            out.append(group)
    for node in nodes:
        if not isinstance(node, ast.Compare) or id(node) in claimed:
            continue
        group = _group(
            _compare_facts(node, constants), lambda _needle, at=node.lineno: at
        )
        if group:
            out.append(group)
    for nested in nodes:
        if isinstance(nested, _SCOPES):
            _scope_groups(nested, constants, out)


def python_groups(source: str) -> list[Group]:
    """One group per Python decision about a run conclusion."""
    groups: list[Group] = []
    for tree in trees(source):
        _scope_groups(tree, {}, groups)
    return sorted(groups, key=lambda found: found.line)


# ── driving one file ─────────────────────────────────────────────────────


def is_workflow_path(path: str) -> bool:
    """True when PATH names a workflow or composite action, whose scalars carry
    GitHub expressions."""
    return bool(_WORKFLOW_PATH.search(path.replace("\\", "/")))


def is_config_path(path: str, config_path: Path) -> bool:
    """True when PATH is the repository's override file."""
    return path.replace("\\", "/") == str(config_path).replace("\\", "/")


def tracked_consumers() -> list[str]:
    """Every tracked file this check can read: workflow, shell, or Python.

    Read from `git ls-files`, because the commit that WIDENS the declared set
    changes no consumer, and a changed-file run would then report a clean pass
    while every existing consumer still recognizes the old, narrower set. That
    pass is the false green this pack refuses, so widening the set re-verifies
    the whole tree. An unreadable path is skipped: the index can name a file a
    rename race just removed.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    found: list[str] = []
    for path in tracked:
        if not path:
            continue
        if is_workflow_path(path) or is_python_source(path):
            found.append(path)
            continue
        try:
            first = Path(path).read_text(encoding="utf-8").split("\n", 1)[0]
        except (OSError, UnicodeDecodeError):
            continue
        if is_shell_source(path, first):
            found.append(path)
    return sorted(found)


def violations(
    text: str, path: str, required: frozenset[str] = TERMINAL_RED
) -> list[tuple[int, str]]:
    """(1-based line, message) for every consumer in TEXT that recognizes a
    strict subset of REQUIRED. Empty when PATH is a kind this check cannot read.
    """
    remedies = _remedy(required)
    if is_workflow_path(path):
        try:
            groups, kind = workflow_groups(text), "workflow"
        except yaml.YAMLError as err:
            return [
                (
                    1,
                    "could not parse as YAML "
                    f"({str(err).partition(chr(10))[0]}); cannot tell whether "
                    "this workflow classifies a run conclusion on a subset of "
                    "the terminal-red set — fix the syntax and re-check.",
                )
            ]
    elif is_python_source(path):
        groups, kind = python_groups(text), "python"
    elif is_shell_source(path, text.split("\n", 1)[0]):
        groups, kind = shell_groups(text), "shell"
    else:
        return []

    physical = py_lines(text) if kind == "python" else text.split("\n")
    found = []
    for group in groups:
        missing = missing_from(group, required)
        if not missing or annotated_near(physical, group.line, OPT_OUT):
            continue
        found.append((group.line, message(group, missing, remedies[kind])))
    return sorted(found)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        metavar="PATH",
        help="the repository's terminal-red override, relative to the repo root "
        f"(default: {CONFIG_PATH})",
    )
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    required = TERMINAL_RED | load_extra(config_path)
    if not args.paths:
        print(
            "check_conclusion_coverage: no files to scan. This check reads only "
            "the paths you give it, so an empty run would report a clean pass "
            "over nothing.",
            file=sys.stderr,
        )
        return 2

    paths = args.paths
    if any(is_config_path(path, config_path) for path in paths):
        paths = tracked_consumers()
        print(
            f"note: {config_path} changed, so every tracked consumer "
            f"({len(paths)}) is re-checked against the declared set.",
            file=sys.stderr,
        )

    status = 0
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        reason = unparseable_shell_reason(path, text)
        if reason is not None:
            print(f"{path}: {reason}", file=sys.stderr)
            status = 1
            continue
        for line, detail in violations(text, path, required):
            print(f"{path}:{line}: {detail}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

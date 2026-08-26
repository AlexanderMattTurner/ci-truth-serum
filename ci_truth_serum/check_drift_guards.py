#!/usr/bin/env python3
"""Require a justification marker on drift-guard tests.

A drift guard — a test that asserts two duplicated sources agree — is a design
smell: with a true single source of truth nothing can drift, so no such test is
needed. The guard is legitimate only when an SSOT is genuinely infeasible (an
external value you don't control, a hard cross-language or cross-process
boundary), and that judgement belongs in the open. A guard MUST carry

    @pytest.mark.drift_guard("why a true SSOT is infeasible")

so review checks the stated reason, not the mere existence of the guard.

This check finds a Python guard three ways. No single way is hard to evade, so
all three run.

  1. INTENT PHRASING — the name or the docstring says what the test is. It says
     "drift guard", or "must stay in sync". This route is honest. An author who
     rewords the guard defeats it. A guard that calls itself an "SSOT-coverage
     contract" passes this route. That laundering is the failure this check
     exists to stop, so phrasing cannot be the only route.
  1a. A MODULE DOCSTRING that says the same thing declares the whole FILE. The
     file then gets ONE finding, on the docstring, because the sentence names no
     test. A module-level `pytestmark = pytest.mark.drift_guard("…")` justifies
     it, which is also how pytest itself marks every test in a file. Route 1 read
     function docstrings only, so a declaration one scope up used to hide the
     guard from all three routes: the tests under it need neither a guard NAME
     nor a comparison route 2 can see.
  1b. LAUNDERED AUTHORITY — a body COMMENT calls a value authoritative and also
     admits the value is a copy of one held elsewhere. An example is "SSOT char
     sets (mirrored from the per-layer suites)". Each half alone is safe, so only
     the conjunction fires. Route 1 cannot see this shape. A copy that an author
     relabels as a source of truth uses no drift words at all, so the phrasing
     pass reads it as the sanctioned single-source pattern.
  2. COPIES-AGREE STRUCTURE — the test compares a hand-maintained copy against a
     value that descends from a source read. A hand-maintained copy is an
     in-source collection literal, or an UPPER_CASE constant, or a `.keys()` view
     of one. This is the mechanical signature of "this hand-kept list must match
     the live config". It ignores what the docstring calls the test, so a new
     label cannot hide it.

Route 2 follows the DATA, not the layout of the file. An earlier version asked
for a source read and a maintained-copy equality in the SAME function body. Two
shapes walked past it:

  - the read moved to module scope, to a fixture, or to a read accessor, and the
    function body then held only the equality;
  - the equality fanned out over `@pytest.mark.parametrize`, and each case then
    compared one item instead of the whole collection.

A value is SOURCE-DERIVED when it descends from a source read. The read itself
qualifies. So do these:

  - a name that holds one;
  - a `sorted()` or a `set()` of one, and a `.keys()` view of one;
  - a subscript or an attribute of one;
  - an EXTRACTION from one, such as `_REV.findall(readme)`. An extraction selects
    part of a value and computes no new fact about it, so what the test then
    compares is still the file. The extraction methods are listed one by one.

Every OTHER call ends the descent. That one rule keeps the check off ordinary
tests: in `assertCountEqual(collect(data), EXPECTED)` the name `collect` is the
code under test, so its result is not source-derived. A comprehension also ends
the descent, which keeps the check off the many tests that build a result by
comprehension and then assert it equals `[]`.

Three bindings carry a source read out of the function that performs it:

  - a module-level assignment whose value is source-derived;
  - a parameter that names a `@pytest.fixture` in the same file that HANDS BACK a
    source-derived value, because a fixture supplies the value a test starts
    from. What the fixture returns decides this, never what its body touches: a
    fixture that reads a file only to write a copy elsewhere then returns the new
    directory, and that directory is not the file it read;
  - a call to a READ ACCESSOR, which is a module-level function whose body is one
    `return` of a source-derived value. A helper that reads and then transforms
    is not an accessor. Such a helper is hard to tell apart from the code under
    test, so it must not carry the read forward.

The fan-out route needs all three of these:

  - the test carries `@pytest.mark.parametrize` whose value list is a
    hand-maintained copy;
  - the body asserts an equality;
  - one side of that equality is a parameter the decorator binds, and the other
    side is source-derived.

The value list must be a maintained copy, so a fan-out over the LIVE side stays
quiet. That shape is the sanctioned single-source pattern: read one config, then
assert the code handles every entry.

Route 2 stays NARROW on purpose, because precision matters more than recall here.
A noisy guard teaches a reviewer to ignore it. Route 2 passes the sanctioned
single-source form, and it passes an ordinary output-versus-expected assertion.
Clear a genuine non-guard with a reasoned opt-out comment. Put it anywhere in the
function body, on a decorator, or in the comment block directly above:

    # not-a-drift-guard: <why this collection equality is not two copies>

That opt-out clears route 2 ONLY. A self-declaration is not a false positive, so
routes 1, 1a, and 1b need the marker, or the annotation the non-Python pass uses:

    # drift-guard-ok: <why a true SSOT is infeasible>

Put that annotation in the same window as the opt-out. A route 1 phrase usually
sits in a DOCSTRING, and a comment cannot go inside a docstring.

Each finding points at the line that DECLARES the guard. Route 1 points at the
`def` line for a guard NAME, and at the docstring line for a guard DOCSTRING.
Route 1b points at the comment. Route 2 points at the `def` line, because the
structure is the whole function. Each message names the hatch that its own route
accepts, so a reader never reads about an escape this check ignores.

Copies-agree tests also live in JavaScript, in TypeScript (``*.test.mjs``), and
in shell suites. Those carry no ``@pytest.mark``. A sibling phrase pass covers
them (``text_violations``). Any line that expresses drift-guard intent must carry
a ``drift-guard-ok: <why a true SSOT is infeasible>`` annotation. The annotation
goes on the same line, or on the line above. Route 1b runs there too, because it
needs only a comment. That surface still has no STRUCTURAL pass, so a guard that
avoids both phrasings passes. A structural pass for JS is the honest follow-up.

Honest limits, stated so this check is not itself laundered: detection is a
heuristic, not proof. These shapes still pass:

  - a comparison the AST cannot see, such as a hand-rolled element-by-element
    loop;
  - a set difference asserted empty, such as `assert exported - documented ==
    set()`. This route must stay quiet there, because the same line is also the
    sanctioned way to assert that one set covers another;
  - two module constants compared with no source read at all;
  - a value fetched at run time;
  - a fixture that lives in ``conftest.py``, because this check reads one file;
  - a value built by a comprehension over a source read;
  - a class attribute that ``setUpClass`` assigns;
  - a helper more than one hop from the read.

The routes close the common cases. They do not make laundering impossible.

Invoked by pre-commit with the staged Python / JS / TS / shell files as arguments.
"""

import ast
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
    python_comments,
    text_comments,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated_near,
    annotation_re,
    annotation_window,
    is_test_path,
    run_file_cli,
)

# Phrases that express guard INTENT — the author is asserting two sources can't
# diverge — rather than merely mentioning the word "drift" (which a test of
# drift-detection tooling, e.g. test_main_check_mode_detects_drift, also does).
# Kept deliberately specific: broad words like "mirror"/"parity"/"matches" recur
# in unrelated tests, and bare "lockstep" often names a runtime mechanism — so
# the copies-agree phrasings ("in lockstep", "kept in sync") are required, not
# just the word.
_GUARD_PATTERNS = (
    r"drift[- ]guard",
    r"anti[- ]?drift",
    r"(?:can't|cannot|never|won't) (?:drift|diverge)",
    r"must (?:stay|remain) in sync",
    r"in lockstep",
    r"kept in (?:sync|step)",
)
_GUARD_RE = re.compile("|".join(_GUARD_PATTERNS), re.IGNORECASE)

_MARKER = "drift_guard"

# The name a file-wide finding carries, in place of a test's. A module docstring
# declares the FILE a guard, and picking one test to blame would be a guess.
_MODULE = "<module>"

# Which route found a guard. The route decides the remedy the message states, so
# a finding carries it out of `violations` instead of `main` guessing it back.
_PHRASE_ROUTE = "phrase"
_STRUCTURAL_ROUTE = "structural"
_MODULE_ROUTE = "module"


class DriftFinding(NamedTuple):
    """One unjustified guard: the line that DECLARES it, the test's name, and the
    route that found it.

    `line` is the declaring line, not the enclosing `def`. A docstring that says
    "must stay in sync" can sit many lines below the `def`, and the author has to
    read the sentence to act on the finding.
    """

    line: int
    name: str
    route: str


# The non-Python opt-out: a comment `drift-guard-ok: <reason>` with a non-empty
# reason. (The bare token `drift-guard` inside it also matches _GUARD_RE, but the
# annotation check runs first, so an annotation line never flags itself.)
_ALLOW_TOKEN = "drift-guard-ok"
_ALLOW_MARKER = annotation_re(_ALLOW_TOKEN)

# The Python structural opt-out: `# not-a-drift-guard: <reason>` clears a
# STRUCTURAL hit (a genuine collection-equality unit test), with a non-empty
# reason so the escape is a stated judgement, not a silent mute.
_OPTOUT_RE = annotation_re("not-a-drift-guard")

# The LAUNDERED form: a comment that claims authority for a value while admitting
# the value is a copy of one held elsewhere ("SSOT char sets (mirrored from the
# per-layer suites)", "the canonical list, restated here"). Neither half is a
# smell alone — naming a real SSOT is the sanctioned idiom, and "mirror" describes
# plenty of honest runtime behaviour — so this fires only on the CONJUNCTION, and
# only inside a comment body.
_AUTHORITY_RE = re.compile(
    r"\bSSOT\b|\bsingle source of truth\b|\bcanonical\b", re.IGNORECASE
)
_COPY_RE = re.compile(
    r"\bmirror(?:s|ed|ing)?\b|\bcop(?:y|ies|ied)\b|\bduplicat(?:e|es|ed|ion)\b"
    r"|\brestat(?:e|es|ed|ing)\b|\bhand-(?:maintained|kept|copied)\b|\bin step with\b",
    re.IGNORECASE,
)
# A denial governing the copy word ("not restated here", "never a copy of the
# SSOT"). Without this the check inverts on its most common honest neighbour: a
# comment whose whole point is that the value is READ from the SSOT rather than
# duplicated says both halves in one breath, and would flag for saying so. The
# denial must sit in the same sentence as the copy word — hence `[^.]*$` — so a
# negation that belongs to an earlier clause does not excuse a later admission.
_NEGATED_RE = re.compile(
    r"\b(?:not|never|no|without|rather than|instead of)\b[^.]*$", re.IGNORECASE
)
# How far back a denial may sit and still govern the copy word. Counted in WORDS,
# never characters: a character window can cut mid-token and turn "cannot" into a
# string starting "not", which `\b` then reads as a denial that was never written.
_DENIAL_WINDOW_WORDS = 4


def _launders(body: str) -> str | None:
    """The matched authority word when comment BODY claims a value is
    authoritative while admitting it is a copy — else None."""
    authority = _AUTHORITY_RE.search(body)
    if not authority:
        return None
    copied = _COPY_RE.search(body)
    if not copied:
        return None
    preceding = " ".join(body[: copied.start()].split()[-_DENIAL_WINDOW_WORDS:])
    return None if _NEGATED_RE.search(preceding) else authority.group(0)


def _source_span(node: ast.FunctionDef | ast.AsyncFunctionDef) -> range:
    """Every line NODE occupies, counting its DECORATORS.

    `FunctionDef.lineno` is the `def` line, so a decorator sits ABOVE the node
    that carries it. A guard fanned out over `@pytest.mark.parametrize` keeps its
    hand-maintained collection in that decorator, so the case table is where a
    reviewer writes the opt-out. Starting the span at `def` would ignore it and
    leave that finding with no way to clear.
    """
    start = min(
        (decorator.lineno for decorator in node.decorator_list), default=node.lineno
    )
    return range(start, (node.end_lineno or node.lineno) + 1)


def _laundering_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef, comments: dict[int, str]
) -> int | None:
    """The FIRST line inside NODE's span whose comment launders a copy as
    authoritative, or None.

    The line is the finding's anchor, so a reader lands on the sentence that
    declares the guard rather than on the `def` above it.

    An annotation comment is skipped: `# not-a-drift-guard: …` and
    `# drift-guard-ok: …` both spell the token they exist to excuse, and a reason
    naturally spells the words this hunts for, so scanning them would make every
    opted-out test re-flag on the wording of its own opt-out.

    Only the laundering conjunction is read from body comments, never the
    _GUARD_PATTERNS phrases. Those phrases are tuned for a NAME or DOCSTRING,
    where "cannot drift" is the author classifying the test; in a free-form body
    comment the same words usually describe the code's behaviour ("the guard
    cannot drift into rejecting valid requests").
    """
    span = _source_span(node)
    return min(
        (
            line
            for line, body in comments.items()
            if line in span
            and not _OPTOUT_RE.search(body)
            and not _ALLOW_MARKER.search(body)
            and _launders(body)
        ),
        default=None,
    )


# Callables that construct/return a collection, and the collection-view methods.
_COLLECTION_CTORS = frozenset({"set", "frozenset", "sorted", "list", "tuple", "dict"})
_COLLECTION_METHODS = frozenset({"keys", "values", "items"})

# unittest asserts that compare two collections for equality.
_COLLECTION_ASSERTS = frozenset(
    {
        "assertEqual",
        "assertCountEqual",
        "assertSetEqual",
        "assertListEqual",
        "assertDictEqual",
    }
)

# Calls that read an external source into the test — the other half of the
# copies-agree signature (a *maintained copy* is only a drift guard when it is
# pinned against a *separate source*, typically a file/config read here).
_SOURCE_READS = frozenset(
    {"read_text", "read_bytes", "read", "load", "loads", "safe_load", "open"}
)

# Methods that pull a subset OUT of a value without computing a new fact about
# it. A drift guard routinely reads a doc and then extracts the interesting rows
# — `_REV.findall(readme)` — and what it compares is still the file's content.
# Listed explicitly rather than "any call whose argument is source-derived",
# because that wider rule would also carry the read through `normalize(live)`,
# where the result belongs to the code under test rather than to the file.
_EXTRACTIONS = frozenset(
    {"findall", "finditer", "split", "rsplit", "splitlines", "readlines", "group"}
)


def _is_drift_guard(name: str, docstring: str) -> bool:
    """A test reads as a drift guard if its name (underscores read as spaces) or
    its docstring uses guard-intent phrasing."""
    return bool(_GUARD_RE.search(name.replace("_", " ")) or _GUARD_RE.search(docstring))


def _docstring_phrase_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str]
) -> int:
    """The line of NODE's docstring that carries the guard phrase.

    The AST gives the literal's span, and the raw source lines inside that span
    give the line. `ast.get_docstring` re-indents the text and drops the quotes,
    so its offsets do not map back to the file.

    A phrase that a line break splits matches the cleaned docstring and no single
    raw line. The literal's first line answers for that one, because some line in
    the range must carry the finding.
    """
    literal = node.body[0].value
    span = range(literal.lineno, (literal.end_lineno or literal.lineno) + 1)
    return next(
        (line for line in span if _GUARD_RE.search(lines[line - 1])), span.start
    )


def _phrase_line(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    lines: list[str],
    comments: dict[int, str],
) -> int | None:
    """The line on which NODE declares itself a drift guard, or None.

    The three self-declaring routes each put the sentence somewhere else. A guard
    NAME sits on the `def` line. A guard DOCSTRING sits below it. A laundered
    comment sits in the body. The name is read first, because a test whose name
    and docstring both declare the guard is one finding, and the `def` line is
    the earlier of the two.
    """
    if _is_drift_guard(node.name, ""):
        return node.lineno
    if _is_drift_guard("", ast.get_docstring(node) or ""):
        return _docstring_phrase_line(node, lines)
    return _laundering_line(node, comments)


def _is_collection_shaped(node: ast.expr) -> bool:
    """True when NODE is or constructs a collection — a set/list/dict/tuple
    literal, a `set()/sorted()/list()/tuple()/dict()/frozenset()` call, or a
    `.keys()/.values()/.items()` call."""
    if isinstance(node, (ast.Set, ast.List, ast.Dict, ast.Tuple)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _COLLECTION_CTORS:
            return True
        if isinstance(func, ast.Attribute) and func.attr in _COLLECTION_METHODS:
            return True
    return False


def _is_maintained_copy(node: ast.expr) -> bool:
    """True when NODE is a HAND-MAINTAINED collection: an in-source
    set/list/dict/tuple literal, an UPPER_CASE module constant (or its
    `.keys()/.values()/.items()`), or a `set()/sorted()/...` wrapping one of
    those. This is the side of a drift-guard equality that a human keeps in step
    with a separate source by hand — the thing that drifts."""
    if isinstance(node, (ast.Set, ast.List, ast.Dict, ast.Tuple)):
        return True
    if isinstance(node, ast.Name):
        return node.id.isupper()
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name) and func.id in _COLLECTION_CTORS and node.args:
            return _is_maintained_copy(node.args[0])
        if isinstance(func, ast.Attribute) and func.attr in _COLLECTION_METHODS:
            return isinstance(func.value, ast.Name) and func.value.id.isupper()
    return False


def _is_read_call(node: ast.AST) -> bool:
    """True when NODE is a call that reads an external source — `p.read_text()`,
    `yaml.safe_load(...)`, or a bare `open(...)`."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in _SOURCE_READS
    return isinstance(func, ast.Name) and func.id == "open"


@dataclass(frozen=True)
class _Sources:
    """Which spellings in one module denote a source-derived value.

    `names` are names bound to one. `readers` are read accessors — call one and
    the result is source-derived. `fixtures` are fixtures that hand one back.

    A fixture's name is not merged into `names`, because only a test that takes
    it as a PARAMETER receives what it supplied. Merging the two would make the
    bare spelling source-derived in every function that reuses it for something
    else.
    """

    names: frozenset[str]
    readers: frozenset[str]
    fixtures: frozenset[str]


_NO_SOURCES = _Sources(frozenset(), frozenset(), frozenset())


def _source_derived(node: ast.expr, sources: _Sources) -> bool:
    """True when NODE's value descends from a source read.

    The descent survives a name binding, a collection constructor or view, a
    subscript, an attribute, and an `_EXTRACTIONS` method. Every OTHER call ends
    it, because a call is a transformation whose result belongs to whatever
    performed it — normally the code under test. That is the whole precision
    budget of the structural route: without it, `collect(data)` in an ordinary
    output-vs-expected assertion would read as the live side of a drift guard.
    """
    if isinstance(node, ast.Name):
        return node.id in sources.names
    if isinstance(node, (ast.Subscript, ast.Attribute)):
        return _source_derived(node.value, sources)
    if not isinstance(node, ast.Call):
        return False
    if _is_read_call(node):
        return True
    func = node.func
    if isinstance(func, ast.Name):
        if func.id in sources.readers:
            return True
        return (
            func.id in _COLLECTION_CTORS
            and bool(node.args)
            and _source_derived(node.args[0], sources)
        )
    if isinstance(func, ast.Attribute):
        if func.attr in _COLLECTION_METHODS:
            return _source_derived(func.value, sources)
        return func.attr in _EXTRACTIONS and any(
            _source_derived(operand, sources) for operand in (func.value, *node.args)
        )
    return False


def _reads_a_hoisted_source(node: ast.expr, outer: _Sources) -> bool:
    """True when NODE's value reaches the test ALREADY read — from a module-level
    name, from a fixture, or from a read accessor.

    OUTER must exclude the test's own locals. Both halves of the test are load
    bearing. `outer` rejects a name the test itself binds. `_NO_SOURCES` carries
    no bindings at all, so a value is derived under it only when the descent ends
    at a read call the test writes inline; rejecting those drops that shape too.

    Only the fan-out route needs this, because that route drops the
    collection-shape requirement and needs a constraint in its place. A fanned-out
    guard reads the live side ONCE and spreads the hand-kept copy over the cases,
    so its live side always arrives as a binding. A test that reads a file it
    just ran the code against is reading back that code's OUTPUT, and an
    observation of output is not a second copy of a source. `template-sync.sh`'s
    suite is exactly that shape: it writes a file, runs the script, then compares
    the file's new content against a per-case expectation.
    """
    return _source_derived(node, outer) and not _source_derived(node, _NO_SOURCES)


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when NODE carries a `@pytest.fixture` decorator, called or bare."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


def _yields_a_source(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the value NODE hands back is source-derived.

    For a FIXTURE, and deliberately not "a read anywhere in the body". A fixture
    that reads a file only to WRITE it somewhere else — the sandbox-building
    idiom — then returns the directory it built, and that directory is not the
    file it read. A whole-subtree walk would call the returned value
    source-derived on the strength of a read that never reaches it, which is the
    co-location mistake this route exists to stop making.
    """
    scope = _bind_assignments(_NO_SOURCES, node.body)
    return any(
        isinstance(child, (ast.Return, ast.Yield))
        and child.value is not None
        and _source_derived(child.value, scope)
        for child in ast.walk(node)
    )


def _is_read_accessor(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when NODE's body is a single `return` of a source-derived value.

    An accessor IS the read, so calling it carries the read to the caller. A
    helper that reads and then transforms is not an accessor: its return value is
    the transformation's output, which no rule can tell apart from the code under
    test. `_NO_SOURCES` is deliberate — an accessor must spell the read itself,
    so resolving it needs no module context and no definition ordering.
    """
    body = [
        stmt
        for stmt in node.body
        if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
    ]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return False
    return _source_derived(body[0].value, _NO_SOURCES)


def _assignments(statements: list[ast.stmt]) -> list[ast.Assign | ast.AnnAssign]:
    """Every assignment STATEMENTS makes in its OWN scope, in source order.

    Nested function, class, and lambda bodies are skipped: their locals are not
    bound here, and treating a helper's local as a module name would make the
    same spelling source-derived everywhere it recurs.
    """
    found: list[ast.Assign | ast.AnnAssign] = []
    pending = list(statements)
    while pending:
        node = pending.pop()
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            found.append(node)
        pending.extend(ast.iter_child_nodes(node))
    return sorted(found, key=lambda node: (node.lineno, node.col_offset))


def _bind_assignments(sources: _Sources, statements: list[ast.stmt]) -> _Sources:
    """SOURCES plus every name STATEMENTS binds to a source-derived value.

    One in-source-order pass is enough, and no fixpoint is needed: Python binds a
    value before anything reads it, so an assignment's right-hand side only names
    bindings this loop has already seen.
    """
    names = set(sources.names)
    for node in _assignments(statements):
        current = _Sources(frozenset(names), sources.readers, sources.fixtures)
        if node.value is None or not _source_derived(node.value, current):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return _Sources(frozenset(names), sources.readers, sources.fixtures)


def _module_sources(tree: ast.Module) -> _Sources:
    """The source-derived spellings a whole module offers its tests."""
    defs = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    seed = _Sources(
        frozenset(),
        frozenset(node.name for node in defs if _is_read_accessor(node)),
        frozenset(
            node.name for node in defs if _is_fixture(node) and _yields_a_source(node)
        ),
    )
    return _bind_assignments(seed, tree.body)


def _outer_sources(
    node: ast.FunctionDef | ast.AsyncFunctionDef, sources: _Sources
) -> _Sources:
    """What reaches NODE already read: the module's names, plus NODE's own
    parameters that name a fixture that reads. NODE's locals are excluded on
    purpose — `_reads_a_hoisted_source` needs that boundary."""
    params = {arg.arg for arg in (*node.args.args, *node.args.kwonlyargs)}
    return _Sources(
        sources.names | (params & sources.fixtures),
        sources.readers,
        sources.fixtures,
    )


class EqualityPair(NamedTuple):
    """One equality an assert makes: its two sides, and whether both sides
    construct a collection."""

    left: ast.expr
    right: ast.expr
    collection_shaped: bool


def _equality_pairs(node: ast.AST) -> list[EqualityPair]:
    """(left, right, collection_shaped) for every equality NODE asserts.

    Two spellings: a bare `assert a == b`, and an `assertEqual`-family call. The
    flag reports whether BOTH sides of a bare assert construct a collection. An
    `assertEqual`-family call is always flagged True: its own name says it
    compares collections, which is the shape test the bare form has to make for
    itself.
    """
    pairs: list[EqualityPair] = []
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assert)
            and isinstance(child.test, ast.Compare)
            and len(child.test.ops) == 1
            and isinstance(child.test.ops[0], ast.Eq)
        ):
            left, right = child.test.left, child.test.comparators[0]
            shaped = _is_collection_shaped(left) and _is_collection_shaped(right)
            pairs.append(EqualityPair(left, right, shaped))
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _COLLECTION_ASSERTS
            and len(child.args) >= 2
        ):
            pairs.append(EqualityPair(child.args[0], child.args[1], True))
    return pairs


def _asserts_maintained_copy_equals(node: ast.AST, sources: _Sources) -> bool:
    """True when NODE asserts a COLLECTION equality between a hand-maintained
    copy and a source-derived value.

    Both requirements matter. The maintained copy keeps this off ordinary
    output-vs-expected assertions. The source-derived side is what makes the pair
    two copies of one thing rather than an input and an expectation.

    A name can satisfy both — `LIVE = json.load(...)` is UPPER_CASE and holds a
    read. No precedence rule resolves that, and none should: asking only that the
    OTHER side be source-derived keeps `assert sorted(A_LIVE) == sorted(B_LIVE)`
    flagged, which is the file-A-equals-file-B guard this check exists for.
    """
    return any(
        shaped
        and (
            (_is_maintained_copy(left) and _source_derived(right, sources))
            or (_is_maintained_copy(right) and _source_derived(left, sources))
        )
        for left, right, shaped in _equality_pairs(node)
    )


def _argnames(node: ast.expr) -> list[str]:
    """The parameter names a `parametrize` argnames argument binds — from the
    comma-separated string form and from the list/tuple-of-strings form."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [name.strip() for name in node.value.split(",") if name.strip()]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            element.value.strip()
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
    return []


def _fanned_out_copy_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The parameters NODE binds from a `@pytest.mark.parametrize` whose value
    list is a hand-maintained copy.

    The maintained-copy requirement is what separates a fanned-out guard from the
    sanctioned single-source pattern. `parametrize("name", sorted(live_config))`
    fans out over the LIVE side and yields nothing here, so a test that reads one
    config and checks the code handles each entry stays quiet.
    """
    params: set[str] = set()
    for decorator in node.decorator_list:
        if not (
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "parametrize"
            and len(decorator.args) >= 2
            and _is_maintained_copy(decorator.args[1])
        ):
            continue
        params.update(_argnames(decorator.args[0]))
    return params


def _fans_out_a_maintained_copy(
    node: ast.FunctionDef | ast.AsyncFunctionDef, sources: _Sources
) -> bool:
    """True when NODE spreads a copies-agree equality across parametrize cases.

    The collection equality still exists; it just lives at the decorator instead
    of inside the assert, so `_asserts_maintained_copy_equals` sees one item
    compared against one item. Collection SHAPE is therefore not required here —
    the maintained collection is the value list, which `_fanned_out_copy_params`
    has already demanded. `_reads_a_hoisted_source` supplies the constraint that
    the dropped shape test used to carry.
    """
    params = _fanned_out_copy_params(node)
    if not params:
        return False

    def bound(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Name) and expr.id in params

    return any(
        (bound(left) and _reads_a_hoisted_source(right, sources))
        or (bound(right) and _reads_a_hoisted_source(left, sources))
        for left, right, _ in _equality_pairs(node)
    )


def _is_structural_guard(
    node: ast.FunctionDef | ast.AsyncFunctionDef, sources: _Sources
) -> bool:
    """The copies-agree structural signature, in either of its two shapes: a
    maintained copy asserted equal to a source-derived collection, or that same
    equality fanned out over parametrize.

    The two routes see different scopes. The collection route reads the test's
    locals too, because a read assigned to a local is the shape it has always
    caught. The fan-out route sees only what arrives from outside the body.
    """
    outer = _outer_sources(node, sources)
    return _asserts_maintained_copy_equals(
        node, _bind_assignments(outer, node.body)
    ) or _fans_out_a_maintained_copy(node, outer)


def _annotated(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    comments: dict[int, str],
    lines: list[str],
    marker: "re.Pattern[str]",
) -> bool:
    """True when a comment matching MARKER annotates NODE.

    Two markers use this. `# not-a-drift-guard: <reason>` is the explicit escape
    for a genuine collection-equality unit test that the structural trigger would
    otherwise flag. `# drift-guard-ok: <reason>` is the stated justification for a
    guard the phrasing routes find, and it clears every route, exactly as it does
    in a JS or shell suite.

    `annotation_window` owns WHERE the reason may go, so this check honours the
    same placement as every other hook in the pack instead of open-coding a
    twentieth answer. The span it is given starts at the first DECORATOR: a guard
    fanned out over `@pytest.mark.parametrize` keeps its hand-maintained
    collection there, so the case table is where a reviewer writes the reason.

    Which lines are COMMENTS still comes from real tokens, never from the raw
    text. This call suppresses a finding, so a text scan here fails OPEN: a
    string literal anywhere in the function that happens to spell the token — a
    fixture for this very lint, an error message quoting the escape hatch —
    would silently disarm the trigger for the whole test.
    """
    span = _source_span(node)
    return any(
        line in comments and marker.search(comments[line])
        for line in annotation_window(lines, span.start, span.stop - 1)
    )


def _justification(decorator: ast.expr) -> str | None:
    """The non-empty justification string of a @pytest.mark.drift_guard(...) call,
    or None if this decorator is not that marker / carries no string reason."""
    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if not (isinstance(func, ast.Attribute) and func.attr == _MARKER):
        return None
    if not decorator.args:
        return None
    arg = decorator.args[0]
    if (
        isinstance(arg, ast.Constant)
        and isinstance(arg.value, str)
        and arg.value.strip()
    ):
        return arg.value
    return None


def _module_justification(tree: ast.Module) -> bool:
    """True when a module-level `pytestmark` carries a justified
    `@pytest.mark.drift_guard(...)`.

    pytest applies `pytestmark` to every test in the file, so it is the answer
    pytest itself gives to a file-wide declaration. Without it a module docstring
    that declares a guard could only be cleared by decorating every test one by
    one, or by rewording the docstring — and rewording is exactly the laundering
    this check exists to stop.

    Only the LAST module-level binding counts, because that is the value pytest
    collects. A justified marker that a later line rebinds — `pytestmark =
    pytest.mark.drift_guard("…")`, then `pytestmark = pytest.mark.unit` — reaches
    no test, so it must clear nothing here either.
    """
    bindings = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign | ast.AnnAssign)
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name) and target.id == "pytestmark"
    ]
    if not bindings or bindings[-1].value is None:
        return False
    value = bindings[-1].value
    marks = list(value.elts) if isinstance(value, (ast.List, ast.Tuple)) else [value]
    return any(_justification(mark) for mark in marks)


def _module_declaration(tree: ast.Module) -> int | None:
    """The line of a MODULE docstring that declares the file a drift guard, or
    None.

    A file-wide claim is the same self-declaration as a test-level one, one
    scope up: "these tests must stay in sync with the live config" describes
    every test under it. Route 1 read only function docstrings, so
    moving the sentence to the top of the file hid the guard from all three
    routes at once — the tests below it need neither a guard NAME nor a
    comparison the AST can see.

    The finding lands ONCE, on the docstring, because the claim is about the
    file rather than about any one test. Naming a test here would be a guess at
    which one the sentence meant.

    A file with no `test_*` function is not judged at all. This is the same
    scoping the per-test routes get for free, and the check needs it: pre-commit
    hands it every staged `.py` file, and a lint that DETECTS drift describes
    what it detects in its own module docstring. `check_lockstep_pins` opens by
    naming the "keep in lockstep" comment it replaces, for exactly that reason.
    """
    if not _is_drift_guard("", ast.get_docstring(tree) or ""):
        return None
    if not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        for node in ast.walk(tree)
    ):
        return None
    return tree.body[0].lineno


def violations(source: str) -> list[DriftFinding]:
    """A DriftFinding for every test in SOURCE that reads as a drift guard — by intent
    PHRASING, by a LAUNDERED-authority body comment, or by copies-agree STRUCTURE
    — but lacks a justified @pytest.mark.drift_guard marker. Each finding sits on
    the line that DECLARES the guard, and names the route that found it. A file
    that does not parse as Python produces no findings (other tooling owns syntax
    errors).

    A structural-only hit is cleared by a `# not-a-drift-guard:` opt-out. That
    opt-out does not clear a self-declaration, because calling a copy
    authoritative is not a false positive. A `# drift-guard-ok:` annotation states
    the reason instead, so it clears either route.

    A MODULE docstring that declares the guard adds one further hit, named
    `<module>` and placed on the docstring, which a module-level `pytestmark`
    justifies."""
    try:
        tree = ast.parse(source)
        comments = python_comments(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return []

    sources = _module_sources(tree)
    lines = source.split("\n")
    hits: list[DriftFinding] = []
    # pytest applies a module-level `pytestmark` to every test in the file, so a
    # justified one answers for each of them too — not only for the file-wide
    # declaration below.
    justified_file = _module_justification(tree)
    declared = _module_declaration(tree)
    if declared is not None and not justified_file:
        hits.append(DriftFinding(declared, _MODULE, _MODULE_ROUTE))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        declaring = _phrase_line(node, lines, comments)
        structural = _is_structural_guard(node, sources)
        if declaring is None and not structural:
            continue
        if justified_file or any(_justification(dec) for dec in node.decorator_list):
            continue
        if _annotated(node, comments, lines, _ALLOW_MARKER):
            continue
        if declaring is None and _annotated(node, comments, lines, _OPTOUT_RE):
            continue
        route = _STRUCTURAL_ROUTE if declaring is None else _PHRASE_ROUTE
        anchor = node.lineno if declaring is None else declaring
        hits.append(DriftFinding(anchor, node.name, route))
    return hits


def text_violations(
    text: str, comments: dict[int, str] | None = None
) -> list[tuple[int, str]]:
    """(1-based line, matched phrase) for every line of TEXT that expresses
    drift-guard intent — an intent PHRASE anywhere on the line, or a LAUNDERED
    authority-plus-copy conjunction inside its comment — without a reason-bearing
    ``drift-guard-ok:`` annotation on that line or the one immediately above.

    The non-AST sibling of ``violations()``: JS/TS/shell tests carry no
    ``@pytest.mark``, so intent is detected by phrase and excused inline instead.

    COMMENTS maps 1-based line -> comment body, from ``comment_lines`` — the bash
    grammar for shell, the JS/TS grammar for a suite, the text delimiter scan for
    a language with neither. Passing it is what keeps the laundering trigger out
    of a heredoc body and out of a `"https://…"` string; omitting it applies that
    text scan to the whole file, which only the caller's path can improve on.

    The PHRASE half deliberately scans the whole line, comment or not: a test
    NAME is a self-declaration, so ``it('configs must stay in sync', …)`` is a
    finding even though it lives in a string literal. Only the laundering half is
    comment-scoped, because only it reads narration ABOUT the tree.
    """
    lines = text.split("\n")
    if comments is None:
        comments = text_comments(text)
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        if annotated_near(lines, i + 1, _ALLOW_TOKEN):
            continue
        body = comments.get(i + 1)
        match = _GUARD_RE.search(line)
        phrase = match.group(0) if match else body and _launders(body)
        if not phrase:
            continue
        hits.append((i + 1, phrase))
    return hits


_PREFER = "prefer removing the duplication (make one source authoritative)"


def _remedy(finding: DriftFinding) -> str:
    """The message FINDING prints, with the hatch its own route accepts.

    The route picks the sentence, because one sentence for all three routes can
    only name one hatch. `# not-a-drift-guard:` clears a structural hit alone, so
    an author who reads it under a phrasing hit writes a comment that this check
    ignores.
    """
    if finding.route == _MODULE_ROUTE:
        return (
            "this module's docstring declares a drift guard over every test in "
            f"the file, and states no reason — {_PREFER}, add pytestmark = "
            f'pytest.mark.{_MARKER}("why a true SSOT is infeasible"), or reword a '
            "docstring that does not mean it."
        )
    hatch = (
        "or a `# drift-guard-ok: <reason>` comment"
        if finding.route == _PHRASE_ROUTE
        else "or — for a genuine non-guard collection-equality — a "
        "`# not-a-drift-guard: <reason>` comment"
    )
    return (
        f"drift guard {finding.name!r} lacks a justification — {_PREFER}, add "
        f'@pytest.mark.{_MARKER}("why a true SSOT is infeasible"), {hatch}.'
    )


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.endswith(".py"):
            for finding in violations(source):
                print(f"{path}:{finding.line}: {_remedy(finding)}", file=sys.stderr)
                status = 1
            continue
        # The non-Python phrase pass runs only on TEST files: a drift guard is a
        # TEST asserting two copies agree, so a guard-intent phrase in production
        # shell/JS is prose about behaviour (a sync script's own comments
        # legitimately say "keeps X in sync") — scanning it there is pure
        # false-positive surface. Python needs no path filter: its AST pass above
        # already scopes to `test_*` functions.
        if not is_test_path(path):
            continue
        # Routing by PATH here, rather than inside `text_violations`, keeps the
        # grammar choice where the language is actually known.
        for lineno, phrase in text_violations(source, comment_lines(source, path)):
            print(
                f"{path}:{lineno}: drift-guard intent ({phrase!r}) lacks a "
                "justification — prefer removing the duplication (make one source "
                "authoritative), or annotate "
                "`drift-guard-ok: <why a true SSOT is infeasible>`.",
                file=sys.stderr,
            )
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

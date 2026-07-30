#!/usr/bin/env python3
"""Require a justification marker on drift-guard tests.

A drift guard — a test that asserts two duplicated sources agree — is a design
smell: with a true single source of truth nothing can drift, so no such test is
needed. The guard is legitimate only when an SSOT is genuinely infeasible (an
external value you don't control, a hard cross-language or cross-process
boundary), and that judgement belongs in the open. A guard MUST carry

    @pytest.mark.drift_guard("why a true SSOT is infeasible")

so review checks the stated reason, not the mere existence of the guard.

A Python guard is detected three ways, because no one of them alone is evadable:

  1. INTENT PHRASING — the name or docstring says what it is ("drift guard",
     "must stay in sync", ...). Honest, but a guard reworded to dodge the
     phrasing — calling itself an "SSOT-coverage contract" instead — slips
     straight through. That laundering is the whole failure mode this check
     exists to stop, so phrasing cannot be the only trigger.
  1b. LAUNDERED AUTHORITY — a body COMMENT that claims a value is authoritative
     while admitting it is a copy of one held elsewhere ("SSOT char sets
     (mirrored from the per-layer suites)"). Neither half is a smell alone, so
     only the conjunction fires. This is the shape trigger 1 structurally
     cannot see: a copy relabelled as a source of truth uses none of the drift
     vocabulary, so the phrasing pass reads it as the sanctioned single-source
     pattern and passes it.
  2. COPIES-AGREE STRUCTURE — the test READS an external source (a file/config)
     and asserts a COLLECTION equality where one side is a hand-maintained copy
     (an in-source collection literal, or an UPPER_CASE constant / its
     `.keys()`). That is the mechanical signature of "this hand-kept list must
     match the live config", and it does not care what the docstring calls the
     test, so relabeling can't hide it.

The structural trigger is deliberately NARROW to stay quiet on legitimate tests
(precision over recall — a noisy guard trains reviewers to ignore it). It fires
only on read-source + maintained-copy-vs-collection; it does NOT fire on the
sanctioned single-source form (read one config, assert code handles every entry
via membership/iteration), nor on an ordinary output-vs-expected unit assertion.
A structural hit that is a genuine non-guard clears with an explicit, reasoned
opt-out comment anywhere in the function body:

    # not-a-drift-guard: <why this collection equality is not two copies>

Copies-agree tests also live in JavaScript/TypeScript (``*.test.mjs``) and shell
suites, which carry no ``@pytest.mark``. For those a SIBLING phrase pass runs
(``text_violations``): any line expressing drift-guard intent must carry a
same-line or immediately-preceding ``drift-guard-ok: <why a true SSOT is
infeasible>`` annotation, or it is flagged. The laundered-authority trigger runs
there too (it needs only a comment, not an AST). That non-Python surface still
has no STRUCTURAL pass, so a guard that dodges both phrasings slips; a JS-side
structural pass is the honest follow-up.

Honest limits, stated so this check is not itself laundered: detection is a
heuristic, not proof. A copies-agree comparison the AST can't see (a hand-rolled
element-by-element loop, two module constants compared with no file read, a value
fetched at runtime) still slips. The triggers close the common cases; they do not
make laundering impossible.

Invoked by pre-commit with the staged Python / JS / TS / shell files as arguments.
"""

import ast
import re
import sys
import tokenize
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
    python_comments,
    text_comments,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotation_re,
    is_test_path,
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

# The non-Python opt-out: a comment `drift-guard-ok: <reason>` with a non-empty
# reason. (The bare token `drift-guard` inside it also matches _GUARD_RE, but the
# annotation check runs first, so an annotation line never flags itself.)
_ALLOW_MARKER = annotation_re("drift-guard-ok")

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


def _self_declares(
    node: ast.FunctionDef | ast.AsyncFunctionDef, comments: dict[int, str]
) -> bool:
    """True when a comment inside NODE's line span launders a copy as
    authoritative.

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
    end = node.end_lineno or node.lineno
    return any(
        _launders(body)
        for line, body in comments.items()
        if node.lineno <= line <= end
        and not _OPTOUT_RE.search(body)
        and not _ALLOW_MARKER.search(body)
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


def _is_drift_guard(name: str, docstring: str) -> bool:
    """A test reads as a drift guard if its name (underscores read as spaces) or
    its docstring uses guard-intent phrasing."""
    return bool(_GUARD_RE.search(name.replace("_", " ")) or _GUARD_RE.search(docstring))


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


def _reads_source(node: ast.AST) -> bool:
    """True when the function body reads an external source (a file/config load).
    Half of the structural signature — a maintained copy pinned against a
    separately-read source is the drift guard."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in _SOURCE_READS:
                return True
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            if child.func.id == "open":
                return True
    return False


def _asserts_maintained_copy_equals(node: ast.AST) -> bool:
    """True when the body asserts a COLLECTION equality with a hand-maintained
    copy on one side — `assert MAINTAINED == other_collection`, or an
    `assertEqual/assertCountEqual/...` where one argument is a maintained copy.
    The maintained-copy requirement is what keeps this off ordinary
    output-vs-expected unit assertions."""
    for child in ast.walk(node):
        if (
            isinstance(child, ast.Assert)
            and isinstance(child.test, ast.Compare)
            and len(child.test.ops) == 1
            and isinstance(child.test.ops[0], ast.Eq)
        ):
            left, right = child.test.left, child.test.comparators[0]
            if (
                _is_collection_shaped(left)
                and _is_collection_shaped(right)
                and (_is_maintained_copy(left) or _is_maintained_copy(right))
            ):
                return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr in _COLLECTION_ASSERTS
            and len(child.args) >= 2
            and (
                _is_maintained_copy(child.args[0]) or _is_maintained_copy(child.args[1])
            )
        ):
            return True
    return False


def _is_structural_guard(node: ast.AST) -> bool:
    """The copies-agree structural signature: the test reads a separate source
    AND asserts a hand-maintained collection copy equals it."""
    return _reads_source(node) and _asserts_maintained_copy_equals(node)


def _has_optout(
    node: ast.FunctionDef | ast.AsyncFunctionDef, comments: dict[int, str]
) -> bool:
    """True when a `# not-a-drift-guard: <reason>` comment sits within the
    function's source span — the explicit escape for a genuine collection-equality
    unit test that the structural trigger would otherwise flag.

    Read from real comment TOKENS, never the raw text. This one suppresses a
    finding, so a text scan here fails OPEN: a string literal anywhere in the
    function that happens to spell the token — a fixture for this very lint, an
    error message quoting the escape hatch — would silently disarm the trigger
    for the whole test. The tokenizer cannot confuse the two.
    """
    end = node.end_lineno or node.lineno
    return any(
        _OPTOUT_RE.search(body)
        for line, body in comments.items()
        if node.lineno <= line <= end
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


def violations(source: str) -> list[tuple[int, str]]:
    """(1-based line, function name) for every test in SOURCE that reads as a
    drift guard — by intent PHRASING, by a LAUNDERED-authority body comment, or by
    copies-agree STRUCTURE — but lacks a justified @pytest.mark.drift_guard marker.
    A structural-only hit is cleared by a `# not-a-drift-guard:` opt-out; the other
    two are not (calling a copy authoritative is a self-declaration). A file that
    does not parse as Python produces no findings (other tooling owns syntax
    errors)."""
    try:
        tree = ast.parse(source)
        comments = python_comments(source)
    except (SyntaxError, ValueError, tokenize.TokenError):
        return []

    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        phrasing = _is_drift_guard(
            node.name, ast.get_docstring(node) or ""
        ) or _self_declares(node, comments)
        structural = _is_structural_guard(node)
        if not (phrasing or structural):
            continue
        if any(_justification(dec) for dec in node.decorator_list):
            continue
        if structural and not phrasing and _has_optout(node, comments):
            continue
        hits.append((node.lineno, node.name))
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
        if _ALLOW_MARKER.search(line):
            continue
        if i > 0 and _ALLOW_MARKER.search(lines[i - 1]):
            continue
        body = comments.get(i + 1)
        match = _GUARD_RE.search(line)
        phrase = match.group(0) if match else body and _launders(body)
        if not phrase:
            continue
        hits.append((i + 1, phrase))
    return hits


def main(argv: list[str]) -> int:
    status = 0
    for path in argv:
        try:
            source = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if path.endswith(".py"):
            for lineno, name in violations(source):
                print(
                    f"{path}:{lineno}: drift guard {name!r} lacks a justification — "
                    "prefer removing the duplication (make one source authoritative), "
                    f'add @pytest.mark.{_MARKER}("why a true SSOT is infeasible"), or — '
                    "for a genuine non-guard collection-equality — a "
                    "`# not-a-drift-guard: <reason>` comment.",
                    file=sys.stderr,
                )
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
    sys.exit(main(sys.argv[1:]))

"""Tests for ci_truth_serum/_fastyaml.py — the one loader the pack parses through.

Two properties matter. The pack must reach libyaml when the installed PyYAML has
it, because every selector member re-parses the workflow tree in its own
subprocess and the pure-Python scanner is the pack's dominant cost. And the two
loaders must agree: a check reports `<path>:<line>:`, so a loader that shifted a
mark or dropped a mapping key would move findings, not just speed them up. The
agreement runs one way. libyaml accepts a little more than the Python scanner
does, and `test_the_c_loader_accepts_a_trailing_tab_the_python_one_rejects`
pins the one case this corpus found.
"""

import importlib.util
import subprocess

import pytest
import yaml
from hypothesis import assume, given
from hypothesis import strategies as st

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

mod = load_hook("_fastyaml.py", "_fastyaml")
lc = load_hook("_linecheck.py", "_linecheck")
cwp = load_hook("check_workflow_pipefail.py", "check_workflow_pipefail")

# A pure-Python twin of the shared line-tagging loader. It reuses `_linecheck`'s
# own mapping constructor, so the two loaders differ in exactly one thing: the
# parser underneath.
PureLineLoader = type("PureLineLoader", (yaml.SafeLoader,), {})
PureLineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, lc._mapping_with_line
)

_DOC = "on:\n  push:\n    branches: [main]\njobs:\n  a:\n    steps:\n      - run: x\n"


class _PureScannerReached(Exception):
    """Raised in place of PyYAML's Python scanner, to prove it never runs."""


# ── the fast loader is the one that runs ─────────────────────────────────
@pytest.mark.skipif(
    not hasattr(yaml, "CSafeLoader"), reason="PyYAML built without libyaml"
)
@pytest.mark.parametrize("loader", [mod.SafeLoader, lc.LineLoader, cwp._LineLoader])
def test_pack_loaders_never_enter_the_python_scanner(
    monkeypatch: pytest.MonkeyPatch, loader: type
) -> None:
    def _boom(self: object) -> None:
        raise _PureScannerReached

    monkeypatch.setattr(yaml.scanner.Scanner, "fetch_more_tokens", _boom)
    assert yaml.load(_DOC, Loader=loader)["jobs"]["a"]["steps"][0]["run"] == "x"


@pytest.mark.skipif(
    not hasattr(yaml, "CSafeLoader"), reason="PyYAML built without libyaml"
)
def test_the_python_scanner_probe_is_not_vacuous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe above proves something only if the pure loader trips it."""

    def _boom(self: object) -> None:
        raise _PureScannerReached

    monkeypatch.setattr(yaml.scanner.Scanner, "fetch_more_tokens", _boom)
    with pytest.raises(_PureScannerReached):
        yaml.load(_DOC, Loader=PureLineLoader)


def test_falls_back_to_the_python_loader_without_libyaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PyYAML built from source has no `CSafeLoader`; the pack still parses."""
    monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    spec = importlib.util.spec_from_file_location(
        "_fastyaml_nolibyaml", HOOKS_DIR / "_fastyaml.py"
    )
    fallback = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fallback)

    assert fallback.SafeLoader is yaml.SafeLoader
    assert fallback.safe_load(_DOC)["jobs"]["a"]["steps"][0]["run"] == "x"


# ── the two loaders agree ────────────────────────────────────────────────
def _corpus() -> list[str]:
    """Every YAML file this repo tracks: the workflows, the composite actions,
    and the two pre-commit manifests."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.yml", "*.yaml"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


CORPUS = _corpus()


# Shapes the repo's own workflows do not carry, where two YAML implementations
# are most likely to disagree.
EDGE_DOCUMENTS = {
    "anchor_and_alias": "base: &b {a: 1}\nuse: *b\n",
    "merge_key": "base: &b {a: 1}\nuse:\n  <<: *b\n  c: 2\n",
    "duplicate_key": "a: 1\na: 2\n",
    "yaml_1_1_booleans": "on: yes\noff: no\nn: n\n",
    "sexagesimal_like": "t: 12:30\nv: 1:2:3\n",
    "block_scalars": "a: |\n  one\n  two\nb: >-\n  three\n  four\n",
    "empty_and_nulls": "a:\nb: ~\nc: null\n",
    "unicode_and_escapes": 'a: "caf\\u00e9 \\t tab"\nb: 🙂\n',
    "quoted_hash": 'a: "#general"\nb: v # comment\n',
    "explicit_tags": "a: !!str 7\nb: !!int '8'\nc: !!binary aGk=\n",
    "nested_flow": "a: [{b: [1, 2]}, {c: {d: e}}]\n",
    "multi_document": "a: 1\n---\nb: 2\n",
    "empty_document": "",
}


def _shape(node: yaml.Node, seen: set[int] | None = None) -> object:
    """A node's kind, tag, marks and children — everything a check reads."""
    seen = set() if seen is None else seen
    if node is None or id(node) in seen:
        return None if node is None else ("alias", node.tag)
    seen = seen | {id(node)}
    marks = (
        type(node).__name__,
        node.tag,
        node.start_mark.line,
        node.start_mark.column,
        node.end_mark.line,
        node.end_mark.column,
    )
    if isinstance(node, yaml.ScalarNode):
        return marks + (node.value,)
    if isinstance(node, yaml.SequenceNode):
        return marks + tuple(_shape(child, seen) for child in node.value)
    return marks + tuple(
        (_shape(k, seen), _shape(v, seen))
        for k, v in node.value  # mapping pairs
    )


def _scalar_spans(text: str, loader: type) -> list[tuple[int, int]]:
    return [
        (token.start_mark.index, token.end_mark.index)
        for token in yaml.scan(text, Loader=loader)
        if isinstance(token, yaml.tokens.ScalarToken)
    ]


def test_the_corpus_is_populated() -> None:
    """Non-vacuity: an empty discovery would make the parity test below collect
    zero cases and pass while proving nothing."""
    assert len(CORPUS) > 20, f"only {len(CORPUS)} tracked YAML files — glob broke?"


@pytest.mark.parametrize("relpath", CORPUS)
def test_loaders_agree_on_the_repo_corpus(relpath: str) -> None:
    assert CORPUS, "no tracked YAML found — the corpus glob broke"
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")

    assert yaml.load(text, Loader=lc.LineLoader) == yaml.load(
        text, Loader=PureLineLoader
    ), f"{relpath}: the two loaders built different documents"
    assert _shape(mod.compose(text)) == _shape(
        yaml.compose(text, Loader=yaml.SafeLoader)
    ), f"{relpath}: the two loaders placed nodes at different marks"
    assert _scalar_spans(text, mod.SafeLoader) == _scalar_spans(
        text, yaml.SafeLoader
    ), f"{relpath}: the two scanners disagree on where scalars end"


@pytest.mark.parametrize("name", sorted(EDGE_DOCUMENTS))
def test_loaders_agree_on_edge_shapes(name: str) -> None:
    text = EDGE_DOCUMENTS[name]

    def _load(loader: type) -> object:
        return _outcome(lambda t: yaml.load(t, Loader=loader), text)

    assert _load(lc.LineLoader) == _load(PureLineLoader)
    assert _scalar_spans(text, mod.SafeLoader) == _scalar_spans(text, yaml.SafeLoader)


def test_line_tags_survive_the_c_parser() -> None:
    """The `__line__` tag is why the pack subclasses a loader at all: a finding
    must land on the step's own line."""
    doc = yaml.load(_DOC, Loader=lc.LineLoader)
    assert doc["__line__"] == 1
    assert doc["jobs"]["a"]["__line__"] == 6  # `steps:`, the mapping's first key
    assert doc["jobs"]["a"]["steps"][0]["__line__"] == 7


# ── the two loaders agree on generated input too ─────────────────────────
_FRAGMENTS = [
    "on:\n  push:\n    branches: [main]",
    "jobs:\n  a:\n    steps:\n      - run: echo hi",
    "a: &anchor {k: v}\nb: *anchor",
    "x: |\n  line\n   deeper",
    "y: >-\n  folded\n  text",
    "- 1\n- 2\n- {a: b}",
    "k: '#not a comment'  # a comment",
    "dup: 1\ndup: 2",
    "\ttab",
    "---\nz: 1\n---\nz: 2",
    "unicode: héllo 🙂",
    "empty:",
]


@st.composite
def _yaml_text(draw: st.DrawFn) -> str:
    """Concatenated YAML fragments plus optional garbage — the shape a lint is
    actually fed, which is whatever bytes happen to be staged."""
    parts = draw(st.lists(st.sampled_from(_FRAGMENTS), max_size=4))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=60)))
    return "\n".join(parts)


REFUSED = ("refused",)


def _outcome(load: object, text: str) -> object:
    """The document LOAD builds from TEXT, or `("refused",)` when it will not.

    The refusal carries no error class. The two implementations sort a malformed
    document into different `yaml.YAMLError` subclasses — `on:\\n{` is a
    `ParserError` to the Python parser and a `ScannerError` to libyaml — and
    every check in this pack catches the base class, so the split never reaches a
    finding. Anything that is NOT a `yaml.YAMLError` escapes and reddens this
    test, which is the crash-resistance half.
    """
    try:
        return ("parsed", load(text))
    except yaml.YAMLError:
        return REFUSED


@given(text=_yaml_text())
def test_the_c_loader_builds_what_the_python_loader_built(text: str) -> None:
    """Crash resistance and parity in one: on any input the Python loader parses,
    the pack's loader parses too and builds the same document. Anything that is
    not a `yaml.YAMLError` escapes `_outcome` and reddens this test."""
    expected = _outcome(yaml.safe_load, text)
    assume(expected != REFUSED)  # libyaml accepts a superset — see the tab case
    assert _outcome(mod.safe_load, text) == expected
    assert _outcome(lambda t: yaml.load(t, Loader=lc.LineLoader), text) == _outcome(
        lambda t: yaml.load(t, Loader=PureLineLoader), text
    )


@given(text=_yaml_text())
def test_scalar_spans_agree_on_generated_input(text: str) -> None:
    """`strip_yaml_comments` blanks comments off these spans, so a scanner that
    ended a scalar elsewhere would hide a real finding or invent one."""
    expected = _outcome(lambda t: _scalar_spans(t, yaml.SafeLoader), text)
    assume(expected != REFUSED)
    assert _outcome(lambda t: _scalar_spans(t, mod.SafeLoader), text) == expected


def test_the_c_loader_accepts_a_trailing_tab_the_python_one_rejects() -> None:
    """The one widening the property above assumes away, pinned as an example.

    PyYAML's Python scanner refuses a tab after a plain scalar. libyaml reads it
    as the separation whitespace YAML says it is, so a workflow with a trailing
    tab used to be reported as unparseable by every check in this pack and is now
    parsed and checked. The widening runs one way: no input the Python loader
    accepts is refused here.
    """
    text = "jobs:\n  a:\n    steps:\n      - run: echo hi\t\n"
    with pytest.raises(yaml.YAMLError):
        yaml.safe_load(text)
    assert mod.safe_load(text) == {"jobs": {"a": {"steps": [{"run": "echo hi"}]}}}

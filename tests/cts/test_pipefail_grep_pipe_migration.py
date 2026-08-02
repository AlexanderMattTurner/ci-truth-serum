"""Differential between the OLD and the NEW check_pipefail_grep_pipe, over real shell.

The old check flagged one shape: `producer | grep -q` under pipefail, with any
`echo`/`printf`/`:` producer exempt. The new one closes three gaps in that shape and
adds a precision gate. Unit cases pin each rule; this file pins what the change does to
CODE PEOPLE WROTE, which is the only place an over-fire shows up.

The corpus is real shell at named commits:

* `fixtures/consumer/*.txt` — files from AlexanderMattTurner/agent-glovebox. Two are
  `sbx-kit/image/lib/create-users.sh` at the commit that shipped the defect
  (`d9992573`) and at the commit that fixed it (`d744f9be`); the other eight are every
  file in that tree at `dbf665e` whose verdict this change moves. They carry a `.txt`
  suffix so this repo's own shell hooks do not lint another project's tree.
* this repo's own tracked shell files.
* the tree named by `CTS_CONSUMER_TREE`, when the variable is set.

Every divergence must have a cause from `_CAUSES`. The test also requires each widening
cause to appear at least once, so a gap that stops firing turns this red instead of
passing on an empty divergence set.

THIS FILE IS TEMPORARY. Delete it, and `fixtures/pipefail_grep_pipe_old.py`, when the
old implementation stops being the thing readers compare against.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT

from .test_check_pipefail_grep_pipe import mod as new_mod
from .test_check_pipefail_grep_pipe import tracked_shell_paths

_FIXTURES = Path(__file__).parent / "fixtures"
CONSUMER_FIXTURES = _FIXTURES / "consumer"


def _load_old():
    """The frozen pre-change implementation. `HOOKS_DIR` goes on `sys.path` first: the
    module imports `_bash_ast`/`_linecheck` as siblings, and its own directory here is
    the fixtures dir, which holds neither."""
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    src = _FIXTURES / "pipefail_grep_pipe_old.py"
    spec = importlib.util.spec_from_file_location("pipefail_grep_pipe_old", src)
    assert spec and spec.loader, src
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


old_mod = _load_old()

# What can make the two implementations disagree. Each is one of the three gaps or the
# precision gate, and nothing else may appear.
_GAP_1 = "expanding-bounded-producer"  # old exempted `printf … "$var"` as bounded
_GAP_2 = "heredoc-script"  # old never looked inside a generated hook
_GAP_3 = "widened-reader"  # old knew only `grep -q`
_LEVER_UNTESTED = "status-not-read"  # new needs the status to change what runs next
_LEVER_NOT_LAST = "reader-not-last"  # only the last stage answers the pipeline
_CAUSES = frozenset({_GAP_1, _GAP_2, _GAP_3, _LEVER_UNTESTED, _LEVER_NOT_LAST})

# The widening causes. Each must fire somewhere in the corpus, or the differential is
# passing because nothing happened.
_WIDENING = (_GAP_1, _GAP_2, _GAP_3)


def _reader_sites(text: str) -> list[tuple[int, object, list, int, bool]]:
    """Every `(reported line, pipeline node, stages, stage index, from a heredoc)` a
    reader could be reported at — one per non-first stage, since the old check reported
    ANY quiet grep stage and the new one only the last."""
    sites = []
    sources = [(0, text)] + [
        (line - 1, body) for line, body in new_mod.heredoc_scripts(new_mod.parse(text))
    ]
    for offset, source in sources:
        for pipeline in new_mod.iter_nodes(new_mod.parse(source), "pipeline"):
            stages = [
                c for c in pipeline.children if c.type not in new_mod._PIPE_TOKENS
            ]
            for index in range(1, len(stages)):
                sites.append(
                    (
                        stages[index].start_point[0] + 1 + offset,
                        pipeline,
                        stages,
                        index,
                        offset > 0,
                    )
                )
    return sites


def _causes_for(text: str, lineno: int, new_only: bool) -> set[str]:
    """Why the two implementations disagree at LINENO."""
    found: set[str] = set()
    for line, pipeline, stages, index, from_heredoc in _reader_sites(text):
        if line != lineno:
            continue
        reader = stages[index]
        if new_only:
            if from_heredoc:
                found.add(_GAP_2)
            elif not old_mod._is_quiet_grep(reader):
                found.add(_GAP_3)
            elif old_mod._producer_is_bounded(stages[index - 1]):
                found.add(_GAP_1)
            continue
        negated = stages[0].type == "negated_command"
        if not new_mod._status_read(pipeline, negated):
            found.add(_LEVER_UNTESTED)
        elif index != len(stages) - 1:
            found.add(_LEVER_NOT_LAST)
    return found


def _corpus() -> list[Path]:
    paths = sorted(CONSUMER_FIXTURES.glob("*.txt")) + tracked_shell_paths(REPO_ROOT)
    tree = os.environ.get("CTS_CONSUMER_TREE")
    if tree:
        paths += tracked_shell_paths(Path(tree))
    return paths


def _divergences() -> list[tuple[Path, int, bool, set[str]]]:
    """`(path, line, new_only, causes)` for every line the two disagree on."""
    out = []
    for path in _corpus():
        text = path.read_text(encoding="utf-8", errors="replace")
        old_hits, new_hits = (
            set(old_mod.violations(text)),
            set(new_mod.violations(text)),
        )
        for line in sorted(new_hits - old_hits):
            out.append((path, line, True, _causes_for(text, line, True)))
        for line in sorted(old_hits - new_hits):
            out.append((path, line, False, _causes_for(text, line, False)))
    return out


@pytest.fixture(scope="module", name="divergences")
def _divergences_fixture() -> list[tuple[Path, int, bool, set[str]]]:
    return _divergences()


def test_every_divergence_has_a_named_cause(divergences) -> None:
    """A verdict this change moves for a reason nobody wrote down is a regression."""
    unexplained = [
        f"{path.name}:{line} ({'new' if new_only else 'old'}-only) causes={causes}"
        for path, line, new_only, causes in divergences
        if not causes or not causes <= _CAUSES
    ]
    assert unexplained == [], f"divergences with no named cause: {unexplained}"


@pytest.mark.parametrize("cause", _WIDENING)
def test_each_widening_cause_fires_in_the_corpus(divergences, cause: str) -> None:
    """Non-vacuity, per gap. A gap that stops finding anything real reds here rather
    than passing on an empty divergence set."""
    hit = [
        f"{path.name}:{line}"
        for path, line, _new_only, causes in divergences
        if cause in causes
    ]
    assert hit, f"no divergence in the corpus is explained by {cause}"


def test_the_change_only_widens_over_this_corpus(divergences) -> None:
    """Measured, not assumed: over this corpus the precision gate removes no verdict the
    old check produced. Every divergence is a NEW finding. A future corpus that does
    lose one reds here, and the loss then has to be justified rather than absorbed."""
    lost = [
        f"{path.name}:{line} ({sorted(causes)})"
        for path, line, new_only, causes in divergences
        if not new_only
    ]
    assert lost == [], f"the new check drops verdicts the old one had: {lost}"


def test_the_shipped_defect_is_the_headline_divergence(divergences) -> None:
    """The one line this whole change exists for: `create-users.sh` line 366 at
    `d9992573`, invisible to the old check because it lived inside a generated hook AND
    because its `printf '%s' "$input"` producer looked bounded."""
    entry = [
        (line, causes)
        for path, line, new_only, causes in divergences
        if path.name.startswith("create-users-d9992573") and new_only
    ]
    assert entry == [(366, {_GAP_2})]

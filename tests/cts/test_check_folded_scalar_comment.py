"""Tests for ci_truth_serum/check_folded_scalar_comment.py — the lint that bans a
`#`-leading line inside a YAML FOLDED (`>`/`>-`) block scalar whose value is an
argument string.

The load-bearing test is not a restatement of the module's regexes:
``test_a_folded_comment_really_discards_the_flags_below_it`` drives the REAL YAML
parser and the REAL shell-word splitter over a fixture and asserts on the
resulting argv — the arguments written after the `#` never survive. That is the
check's whole justification. Everything else pins WHEN it fires.
"""

import shlex
import string
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

fsc = load_hook("check_folded_scalar_comment.py", "check_folded_scalar_comment")


def _yaml(*lines: str) -> str:
    """A YAML document from LINES, so a test's 1-based line numbers are countable
    by eye against the literal it passes in."""
    return "\n".join(lines) + "\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


# ── why the lint exists: real parser + real splitter, end to end ──────────

_E2E_BROKEN = _yaml(
    "jobs:",  # 1
    "  agent:",  # 2
    "    steps:",  # 3
    "      - uses: some/agent-action@v1",  # 4
    "        with:",  # 5
    "          tool_args: >-",  # 6
    "            --setting-sources user",  # 7
    "            # Path-scoped, never bare: explains the grant below",  # 8
    '            --allowedTools "Read(./**),Edit(//tmp/out.json)"',  # 9
)

_E2E_FIXED = _yaml(
    "jobs:",  # 1
    "  agent:",  # 2
    "    steps:",  # 3
    "      - uses: some/agent-action@v1",  # 4
    "        with:",  # 5
    "          # Path-scoped, never bare: explains the grant below",  # 6
    "          tool_args: >-",  # 7
    "            --setting-sources user",  # 8
    '            --allowedTools "Read(./**),Edit(//tmp/out.json)"',  # 9
)


def _tool_args(document: str) -> str:
    """The `tool_args` input as the real YAML parser produces it — the value the
    action would actually hand to the CLI."""
    return yaml.safe_load(document)["jobs"]["agent"]["steps"][0]["with"]["tool_args"]


def test_a_folded_comment_really_discards_the_flags_below_it():
    """No regex, no source text: the real YAML parser folds the `#` line into the
    value, and the real shell-word splitter then treats it as a comment and drops
    everything after it. `--allowedTools` is written plainly in the file and never
    reaches the program, so the step runs with NO permission rules."""
    value = _tool_args(_E2E_BROKEN)
    assert "--allowedTools" in value  # the parser keeps it; the splitter does not

    argv = shlex.split(value, comments=True)
    assert argv == ["--setting-sources", "user"]
    assert not [word for word in argv if "allowedTools" in word]


def test_the_remedy_really_delivers_the_flags():
    """The same arguments with the comment moved above the key: the parser drops
    the comment and every flag survives the split."""
    assert shlex.split(_tool_args(_E2E_FIXED), comments=True) == [
        "--setting-sources",
        "user",
        "--allowedTools",
        "Read(./**),Edit(//tmp/out.json)",
    ]


def test_the_lint_separates_exactly_those_two_documents():
    """Ties the check to the proven failure above: red on the document whose flags
    are provably discarded, silent on the one whose flags survive."""
    assert fsc.violations(_E2E_BROKEN) == [8]
    assert fsc.violations(_E2E_FIXED) == []


# ── when it fires ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "header",
    [
        ">",
        ">-",
        ">+",
        ">2",
        ">2-",
        # a header carrying a REAL trailing comment: that `#` is genuine YAML
        # comment syntax, so it is neither reported itself nor stops the judging
        ">-  # the one place a `#` is a comment in this construct",
    ],
)
def test_fires_under_every_folded_header_spelling(header):
    text = _yaml(
        f"tool_args: {header}",
        "  --setting-sources user",
        "  # explains the grant below",
        '  --allowedTools "Read(./**)"',
    )
    assert fsc.violations(text) == [3]


def test_fires_on_a_sequence_item_folded_scalar():
    """`- >-` has no key at all; the block's extent is measured from the dash."""
    text = _yaml(
        "args:",  # 1
        "  - >-",  # 2
        "    --setting-sources user",  # 3
        "    # explains the grant below",  # 4
        '    --allowedTools "Read(./**)"',  # 5
        "  - a sibling item that closes the block",  # 6
    )
    assert fsc.violations(text) == [4]


def test_every_comment_line_is_reported_by_its_own_number():
    text = _yaml(
        "tool_args: >-",  # 1
        "  --setting-sources user",  # 2
        "  # first comment line",  # 3
        "  # second comment line",  # 4
        "  --model sonnet",  # 5
        "  # third comment line",  # 6
        '  --allowedTools "Read(./**)"',  # 7
    )
    assert fsc.violations(text) == [3, 4, 6]


def test_a_blank_line_inside_a_block_does_not_end_it():
    """A blank line carries no indentation, so it is not a dedent that closes the
    block — the `#` line after it is still content."""
    text = _yaml(
        "tool_args: >-",  # 1
        "  --setting-sources user",  # 2
        "",  # 3
        "  # explains the grant below",  # 4
        '  --allowedTools "Read(./**)"',  # 5
    )
    assert fsc.violations(text) == [4]


# ── when it does not fire ────────────────────────────────────────────────


@pytest.mark.parametrize("marker", ["|", "|-", "|+", "|2", "|2-"])
def test_a_literal_block_scalar_is_not_covered(marker):
    """A literal scalar keeps its newlines, so a `#` line stays on its own line and
    nothing folds it into an argument string. The `- ` bullet is exactly the
    argument-string tell the folded check keys on, so this fixture WOULD fire if a
    literal spelling were ever mistaken for a folded one."""
    text = _yaml(
        "      - name: Ask the agent",
        "        with:",
        f"          prompt: {marker}",
        "            # Task",
        "            Review the diff and report what changed.",
        "            - Prefer a fix over a workaround.",
    )
    assert fsc.violations(text) == []


def test_a_folded_block_of_pure_prose_does_not_fire():
    """No content line begins with `-`, so the value is prose and never shell-split:
    a folded `#` there is mangled cosmetically, not silently truncated."""
    text = _yaml(
        "inputs:",  # 1
        "  paths-regex:",  # 2
        "    description: >",  # 3
        "      Extended regex matched against the changed paths. Prose that",  # 4
        "      # looks like a comment here is folded into the description and",  # 5
        "      only reads oddly, because nothing shell-splits a description.",  # 6
        "    required: true",  # 7
    )
    assert fsc.violations(text) == []


def test_a_sequence_item_header_is_not_itself_the_option_line():
    """`- >` begins with a dash, but it is the HEADER, not a content line — counting
    it as the option line that arms the check would fire on every folded item."""
    text = _yaml(
        "notes:",  # 1
        "  - >",  # 2
        "    Prose that",  # 3
        "    # looks like a comment is folded into this note and reads oddly,",  # 4
        "    because nothing shell-splits a note.",  # 5
    )
    assert fsc.violations(text) == []


@pytest.mark.parametrize("comment_indent", ["        ", "    ", ""])
def test_a_comment_at_or_left_of_the_key_indent_has_closed_the_block(comment_indent):
    """A folded block extends only over lines indented MORE than its header. At or
    left of that indent the block is over and the `#` is a genuine YAML comment —
    the construct a contributor is being told to write."""
    text = _yaml(
        "        tool_args: >-",
        "          --setting-sources user",
        '          --allowedTools "Read(./**)"',
        f"{comment_indent}# a genuine YAML comment, outside the block",
        "        env:",
        "          FOO: bar",
    )
    assert fsc.violations(text) == []


def _top_level_scalar(document: str) -> str:
    return yaml.safe_load(document)["tool_args"]


def test_a_hash_mid_line_is_not_a_comment_line():
    """Only a LINE-LEADING `#` reads as a comment a human wrote. A `#` inside an
    argument value is part of that argument (and the splitter keeps it: it opens a
    shell comment only at a word boundary)."""
    text = _yaml(
        "tool_args: >-",
        "  --setting-sources user",
        "  --channel '#general'",
        '  --allowedTools "Read(./**)"',
    )
    assert fsc.violations(text) == []
    assert "#general" in shlex.split(_top_level_scalar(text), comments=True)


def test_a_folded_argument_string_with_no_comment_line_does_not_fire():
    text = _yaml(
        "tool_args: >-",
        "  --setting-sources user",
        '  --allowedTools "Read(./**)"',
    )
    assert fsc.violations(text) == []


# ── opt-out ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "lines",
    [
        # on the flagged line itself
        (
            "  --setting-sources user",
            f"  # {fsc.ALLOW}: this folded prose really does begin with #",
        ),
        # on the block content line above it
        (
            f"  --setting-sources user  # {fsc.ALLOW}: prose begins with #",
            "  # a line the annotation above clears",
        ),
    ],
)
def test_a_reason_bearing_annotation_opts_out(lines):
    text = _yaml("tool_args: >-", *lines, '  --allowedTools "Read(./**)"')
    assert fsc.violations(text) == []


@pytest.mark.parametrize(
    ("carrier", "expected"),
    [
        # the slug in the folded VALUE, not in a comment: an argument string may
        # legitimately carry the words, and they must not clear the line below
        (f"  --note {fsc.ALLOW}: an argument, not an annotation", [3]),
        (f'  --message "{fsc.ALLOW}: still not an annotation"', [3]),
        # a bare slug states that someone looked, not what they concluded — and the
        # carrier is itself a comment line inside the block, so it is flagged too
        (f"  # {fsc.ALLOW}", [2, 3]),
        # a DIFFERENT, longer slug that merely contains this one
        (f"  # {fsc.ALLOW}-legacy: a neighbouring lint's opt-out", [2, 3]),
    ],
)
def test_a_non_annotation_does_not_opt_out(carrier, expected):
    text = _yaml(
        "tool_args: >-",
        carrier,
        "  # the line the carrier above must not clear",
        "  --model sonnet",
    )
    assert fsc.violations(text) == expected


# ── check_file / main ────────────────────────────────────────────────────


def test_check_file_reports_the_violation_with_its_line(tmp_path):
    found = fsc.check_file(_write(tmp_path, _E2E_BROKEN))
    assert found == [(8, fsc.MESSAGE)]


def test_check_file_passes_the_remedy(tmp_path):
    assert fsc.check_file(_write(tmp_path, _E2E_FIXED)) == []


def test_check_file_reports_malformed_yaml(tmp_path):
    """An unparseable workflow is reported, not silently passed: this file IS the
    artifact under test, so "no findings" on it would be a false green."""
    found = fsc.check_file(_write(tmp_path, "on: [push\njobs: {\n"))
    assert len(found) == 1
    line, message = found[0]
    assert line is None
    assert "could not parse as YAML" in message


def _point_at(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(fsc, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(fsc, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(fsc, "ACTIONS_DIR", tmp_path / "nonexistent")
    return wf


def test_main_returns_zero_when_clean(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "ok.yaml").write_text(_E2E_FIXED)
    assert fsc.main() == 0
    assert "ERROR" not in capsys.readouterr().out


def test_main_reports_and_fails_on_violation(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "bad.yaml").write_text(_E2E_BROKEN)
    (wf / "ok.yaml").write_text(_E2E_FIXED)
    assert fsc.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=8::" in out
    assert "1 violation(s) found" in out


def test_main_reports_unparseable_yaml_without_a_line(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    (wf / "bad.yaml").write_text("on: [push\njobs: {\n")
    assert fsc.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml::" in out
    assert ",line=" not in out


# ── fuzz: the invariants that hold for ALL inputs ─────────────────────────

# Tokens the detector actually branches on, so generated documents are not inert
# noise — folded and literal headers in every spelling, option lines, comment
# lines, the annotation, and indentation at several depths.
_TOKENS = [
    "tool_args: >-",
    "tool_args: >",
    "args: >2+",
    "  - >-",
    "  - key: >",
    "prompt: |",
    "prompt: |-",
    "  --flag value",
    "    --flag value",
    "  # a comment line",
    "    # a comment line",
    "#",
    f"  # {fsc.ALLOW}: a stated reason",
    f"  # {fsc.ALLOW}",
    f"  --note {fsc.ALLOW}: not an annotation",
    "  plain content",
    "        deeply indented",
    "\t--tab-indented",
    "",
    "   ",
]

# Newline variants str.splitlines() recognises, written as escapes so no invisible
# byte hides in this source.
_NEWLINES = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"]

_LINE = st.one_of(
    st.sampled_from(_TOKENS),
    st.text(alphabet=string.printable.replace("\n", "").replace("\r", ""), max_size=40),
)


@st.composite
def _documents(draw) -> str:
    lines = draw(st.lists(_LINE, max_size=14))
    return draw(st.sampled_from(_NEWLINES)).join(lines)


@given(_documents())
def test_violations_never_crashes_and_reports_real_lines(text):
    hits = fsc.violations(text)
    assert hits == fsc.violations(text)  # deterministic
    lines = text.splitlines()
    assert hits == sorted(hits)
    assert len(set(hits)) == len(hits)
    for lineno in hits:
        assert 1 <= lineno <= len(lines)
        # every reported line really opens with a `#` — the property that makes
        # the message ("this line reads as a comment") true of what it points at
        assert lines[lineno - 1].lstrip().startswith("#")

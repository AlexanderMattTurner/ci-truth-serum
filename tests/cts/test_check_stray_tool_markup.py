"""Tests for hooks/check_stray_tool_markup.py — the lint that bans stray agent
tool-call markup (a leaked closing/opening tool-call tag on its own line)
committed into a file.

Drives `violations()` directly (each sentinel variant member-by-member, the
false-positive carve-outs — inline mentions, inline-code spans, fenced blocks, a
bare content opener — the escape hatch) plus main()'s CLI contract and the real
script end-to-end, and asserts the committed tree is clean.

This file is excluded from the dogfood check-extras aggregate in
.pre-commit-config.yaml (see its exclude regex). Every fixture keeps its stray
tag inline in a single-line string literal, so no *physical* line of this test is
a bare tag; the `antml:`-prefixed tags are assembled from a separate `_A` piece so
the literal tag byte sequence — which is itself agent tool-call transport
scaffolding — never appears in the source.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, dogfood_extras_exclude, load_hook

_SRC = HOOKS_DIR / "check_stray_tool_markup.py"
mod = load_hook("check_stray_tool_markup.py", "check_stray_tool_markup")

# The transport prefix, kept in a variable so no complete `antml:`-prefixed tag
# literal (the real scaffolding this file must not emit) appears in the source.
_A = "antml:"


# -- non-vacuity: red on a leaked tag, green once removed ----------------------


def test_flags_then_clean() -> None:
    assert mod.violations("intro\n</invoke>\n") == [2]
    assert mod.violations("intro\nthe tool call is done\n") == []


# -- each plain sentinel variant, member-by-member -----------------------------

FLAGGED = {
    "close_invoke": "</invoke>",
    "open_invoke_attrs": '<invoke name="Write">',
    "open_invoke_bare": "<invoke>",
    "close_parameter": "</parameter>",
    "open_parameter_attrs": '<parameter name="content">',
    "open_function_calls": "<function_calls>",
    "close_function_calls": "</function_calls>",
    "close_content": "</content>",
    "indented_close_invoke": "   </invoke>",
    "trailing_ws_close_invoke": "</invoke>   ",
}


@pytest.mark.parametrize("tag", FLAGGED.values(), ids=list(FLAGGED))
def test_each_flagged_variant(tag: str) -> None:
    assert mod.violations(f"body line\n{tag}\n") == [2]


# -- the antml:-prefixed variants (assembled, never written literally) ---------

ANTML_FLAGGED = {
    "antml_close_invoke": f"</{_A}invoke>",
    "antml_open_invoke_attrs": f'<{_A}invoke name="Write">',
    "antml_close_parameter": f"</{_A}parameter>",
    "antml_open_parameter": f'<{_A}parameter name="content">',
    "antml_open_function_calls": f"<{_A}function_calls>",
    "antml_close_function_calls": f"</{_A}function_calls>",
    "antml_close_content": f"</{_A}content>",
}


@pytest.mark.parametrize("tag", ANTML_FLAGGED.values(), ids=list(ANTML_FLAGGED))
def test_each_antml_flagged_variant(tag: str) -> None:
    assert mod.violations(f"body line\n{tag}\n") == [2]


# -- false positives that must NOT be flagged ----------------------------------

CLEAN = {
    "inline_code_span": "The agent emits a `</invoke>` tag when the call ends.\n",
    "inline_prose": "Then the </invoke> tag closes the call, and we return.\n",
    "tag_with_trailing_text": "</invoke> and then the next thing happens\n",
    "tag_with_leading_text": "see the closing </invoke> at the end\n",
    "content_opener_allowed": "<content>\n",
    "antml_content_opener_allowed": f"<{_A}content>\n",
    "unrelated_html_close": "</div>\n",
    "unrelated_html_open": "<span>\n",
    "capitalized_jsx_component": "<Content>\n",
    "plain_prose": "the file-authoring call completed cleanly\n",
}


@pytest.mark.parametrize("body", CLEAN.values(), ids=list(CLEAN))
def test_no_false_positive(body: str) -> None:
    assert mod.violations(body) == []


# -- fenced code blocks are skipped --------------------------------------------


def test_fenced_block_is_skipped() -> None:
    body = "intro\n\n```\n</invoke>\n```\n\ndone\n"
    assert mod.violations(body) == []


def test_tilde_fence_is_skipped() -> None:
    body = "intro\n\n~~~\n</parameter>\n~~~\n\ndone\n"
    assert mod.violations(body) == []


def test_info_string_fence_is_skipped() -> None:
    body = "intro\n\n```xml\n<function_calls>\n```\n"
    assert mod.violations(body) == []


def test_tag_after_a_closed_fence_still_fires() -> None:
    body = "```\n</invoke>\n```\n</parameter>\n"
    assert mod.violations(body) == [4]


# -- escape hatch ---------------------------------------------------------------


def test_html_comment_allow_above_suppresses() -> None:
    body = "<!-- allow-stray-markup: literal example for the docs -->\n</invoke>\n"
    assert mod.violations(body) == []


def test_hash_comment_allow_above_suppresses() -> None:
    body = "# allow-stray-markup: fixture demonstrating the leak\n</invoke>\n"
    assert mod.violations(body) == []


def test_allow_marker_two_lines_above_does_not_suppress() -> None:
    body = "<!-- allow-stray-markup: too far -->\nfiller\n</invoke>\n"
    assert mod.violations(body) == [3]


# -- main(): CLI contract -------------------------------------------------------


def test_main_reports_path_line_and_remedy(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("line one\n</invoke>\n", encoding="utf-8")
    assert mod.main([str(doc)]) == 1
    err = capsys.readouterr().err
    assert f"{doc}:2:" in err
    assert "stray agent tool-call markup" in err


def test_main_returns_zero_on_clean_file(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("the tool call finished and wrote the file\n", encoding="utf-8")
    assert mod.main([str(doc)]) == 0
    assert capsys.readouterr().err == ""


def test_main_skips_an_unreadable_path(tmp_path: Path) -> None:
    assert mod.main([str(tmp_path / "absent.md")]) == 0


# -- end-to-end: the real CLI entrypoint ----------------------------------------


def test_cli_invocation_flags_and_exits_nonzero(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("body\n</content>\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SRC), str(doc)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert f"{doc}:2:" in proc.stderr


# -- the committed tree is clean ------------------------------------------------

_SCANNED_SUFFIXES = (".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".md", ".rst")


def test_repo_tree_is_clean() -> None:
    """Every tracked file of a scanned kind (minus the dogfood excludes, the one
    authoritative skip list) passes today. Non-vacuous: the flagged-variant cases
    above show `violations` fires on a real leaked tag."""
    exclude = dogfood_extras_exclude()
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    scanned = [
        str(REPO_ROOT / rel)
        for rel in tracked
        if rel and rel.endswith(_SCANNED_SUFFIXES) and not exclude.match(rel)
    ]
    assert scanned, "scope selection found nothing — the assertion would be vacuous"
    assert mod.main(scanned) == 0

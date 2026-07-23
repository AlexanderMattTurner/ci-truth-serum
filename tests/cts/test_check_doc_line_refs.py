"""Tests for ci_truth_serum/check_doc_line_refs.py — the lint that bans exact
line-number citations of source files in Markdown docs.

Drives `violations()` directly (each flagged form member-by-member, the
false-positive carve-outs, fences, the escape hatch) plus main()'s CLI contract
(CHANGELOG skip, unreadable skip, exit codes) and the real script end-to-end.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, dogfood_extras_exclude, load_hook

_SRC = HOOKS_DIR / "check_doc_line_refs.py"
mod = load_hook("check_doc_line_refs.py", "check_doc_line_refs")


# -- non-vacuity: red on a flagged ref, green once removed ---------------------


def test_flags_then_clean() -> None:
    hits = mod.violations("See `seed-user-overlay.sh:121-146` for the merge.\n")
    assert hits == [(1, "seed-user-overlay.sh:121-146")]
    assert (
        mod.violations("See the `.mcpServers` merge in seed-user-overlay.sh.\n") == []
    )


# -- each flagged form, member-by-member ---------------------------------------

FLAGGED = {
    "file_ext_single": (
        "cite `bin/lib/cli_entry.py:17` here\n",
        "bin/lib/cli_entry.py:17",
    ),
    "file_ext_range": ("cite `policy/seed.sh:92-111` here\n", "policy/seed.sh:92-111"),
    "paren_L_single": ("the merge (L102) touches only .mcpServers\n", "(L102)"),
    "paren_L_range": ("malformed-JSON tolerance (L98-110) holds\n", "(L98-110)"),
    "paren_L_range_single_digit": ("the guard (L2-9) holds\n", "(L2-9)"),
    "tilde_L": ("after the monitor-port rule (~L660) add the ACCEPT\n", "~L660"),
    "tilde_L_range": ("the block ~L92-111 does the rewrite\n", "~L92-111"),
    "tilde_colon": ("in agent-entrypoint.sh ~:762 the scrub runs\n", "~:762"),
    "bare_range": ("the malformed-JSON tolerance L98-110 holds\n", "L98-110"),
}


@pytest.mark.parametrize("line, match", FLAGGED.values(), ids=list(FLAGGED))
def test_each_flagged_form(line: str, match: str) -> None:
    assert mod.violations(line) == [(1, match)]


# -- false positives that must NOT be flagged ----------------------------------

CLEAN = {
    "https_port": "reach the gateway at https://gateway.example:8080/mcp\n",
    "localhost_port": "the proxy listens on localhost:3128\n",
    "timestamp": "logged at 10:00:00 in the audit trail\n",
    "ip_address": "the firewall sits at 172.30.0.2 in the netns\n",
    "osi_layer_paren": "the allow-probe does a bare TCP connect (L4) instead\n",
    "defense_layer_bare": "PromptArmor L5 is extracted to its own module\n",
    "md_anchor": "see the [placement decision](#placement) section\n",
    "chmod_octal": "the entrypoint hardens the key to chmod 0644\n",
    "plain_file_ref": "extend the merge in `seed-user-overlay.sh` as needed\n",
    "fileext_inside_url": "browse https://example.com/tree/foo.py:42 for the source\n",
}


@pytest.mark.parametrize("line", CLEAN.values(), ids=list(CLEAN))
def test_no_false_positive(line: str) -> None:
    assert mod.violations(line) == []


# -- fenced code blocks are skipped --------------------------------------------


def test_fenced_code_block_is_skipped() -> None:
    body = "intro\n\n```\ngrep -n foo bar.py:42\n```\n\ndone\n"
    assert mod.violations(body) == []


def test_ref_after_a_closed_fence_still_fires() -> None:
    body = "```\nbar.py:42\n```\ncite `foo.sh:12` here\n"
    assert mod.violations(body) == [(4, "foo.sh:12")]


# -- escape hatch ---------------------------------------------------------------


def test_allow_marker_same_line_suppresses() -> None:
    line = "cite `foo.sh:12-34` <!-- allow-line-ref: stable generated banner -->\n"
    assert mod.violations(line) == []


def test_allow_marker_line_above_suppresses() -> None:
    body = "<!-- allow-line-ref: pinned to a tagged release -->\ncite `foo.sh:12-34`\n"
    assert mod.violations(body) == []


def test_allow_marker_requires_reason() -> None:
    line = "cite `foo.sh:12-34` <!-- allow-line-ref: -->\n"
    assert mod.violations(line) == [(1, "foo.sh:12-34")]


def test_allow_marker_two_lines_above_does_not_suppress() -> None:
    body = "<!-- allow-line-ref: too far away -->\nfiller\ncite `foo.sh:12-34` here\n"
    assert mod.violations(body) == [(3, "foo.sh:12-34")]


# -- main(): CLI contract --------------------------------------------------------


def test_main_reports_path_line_match_and_remedy(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("line one\nline two `foo.sh:12` here\n", encoding="utf-8")
    assert mod.main([str(doc)]) == 1
    err = capsys.readouterr().err
    assert f"{doc}:2: `foo.sh:12`" in err
    assert "line number" in err


def test_main_returns_zero_on_clean_file(tmp_path: Path, capsys) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("a durable pointer to `foo.sh` never rots\n", encoding="utf-8")
    assert mod.main([str(doc)]) == 0
    assert capsys.readouterr().err == ""


def test_main_skips_changelog_by_basename(tmp_path: Path) -> None:
    """Released changelog entries are an immutable audit record — a CHANGELOG.md
    is skipped wherever it lives, even when it cites lines."""
    for rel in ("CHANGELOG.md", "docs/CHANGELOG.md"):
        log = tmp_path / rel
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("a bare TCP connect (L4) instead of `foo.py:12`\n", "utf-8")
    assert mod.main([str(tmp_path / "CHANGELOG.md")]) == 0
    assert mod.main([str(tmp_path / "docs" / "CHANGELOG.md")]) == 0


def test_main_skips_an_unreadable_path(tmp_path: Path) -> None:
    assert mod.main([str(tmp_path / "absent.md")]) == 0


# -- end-to-end: the real CLI entrypoint ----------------------------------------


def test_cli_invocation_flags_and_exits_nonzero(tmp_path: Path) -> None:
    doc = tmp_path / "x.md"
    doc.write_text("cite `foo.sh:12-34` here\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SRC), str(doc)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert f"{doc}:1: `foo.sh:12-34`" in proc.stderr


def test_enforced_scope_is_clean() -> None:
    """Every tracked Markdown file (minus the dogfood excludes, the one
    authoritative skip list) passes today; main() itself skips CHANGELOG.md.
    Non-vacuous: the flagged-form cases above show `violations` fires."""
    exclude = dogfood_extras_exclude()
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    scanned = [
        str(REPO_ROOT / rel) for rel in tracked if rel and not exclude.match(rel)
    ]
    assert scanned, "scope selection found nothing — the assertion would be vacuous"
    assert mod.main(scanned) == 0

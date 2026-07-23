"""Tests for ci_truth_serum/check_pin_comment_truth.py — the lint that keeps `# vX.Y`
comments on SHA-pinned `uses:` lines present, wellformed, and consistent for a
given `owner/repo@sha` across the whole repo.

Drives ``pin_records()`` for line parsing, ``check_files()`` for the cross-file
rules, and ``main()`` for discovery and the exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_pin_comment_truth.py", "check_pin_comment_truth")

SHA = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
SHA2 = "aabbccddeeff00112233445566778899aabbccdd"


def _msgs(*texts: str) -> list[str]:
    return [
        m
        for _p, _l, m in mod.check_files(
            [(f"f{i}.yaml", t) for i, t in enumerate(texts)]
        )
    ]


# ── pin_records ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "line, version, opted",
    [
        (f"      - uses: actions/checkout@{SHA} # v6", "v6", False),
        (f"      - uses: actions/checkout@{SHA} # v6.0.2", "v6.0.2", False),
        (
            f"      - uses: actions/checkout@{SHA} # v6.0.2 # zizmor: ignore[x]",
            "v6.0.2",
            False,
        ),
        (f"      - uses: actions/checkout@{SHA}", None, False),
        (f"      - uses: actions/checkout@{SHA} # latest", None, False),
        (f"      - uses: actions/checkout@{SHA} # version 6", None, False),
        (f"      - uses: actions/checkout@{SHA} # pin-comment-ok", None, True),
        (f"      - uses: actions/checkout@{SHA} # v6 pin-comment-ok", "v6", True),
    ],
)
def test_pin_records_parsing(line: str, version: str | None, opted: bool) -> None:
    assert mod.pin_records(line + "\n") == [
        (1, f"actions/checkout@{SHA}", version, opted)
    ]


def test_pin_records_subpath_action_keeps_full_ref() -> None:
    assert mod.pin_records(f"        uses: actions/cache/restore@{SHA} # v4\n") == [
        (1, f"actions/cache/restore@{SHA}", "v4", False)
    ]


@pytest.mark.parametrize(
    "line",
    [
        "      - uses: actions/checkout@v6",  # tag pin — zizmor's job, not ours
        f"      # uses: actions/checkout@{SHA}",  # commented out
        f"      - uses: actions/checkout@{SHA[:12]}",  # short SHA is not a 40-hex pin
        "      - run: echo uses actions/checkout",  # not a uses: key
    ],
)
def test_non_sha_or_commented_lines_yield_no_record(line: str) -> None:
    assert mod.pin_records(line + "\n") == []


# ── check_files rules ────────────────────────────────────────────────────
def test_missing_comment_is_flagged() -> None:
    msgs = _msgs(f"steps:\n  - uses: actions/checkout@{SHA}\n")
    assert len(msgs) == 1 and "no wellformed version comment" in msgs[0]


def test_malformed_comment_is_flagged_as_missing() -> None:
    msgs = _msgs(f"  - uses: actions/checkout@{SHA} # latest and greatest\n")
    assert len(msgs) == 1 and "no wellformed version comment" in msgs[0]


def test_conflicting_comments_flagged_at_every_occurrence() -> None:
    found = mod.check_files(
        [
            ("a.yaml", f"  - uses: actions/checkout@{SHA} # v6\n"),
            ("b.yaml", f"  - uses: actions/checkout@{SHA} # v7.0.0\n"),
        ]
    )
    assert len(found) == 2
    assert all("conflicting version comments" in m for _p, _l, m in found)
    assert all("'v6'" in m and "'v7.0.0'" in m for _p, _l, m in found)


def test_conflict_detected_within_one_file_too() -> None:
    text = (
        f"  - uses: actions/checkout@{SHA} # v6\n"
        f"  - uses: actions/checkout@{SHA} # v6.0.2\n"
    )
    assert len(_msgs(text)) == 2


# Legitimate corpus: consistent comments, distinct SHAs, trailing text, opt-outs.
def test_clean_corpus_yields_zero_findings() -> None:
    assert (
        _msgs(
            f"  - uses: actions/checkout@{SHA} # v6.0.2\n"
            f"  - uses: actions/checkout@{SHA} # v6.0.2 # zizmor: ignore[artipacked]\n"
            f"  - uses: astral-sh/setup-uv@{SHA2} # v8.3.2\n"
            "  - uses: actions/setup-node@v4\n",
            f"  - uses: actions/checkout@{SHA} # v6.0.2\n",
        )
        == []
    )


def test_same_owner_repo_different_shas_may_differ() -> None:
    assert (
        _msgs(
            f"  - uses: actions/setup-node@{SHA} # v6.4.0\n"
            f"  - uses: actions/setup-node@{SHA2} # v4\n"
        )
        == []
    )


def test_opted_out_line_is_neither_flagged_nor_a_conflict_source() -> None:
    assert (
        _msgs(
            f"  - uses: actions/checkout@{SHA} # v6\n"
            f"  - uses: actions/checkout@{SHA} # v9 pin-comment-ok\n"
            f"  - uses: x/y@{SHA2} # pin-comment-ok\n"
        )
        == []
    )


# ── main ─────────────────────────────────────────────────────────────────
def _wire(tmp_path, monkeypatch, text: str):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yaml").write_text(text)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")


def test_main_flags_and_locates(tmp_path, monkeypatch, capsys) -> None:
    _wire(tmp_path, monkeypatch, f"steps:\n  - uses: actions/checkout@{SHA}\n")
    assert mod.main() == 1
    assert "::error file=.github/workflows/ci.yaml,line=2::" in capsys.readouterr().out


def test_main_clean_repo_passes(tmp_path, monkeypatch) -> None:
    _wire(tmp_path, monkeypatch, f"steps:\n  - uses: actions/checkout@{SHA} # v6\n")
    assert mod.main() == 0

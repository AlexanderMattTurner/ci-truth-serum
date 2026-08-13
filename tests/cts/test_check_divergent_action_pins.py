"""Tests for ci_truth_serum/check_divergent_action_pins.py — the lint that keeps a
SHA-pinned GitHub Action pinned to ONE SHA repo-wide.

Drives ``pin_records()`` for line parsing, ``check_files()`` for the cross-file
divergence rule, and ``main()`` for discovery and the exit-code contract.
"""

from tests._helpers import load_hook

mod = load_hook("check_divergent_action_pins.py", "check_divergent_action_pins")

SHA_A = "de0fac2e4500dabe0009e67214ff5f5447ce83dd"
SHA_B = "0057852bfaa89a56745cba8c7296529d2fc39830"


def _msgs(*texts: str) -> list[str]:
    return [
        m
        for _p, _l, m in mod.check_files(
            [(f"f{i}.yaml", t) for i, t in enumerate(texts)]
        )
    ]


# ── pin_records ──────────────────────────────────────────────────────────
def test_pin_records_parsing() -> None:
    assert mod.pin_records(f"      - uses: actions/checkout@{SHA_A} # v6\n") == [
        (1, "actions/checkout", SHA_A, False)
    ]


def test_pin_records_opt_out() -> None:
    assert mod.pin_records(
        f"      - uses: actions/checkout@{SHA_A} # divergent-pin-ok\n"
    ) == [(1, "actions/checkout", SHA_A, True)]


def test_non_sha_or_commented_lines_yield_no_record() -> None:
    for line in (
        "      - uses: actions/checkout@v6",  # tag pin — zizmor's job, not ours
        f"      # uses: actions/checkout@{SHA_A}",  # commented out
        f"      - uses: actions/checkout@{SHA_A[:12]}",  # short SHA, not a 40-hex pin
        "      - run: echo uses actions/checkout",  # not a uses: key
        "      - uses: ./.github/actions/setup-base-env",  # local ref, no upstream
    ):
        assert mod.pin_records(line + "\n") == []


# ── check_files: the divergence rule ────────────────────────────────────
def test_same_action_two_shas_is_rejected() -> None:
    """An action pinned to two different SHAs across the repo is a divergent pin: a
    bump that updated only some call sites, or a stale `# vX` comment. Both refs
    are validly SHA-pinned, so only the convergence check can catch it."""
    msgs = _msgs(
        f"  - uses: actions/checkout@{SHA_A}\n  - uses: actions/checkout@{SHA_B}\n"
    )
    assert len(msgs) == 2
    assert all("divergent pin" in m and "actions/checkout" in m for m in msgs)


def test_same_action_same_sha_twice_passes() -> None:
    """Non-vacuity: repeating one action at ONE SHA (the normal case) must not trip
    the convergence check — it fires on divergence, not on repetition."""
    assert (
        _msgs(
            f"  - uses: actions/checkout@{SHA_A}\n  - uses: actions/checkout@{SHA_A}\n"
        )
        == []
    )


def test_distinct_actions_may_hold_distinct_shas() -> None:
    """The check keys on the action, not the SHA — two DIFFERENT actions at
    different SHAs is normal and must pass."""
    assert (
        _msgs(f"  - uses: actions/checkout@{SHA_A}\n  - uses: actions/cache@{SHA_B}\n")
        == []
    )


def test_divergence_is_found_across_files() -> None:
    """Divergence is a property of the whole tree, so two call sites in SEPARATE
    files must still be compared against each other."""
    msgs = _msgs(
        f"  - uses: actions/checkout@{SHA_A}\n", f"  - uses: actions/checkout@{SHA_B}\n"
    )
    assert len(msgs) == 2


def test_a_subpath_action_is_its_own_identity() -> None:
    """`actions/cache` and `actions/cache/restore` are different actions, so
    holding different SHAs is not a divergence."""
    assert (
        _msgs(
            f"  - uses: actions/cache@{SHA_A}\n  - uses: actions/cache/restore@{SHA_B}\n"
        )
        == []
    )


def test_opted_out_line_is_neither_flagged_nor_a_conflict_source() -> None:
    assert (
        _msgs(
            f"  - uses: actions/checkout@{SHA_A} # divergent-pin-ok\n"
            f"  - uses: actions/checkout@{SHA_B} # divergent-pin-ok\n"
        )
        == []
    )


def test_unpinned_ref_is_left_to_zizmor() -> None:
    """A tag-pinned ref is zizmor's finding, not this check's — reporting it here
    too would double-report one defect."""
    assert _msgs("  - uses: actions/checkout@v4\n") == []


# ── main ─────────────────────────────────────────────────────────────────
def _wire(tmp_path, monkeypatch, *files: tuple[str, str]):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, text in files:
        (wf / name).write_text(text)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")


def test_main_flags_and_locates(tmp_path, monkeypatch, capsys) -> None:
    _wire(
        tmp_path,
        monkeypatch,
        ("a.yaml", f"steps:\n  - uses: actions/checkout@{SHA_A}\n"),
        ("b.yaml", f"steps:\n  - uses: actions/checkout@{SHA_B}\n"),
    )
    assert mod.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/a.yaml,line=2::" in out
    assert "::error file=.github/workflows/b.yaml,line=2::" in out


def test_main_clean_repo_passes(tmp_path, monkeypatch) -> None:
    _wire(
        tmp_path,
        monkeypatch,
        ("ci.yaml", f"steps:\n  - uses: actions/checkout@{SHA_A}\n"),
    )
    assert mod.main() == 0


def test_composite_action_divergence_is_found(tmp_path, monkeypatch) -> None:
    """A composite action under .github/actions/ shares the repo-wide pin set, so a
    SHA there that disagrees with a workflow's is the same defect."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yaml").write_text(f"steps:\n  - uses: actions/checkout@{SHA_A}\n")
    actions = tmp_path / ".github" / "actions" / "my-action"
    actions.mkdir(parents=True)
    (actions / "action.yaml").write_text(
        f"runs:\n  using: composite\n  steps:\n    - uses: actions/checkout@{SHA_B}\n"
    )
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(mod, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert mod.main() == 1

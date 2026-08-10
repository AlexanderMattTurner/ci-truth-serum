"""Tests for ci_truth_serum/check_external_clock_targets.py — the static half of
the external-clock guard.

A host outside GitHub reads a manifest of workflow file names and dispatches each
with a bare `workflow_dispatch` POST. This check fails a manifest entry the clock
cannot fire: a missing workflow (404), a workflow with no `workflow_dispatch`
trigger (422), or one with a required default-less input (422). Each drops every
tick with no red mark, so the check is what makes the drop visible before it
ships.

Drives `parse_manifest()` / `_required_inputs_without_default()` /
`dispatch_defect()` / `violations()` for the rules, and `main()` for discovery,
the missing-manifest note, and the exit-code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_external_clock_targets.py", "check_external_clock_targets")

# A workflow that a bare workflow_dispatch POST can fire.
_FIREABLE = "on:\n  workflow_dispatch:\n  schedule:\n    - cron: '*/5 * * * *'\n"


# ── parse_manifest ────────────────────────────────────────────────────────
def test_parse_manifest_skips_comments_and_blanks_and_strips_whitespace() -> None:
    text = (
        "# a header comment\n"
        "\n"
        "rearm-auto-merge.yaml\n"
        "  spaced.yaml  \n"
        "trailing.yaml  # inline comment\n"
    )
    assert mod.parse_manifest(text) == [
        (3, "rearm-auto-merge.yaml"),
        (4, "spaced.yaml"),
        (5, "trailing.yaml"),
    ]


def test_parse_manifest_reads_a_final_entry_with_no_trailing_newline() -> None:
    # The dispatcher reads this entry (`|| [[ -n "$line" ]]`); the check must too,
    # or it would verify one fewer workflow than the clock fires.
    assert mod.parse_manifest("only.yaml") == [(1, "only.yaml")]


# ── _required_inputs_without_default ──────────────────────────────────────
@pytest.mark.parametrize(
    "text, expected",
    [
        ("on:\n  workflow_dispatch:\n", []),  # bare, no inputs
        (
            "on:\n  workflow_dispatch:\n    inputs:\n      ref:\n"
            "        required: true\n",
            ["ref"],
        ),
        (
            "on:\n  workflow_dispatch:\n    inputs:\n      ref:\n"
            "        required: true\n        default: main\n",
            [],  # a default means the bare POST supplies nothing and still works
        ),
        (
            "on:\n  workflow_dispatch:\n    inputs:\n      ref:\n"
            "        required: false\n",
            [],
        ),
        ("on: [push, workflow_dispatch]\n", []),  # list form carries no inputs
    ],
)
def test_required_inputs_without_default(text: str, expected: list) -> None:
    import yaml

    assert mod._required_inputs_without_default(yaml.safe_load(text)) == expected


# ── dispatch_defect ───────────────────────────────────────────────────────
def test_dispatch_defect_none_for_a_fireable_workflow() -> None:
    import yaml

    assert mod.dispatch_defect(yaml.safe_load(_FIREABLE)) is None


def test_dispatch_defect_flags_a_missing_trigger() -> None:
    import yaml

    defect = mod.dispatch_defect(
        yaml.safe_load("on:\n  schedule:\n    - cron: '5 * * * *'\n")
    )
    assert (
        defect is not None
        and "no `workflow_dispatch` trigger" in defect
        and "422" in defect
    )


def test_dispatch_defect_flags_a_required_default_less_input() -> None:
    import yaml

    doc = yaml.safe_load(
        "on:\n  workflow_dispatch:\n    inputs:\n      target:\n        required: true\n"
    )
    defect = mod.dispatch_defect(doc)
    assert defect is not None and "target" in defect and "422" in defect


def test_dispatch_defect_handles_the_bareword_on_key() -> None:
    # PyYAML resolves the bareword key `on:` to True; the check must still see the
    # trigger through workflow_triggers, not miss it as absent.
    import yaml

    doc = yaml.safe_load("on:\n  workflow_dispatch:\n")
    assert True in doc  # the parse really did fold `on` to True
    assert mod.dispatch_defect(doc) is None


# ── violations ────────────────────────────────────────────────────────────
def _resolve(mapping: dict):
    return lambda name: mapping.get(name)


def test_violations_empty_when_every_entry_is_fireable() -> None:
    found = mod.violations(
        "a.yaml\nb.yaml\n", _resolve({"a.yaml": _FIREABLE, "b.yaml": _FIREABLE})
    )
    assert found == []


def test_violations_flags_a_missing_workflow_with_its_line() -> None:
    found = mod.violations(
        "present.yaml\ngone.yaml\n", _resolve({"present.yaml": _FIREABLE})
    )
    assert len(found) == 1
    line, message = found[0]
    assert line == 2 and "gone.yaml" in message and "404" in message


def test_violations_flags_a_workflow_without_dispatch() -> None:
    found = mod.violations("x.yaml\n", _resolve({"x.yaml": "on:\n  push:\n"}))
    assert len(found) == 1 and "422" in found[0][1]


def test_violations_flags_a_required_default_less_input() -> None:
    doc = "on:\n  workflow_dispatch:\n    inputs:\n      ref:\n        required: true\n"
    found = mod.violations("x.yaml\n", _resolve({"x.yaml": doc}))
    assert len(found) == 1 and "ref" in found[0][1]


def test_violations_reports_unparseable_workflow_rather_than_passing_it() -> None:
    # Fail closed: an unparseable target cannot be confirmed dispatchable.
    found = mod.violations("x.yaml\n", _resolve({"x.yaml": "on: [unterminated\n"}))
    assert len(found) == 1 and "did not parse as YAML" in found[0][1]


# ── main ──────────────────────────────────────────────────────────────────
def _repo(tmp_path, monkeypatch, workflows: dict, manifest: "str | None"):
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    for name, text in workflows.items():
        (wf_dir / name).write_text(text)
    if manifest is not None:
        sched = tmp_path / ".github" / "scheduler"
        sched.mkdir(parents=True)
        (sched / "sweeps.txt").write_text(manifest)
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf_dir)


def test_main_passes_when_the_manifest_is_fireable(tmp_path, monkeypatch) -> None:
    _repo(tmp_path, monkeypatch, {"sweep.yaml": _FIREABLE}, "sweep.yaml\n")
    assert mod.main([]) == 0


def test_main_fails_and_names_the_bad_entry(tmp_path, monkeypatch, capsys) -> None:
    _repo(tmp_path, monkeypatch, {"sweep.yaml": _FIREABLE}, "sweep.yaml\ntypo.yaml\n")
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "::error file=" in out and "typo.yaml" in out and "line=2" in out


def test_main_with_no_manifest_scans_nothing(tmp_path, monkeypatch, capsys) -> None:
    _repo(tmp_path, monkeypatch, {"sweep.yaml": _FIREABLE}, None)
    assert mod.main([]) == 0
    assert "scanned nothing" in capsys.readouterr().err


def test_main_with_a_comment_only_manifest_passes(tmp_path, monkeypatch) -> None:
    _repo(
        tmp_path, monkeypatch, {"sweep.yaml": _FIREABLE}, "# nothing dispatched yet\n"
    )
    assert mod.main([]) == 0


def test_main_honors_a_custom_manifest_path(tmp_path, monkeypatch, capsys) -> None:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "sweep.yaml").write_text(_FIREABLE)
    (tmp_path / "clocks.txt").write_text("missing.yaml\n")
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mod, "WORKFLOWS_DIR", wf_dir)
    assert mod.main(["--manifest", "clocks.txt"]) == 1
    assert "missing.yaml" in capsys.readouterr().out

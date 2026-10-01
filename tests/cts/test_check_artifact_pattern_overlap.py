"""Tests for ci_truth_serum/check_artifact_pattern_overlap.py — the workflow lint
that refuses a download `pattern:` matching two upload families in one run.

Ported from agent-glovebox's `test_workflow_artifact_patterns.py`. The defect it
generalizes: shards uploaded `pytest-durations-<n>`, a later step uploaded the
shared `pytest-durations-map`, and `pattern: pytest-durations-*` merged all of
them into one directory, where the map read as a sixth shard's output.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_artifact_pattern_overlap.py", "check_artifact_pattern_overlap")

SHARDS = (
    "  shard:\n"
    "    strategy:\n"
    "      matrix:\n"
    "        n: [1, 2, 3]\n"
    "    steps:\n"
    "      - uses: actions/upload-artifact@v4\n"
    "        with:\n"
    "          name: pytest-durations-${{ matrix.n }}\n"
)
MAP = (
    "  map:\n"
    "    steps:\n"
    "      - uses: actions/upload-artifact@v4\n"
    "        with:\n"
    "          name: pytest-durations-map\n"
)
GATE = (
    "  gate:\n"
    "    steps:\n"
    "      - uses: actions/download-artifact@v4\n"
    "        with:\n"
    "          pattern: pytest-durations-*\n"
    "          merge-multiple: true\n"
)

UPLOAD_WRAPPER = (
    "inputs:\n"
    "  name:\n"
    "    required: true\n"
    "runs:\n"
    "  using: composite\n"
    "  steps:\n"
    "    - uses: actions/upload-artifact@v4\n"
    "      with:\n"
    "        name: ${{ inputs.name }}\n"
    "    - uses: actions/upload-artifact@v4\n"
    "      with:\n"
    "        name: ${{ inputs.retry == 'true' && format('{0}-r', inputs.name) || inputs.name }}\n"
)
DOWNLOAD_WRAPPER = (
    "inputs:\n"
    "  pattern:\n"
    "    default: ''\n"
    "runs:\n"
    "  using: composite\n"
    "  steps:\n"
    "    - uses: actions/download-artifact@v4\n"
    "      with:\n"
    "        pattern: ${{ inputs.pattern }}\n"
)


def _repo(tmp_path: Path, workflow: str, actions: dict[str, str] | None = None) -> Path:
    wf_dir = tmp_path / ".github" / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "ci.yaml").write_text(workflow, encoding="utf-8")
    for name, body in (actions or {}).items():
        action_dir = tmp_path / ".github" / "actions" / name
        action_dir.mkdir(parents=True, exist_ok=True)
        (action_dir / "action.yaml").write_text(body, encoding="utf-8")
    return tmp_path


def _found(tmp_path: Path, workflow: str, actions: dict[str, str] | None = None):
    root = _repo(tmp_path, workflow, actions)
    return mod.violations(workflow, root)


def _jobs(*blocks: str) -> str:
    return "on: push\njobs:\n" + "".join(blocks)


# ── the defect the check exists for ──────────────────────────────────────


def test_a_pattern_over_a_shard_family_and_a_map_is_flagged(tmp_path):
    found = _found(tmp_path, _jobs(SHARDS, MAP, GATE))
    assert len(found) == 1
    line, message = found[0]
    assert line == 18
    assert "`pytest-durations-*`" in message
    assert "['pytest-durations-*', 'pytest-durations-map']" in message
    assert f"# {mod.OPT_OUT}: <reason>" in message


def test_a_matrix_family_alone_is_one_family(tmp_path):
    assert _found(tmp_path, _jobs(SHARDS, GATE)) == []


def test_a_second_upload_outside_the_prefix_is_clean(tmp_path):
    other = MAP.replace("pytest-durations-map", "durations-map")
    assert _found(tmp_path, _jobs(SHARDS, other, GATE)) == []


def test_one_name_uploaded_from_two_jobs_is_one_family(tmp_path):
    again = MAP.replace("  map:", "  map2:")
    assert _found(tmp_path, _jobs(MAP, again, GATE)) == []


def test_an_expression_the_pattern_does_not_cover_stays_silent(tmp_path):
    """`${{ matrix.os }}-durations-x` could expand into the prefix, but the
    check cannot know its values, so it does not guess."""
    odd = MAP.replace("pytest-durations-map", "${{ matrix.os }}-durations-x")
    assert _found(tmp_path, _jobs(SHARDS, odd, GATE)) == []


def test_a_name_that_is_only_an_expression_is_no_family(tmp_path):
    whole = MAP.replace("pytest-durations-map", "${{ matrix.artifact }}")
    star = GATE.replace("pytest-durations-*", "'*'")
    assert _found(tmp_path, _jobs(SHARDS, whole, star)) == []


def test_an_upload_with_no_name_is_the_default_artifact_family(tmp_path):
    bare = "  bare:\n    steps:\n      - uses: actions/upload-artifact@v4\n"
    star = GATE.replace("pytest-durations-*", "'*'")
    found = _found(tmp_path, _jobs(SHARDS, bare, star))
    assert len(found) == 1
    assert "'artifact'" in found[0][1]


def test_uploads_in_another_workflow_file_do_not_count(tmp_path):
    root = _repo(tmp_path, _jobs(SHARDS, GATE))
    (root / ".github" / "workflows" / "other.yaml").write_text(
        _jobs(MAP), encoding="utf-8"
    )
    assert mod.violations(_jobs(SHARDS, GATE), root) == []


# ── downloads the check cannot place in this run ─────────────────────────


def test_a_pattern_holding_an_expression_is_skipped(tmp_path):
    computed = GATE.replace("pytest-durations-*", "${{ inputs.pattern }}")
    assert _found(tmp_path, _jobs(SHARDS, MAP, computed)) == []


@pytest.mark.parametrize(
    ("run_id", "flagged"),
    [("${{ github.event.workflow_run.id }}", False), ("${{ github.run_id }}", True)],
)
def test_run_id_decides_whose_uploads_the_pattern_reads(tmp_path, run_id, flagged):
    gate = GATE + f"          run-id: {run_id}\n"
    assert bool(_found(tmp_path, _jobs(SHARDS, MAP, gate))) is flagged


# ── local composite actions expand at the call site ──────────────────────


def test_an_upload_through_a_local_wrapper_is_a_family(tmp_path):
    wrapped = (
        "  map:\n"
        "    steps:\n"
        "      - uses: ./.github/actions/upload-retry\n"
        "        with:\n"
        "          name: pytest-durations-map\n"
    )
    found = _found(
        tmp_path, _jobs(SHARDS, wrapped, GATE), {"upload-retry": UPLOAD_WRAPPER}
    )
    assert len(found) == 1
    assert "['pytest-durations-*', 'pytest-durations-map']" in found[0][1]


def test_a_download_through_a_two_hop_wrapper_reports_the_caller_step(tmp_path):
    outer = (
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - uses: ./.github/actions/download-retry\n"
        "      with:\n"
        "        pattern: pytest-durations-*\n"
    )
    gate = "  gate:\n    steps:\n      - run: echo hi\n      - uses: ./.github/actions/shard\n"
    actions = {"download-retry": DOWNLOAD_WRAPPER, "shard": outer}
    found = _found(tmp_path, _jobs(SHARDS, MAP, gate), actions)
    assert [line for line, _ in found] == [19]


def test_a_composite_input_default_names_the_artifact(tmp_path):
    fixed = (
        "inputs:\n"
        "  suffix:\n"
        "    default: map\n"
        "runs:\n"
        "  using: composite\n"
        "  steps:\n"
        "    - uses: actions/upload-artifact@v4\n"
        "      with:\n"
        "        name: pytest-durations-${{ inputs.suffix }}\n"
    )
    call = "  map:\n    steps:\n      - uses: ./.github/actions/publish-map\n"
    found = _found(tmp_path, _jobs(SHARDS, call, GATE), {"publish-map": fixed})
    assert "'pytest-durations-map'" in found[0][1]


def test_a_missing_or_cyclic_local_action_yields_nothing(tmp_path):
    loop = "runs:\n  using: composite\n  steps:\n    - uses: ./.github/actions/loop\n"
    calls = (
        "  odd:\n    steps:\n"
        "      - uses: ./.github/actions/loop\n"
        "      - uses: ./.github/actions/absent\n"
    )
    assert _found(tmp_path, _jobs(SHARDS, calls, GATE), {"loop": loop}) == []


# ── each upload spelling contributes a family on its own (non-vacuity) ────


@pytest.mark.parametrize(
    ("second", "actions"),
    [
        (MAP, {}),
        (
            "  map:\n    steps:\n      - uses: ./.github/actions/up\n"
            "        with:\n          name: pytest-durations-map\n",
            {"up": UPLOAD_WRAPPER},
        ),
        (
            "  map:\n    steps:\n      - uses: actions/upload-artifact@v4\n",
            {},
        ),
    ],
    ids=["direct", "wrapped", "default-name"],
)
def test_each_upload_spelling_flips_the_verdict(tmp_path, second, actions):
    pattern = "'*'" if "with" not in second else "pytest-durations-*"
    gate = GATE.replace("pytest-durations-*", pattern)
    assert _found(tmp_path, _jobs(SHARDS, gate), actions) == []
    assert len(_found(tmp_path, _jobs(SHARDS, second, gate), actions)) == 1


# ── text that only names an upload is not an upload (probes) ──────────────


def test_an_upload_spelled_in_a_run_message_or_heredoc_is_not_an_upload(tmp_path):
    talk = (
        "  talk:\n"
        "    steps:\n"
        "      - run: |\n"
        '          echo "uses: actions/upload-artifact with name: pytest-durations-map"\n'
        "          cat <<'EOF' > step.yaml\n"
        "          - uses: actions/upload-artifact@v4\n"
        "            with:\n"
        "              name: pytest-durations-map\n"
        "          EOF\n"
    )
    assert _found(tmp_path, _jobs(SHARDS, talk, GATE)) == []


# ── opt-out ──────────────────────────────────────────────────────────────


def test_an_opt_out_with_a_reason_clears_the_finding(tmp_path):
    gate = GATE.replace(
        "pytest-durations-*\n",
        f"pytest-durations-*  # {mod.OPT_OUT}: the reader skips the map\n",
    )
    assert _found(tmp_path, _jobs(SHARDS, MAP, gate)) == []


def test_an_opt_out_without_a_reason_does_not_clear_it(tmp_path):
    gate = GATE.replace(
        "pytest-durations-*\n", f"pytest-durations-*  # {mod.OPT_OUT}\n"
    )
    assert len(_found(tmp_path, _jobs(SHARDS, MAP, gate))) == 1


def test_an_opt_out_inside_a_quoted_value_does_not_clear_it(tmp_path):
    gate = GATE.replace(
        "      - uses: actions/download-artifact@v4\n",
        f'      - name: "fetch  # {mod.OPT_OUT}: not a comment"\n'
        "        uses: actions/download-artifact@v4\n",
    )
    assert len(_found(tmp_path, _jobs(SHARDS, MAP, gate))) == 1


def test_an_opt_out_in_the_next_job_does_not_reach_back(tmp_path):
    after = f"  later:\n    # {mod.OPT_OUT}: about this job\n    steps:\n      - run: true\n"
    assert len(_found(tmp_path, _jobs(SHARDS, MAP, GATE, after))) == 1


# ── parse failures and the command line ──────────────────────────────────


def test_unparseable_yaml_is_reported_not_passed(tmp_path):
    found = mod.violations("jobs: [unclosed\n", tmp_path)
    assert [line for line, _ in found] == [1]
    assert "could not parse as YAML" in found[0][1]


def test_main_exits_one_and_prints_an_annotation(tmp_path, capsys):
    root = _repo(tmp_path, _jobs(SHARDS, MAP, GATE))
    with pytest.raises(SystemExit) as stop:
        mod.main(["--repo-root", str(root)])
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/ci.yaml,line=18::" in out


def test_main_returns_quietly_when_clean(tmp_path, capsys):
    root = _repo(tmp_path, _jobs(SHARDS, GATE))
    mod.main(["--repo-root", str(root), "ignored.yaml"])
    assert "ERROR" not in capsys.readouterr().out

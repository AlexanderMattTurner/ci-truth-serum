"""Tests for hooks/check_required_reporter.py — the (opinionated) pre-commit
lint that makes every always() reporter on a gated workflow declare, in a
classification comment, whether it is a required status check.

Drives the module's functions directly so every branch (PR-trigger detection,
opt-out, reporter discovery, job-block carving, the true/false/unclassified
classification grammar, the YAML-shape guards, and main()'s exit code) is
asserted in isolation.
"""

from pathlib import Path

from tests._helpers import load_hook

crr = load_hook("check_required_reporter.py", "check_required_reporter")


def _write(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body)
    return path


# ── check_file ────────────────────────────────────────────────────────────

REQUIRED_TRAILING = """\
name: x
on:
  pull_request:
jobs:
  work:
    runs-on: ubuntu-latest
  report:  # required-check: true
    needs: [work]
    if: always()
    runs-on: ubuntu-latest
"""

ADVISORY_WITH_REASON = """\
name: x
on:
  pull_request:
jobs:
  work:
    runs-on: ubuntu-latest
  cleanup:
    needs: [work]
    if: always()
    # required-check: false  # cleanup only, never gates a merge
    runs-on: ubuntu-latest
"""

UNCLASSIFIED = """\
name: x
on:
  pull_request:
jobs:
  work:
    runs-on: ubuntu-latest
  report:
    needs: [work]
    if: always()
    runs-on: ubuntu-latest
"""

ADVISORY_NO_REASON = """\
name: x
on:
  pull_request:
jobs:
  report:
    if: always()  # required-check: false
    runs-on: ubuntu-latest
"""

NO_REPORTER = """\
name: x
on:
  pull_request:
jobs:
  build:
    runs-on: ubuntu-latest
"""

OPT_OUT_YAML = f"""\
name: x
on:
  pull_request:  # {crr.OPT_OUT}
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
"""

NO_PR_TRIGGER = """\
name: x
on:
  push:
    branches: [main]
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
"""

PR_TARGET_UNCLASSIFIED = """\
name: x
on:
  pull_request_target:
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
"""

PR_TARGET_OPT_OUT = f"""\
name: x
on:
  pull_request_target:  # {crr.OPT_OUT}
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
"""

TWO_REPORTERS = """\
name: x
on:
  pull_request:
jobs:
  report:  # required-check: true
    if: always()
    runs-on: ubuntu-latest
  audit:
    if: always()
    runs-on: ubuntu-latest
"""


def test_passes_required_trailing_comment(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", REQUIRED_TRAILING)) == []


def test_passes_advisory_with_reason(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", ADVISORY_WITH_REASON)) == []


def test_flags_unclassified_reporter(tmp_path):
    found = crr.check_file(_write(tmp_path, "wf.yaml", UNCLASSIFIED))
    assert len(found) == 1
    line, message = found[0]
    assert line == 7  # `report:` key line
    assert "unclassified" in message
    assert crr.OPT_OUT in message


def test_flags_advisory_without_reason(tmp_path):
    found = crr.check_file(_write(tmp_path, "wf.yaml", ADVISORY_NO_REASON))
    assert len(found) == 1
    assert "no reason" in found[0][1]


def test_passes_workflow_without_reporter(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", NO_REPORTER)) == []


def test_respects_opt_out(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", OPT_OUT_YAML)) == []


def test_passes_no_pr_trigger(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", NO_PR_TRIGGER)) == []


def test_flags_pull_request_target(tmp_path):
    found = crr.check_file(_write(tmp_path, "wf.yaml", PR_TARGET_UNCLASSIFIED))
    assert len(found) == 1
    assert "unclassified" in found[0][1]


def test_respects_opt_out_on_pull_request_target(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", PR_TARGET_OPT_OUT)) == []


def test_flags_each_unclassified_reporter_independently(tmp_path):
    # One reporter is classified, the other is not — only the latter is flagged.
    found = crr.check_file(_write(tmp_path, "wf.yaml", TWO_REPORTERS))
    assert len(found) == 1
    line, message = found[0]
    assert line == 8  # `audit:` key line
    assert "audit" in message


MARKER_BURIED_IN_STEP = """\
name: x
on:
  pull_request:
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
    steps:
      - name: noise # required-check: true
        run: echo hi
"""

QUOTED_REPORTER_KEY = """\
name: x
on:
  pull_request:
jobs:
  "report": # required-check: true
    if: always()
    runs-on: ubuntu-latest
"""


def test_marker_inside_a_step_does_not_classify(tmp_path):
    # A `# required-check:` string buried in step content must NOT satisfy the
    # classification — only the key line and direct-child lines count.
    found = crr.check_file(_write(tmp_path, "wf.yaml", MARKER_BURIED_IN_STEP))
    assert len(found) == 1
    assert "unclassified" in found[0][1]


def test_quoted_reporter_key_is_classified(tmp_path):
    # The block carver strips surrounding quotes so the source key matches the
    # unquoted name PyYAML reports — otherwise the lookup misses and a classified
    # reporter is falsely flagged.
    assert crr.check_file(_write(tmp_path, "wf.yaml", QUOTED_REPORTER_KEY)) == []


def test_ignores_non_mapping_document(tmp_path):
    assert crr.check_file(_write(tmp_path, "wf.yaml", "- a\n- b\n")) == []


def test_ignores_non_mapping_triggers(tmp_path):
    # `on: push` — bareword `on` parses as True (YAML 1.1); value is a scalar.
    assert crr.check_file(_write(tmp_path, "wf.yaml", "on: push\n")) == []


def test_ignores_non_mapping_jobs(tmp_path):
    body = "on:\n  pull_request:\njobs: scalar-not-a-mapping\n"
    assert crr.check_file(_write(tmp_path, "wf.yaml", body)) == []


def test_ignores_non_dict_job_config(tmp_path):
    # A job whose value is a scalar (not a mapping) is not a reporter candidate.
    body = "on:\n  pull_request:\njobs:\n  weird: just-a-string\n"
    assert crr.check_file(_write(tmp_path, "wf.yaml", body)) == []


BOTH_PR_TRIGGERS = """\
name: x
on:
  pull_request:
  pull_request_target:
jobs:
  report:
    if: always()
    runs-on: ubuntu-latest
"""


def test_flags_both_pr_triggers(tmp_path):
    # Both triggers present: the loop visits both; the second iteration exercises
    # the branch where pr_line is already set.
    found = crr.check_file(_write(tmp_path, "wf.yaml", BOTH_PR_TRIGGERS))
    assert len(found) == 1
    assert "unclassified" in found[0][1]


def test_locate_trigger_fallback_line_for_flow_style_yaml(tmp_path):
    # Flow-style `on: {pull_request: null}` parses to the same structure as
    # block-style, but the regex `^\s*pull_request\s*:` won't match the
    # flow-style source line, so _locate_trigger falls back to line 1. The
    # block-style reporter is still discovered and flagged.
    body = (
        "on: {pull_request: null}\n"
        "jobs:\n"
        "  report:\n"
        "    if: 'always()'\n"
        "    runs-on: ubuntu-latest\n"
    )
    found = crr.check_file(_write(tmp_path, "wf.yaml", body))
    assert len(found) == 1
    line, message = found[0]
    assert line == 3  # the reporter's own key line, not the trigger fallback
    assert "unclassified" in message


def test_reporter_block_missing_falls_back_to_trigger_line(tmp_path):
    # Flow-style jobs: PyYAML still sees the always() reporter, but _job_blocks
    # (which scans block-style source) can't carve a block for it, so check_file
    # falls back to the pull_request trigger line and still flags it.
    body = "on:\n  pull_request:\njobs: {report: {if: 'always()', runs-on: x}}\n"
    found = crr.check_file(_write(tmp_path, "wf.yaml", body))
    assert len(found) == 1
    line, message = found[0]
    assert line == 2  # pull_request: trigger line
    assert "unclassified" in message


# ── _job_blocks ───────────────────────────────────────────────────────────


def test_job_blocks_returns_empty_without_jobs_key(tmp_path):
    assert crr._job_blocks("on:\n  pull_request:\n") == {}


def test_job_blocks_returns_empty_when_jobs_has_no_children(tmp_path):
    # `jobs:` is the last key and has only blank/comment lines under it.
    assert crr._job_blocks("jobs:\n  # nothing here\n") == {}


def test_job_blocks_skips_inter_job_comment_lines(tmp_path):
    # A comment line at the job indent is neither a key nor a body line — the
    # carver must step over it and still discover the job that follows.
    body = (
        "jobs:\n"
        "  # a comment between jobs: and the first job\n"
        "  report:\n"
        "    if: always()\n"
    )
    blocks = crr._job_blocks(body)
    assert set(blocks) == {"report"}
    assert blocks["report"][0] == 3  # `report:` key line


def test_job_blocks_stops_at_dedented_top_level_key(tmp_path):
    # A trailing top-level key after jobs: must not be swallowed into the block.
    body = "jobs:\n  report:\n    if: always()\ndefaults:\n  run:\n    shell: bash\n"
    blocks = crr._job_blocks(body)
    assert set(blocks) == {"report"}
    assert "defaults" not in blocks["report"][1]


# ── workflow_files ────────────────────────────────────────────────────────


def test_workflow_files_collects_workflows_and_actions(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    actions = tmp_path / ".github" / "actions"
    _write(wf, "a.yaml", "on:\n  push:\n")
    _write(wf, "b.yml", "on:\n  push:\n")
    _write(actions / "setup", "action.yaml", "name: s\n")
    monkeypatch.setattr(crr, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(crr, "ACTIONS_DIR", actions)
    files = crr.workflow_files()
    assert files == sorted(files)
    assert sorted(p.name for p in files) == ["a.yaml", "action.yaml", "b.yml"]


# ── main ──────────────────────────────────────────────────────────────────


def _point_at(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(crr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(crr, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(crr, "ACTIONS_DIR", tmp_path / "nonexistent")
    return wf


def test_main_returns_zero_when_clean(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "ok.yaml", REQUIRED_TRAILING)
    assert crr.main() == 0
    assert "ERROR" not in capsys.readouterr().out


def test_main_reports_and_fails_on_violation(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "bad.yaml", UNCLASSIFIED)
    _write(wf, "ok.yaml", REQUIRED_TRAILING)
    assert crr.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=7::" in out
    assert "1 violation(s) found" in out


def test_main_returns_zero_on_opt_out(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "opted-out.yaml", OPT_OUT_YAML)
    assert crr.main() == 0

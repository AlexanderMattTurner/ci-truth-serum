"""Tests for ci_truth_serum/check_workflow_pipefail.py — the pre-commit lint that bans an
exit-code-masking pipe in a GitHub Actions step whose shell lacks pipefail
(`runCmd:`, `shell: sh`, a custom non-pipefail `bash …`, or a `defaults.run`
override). A `cmd | tee log` there exits with tee's status, so a failing `cmd`
reports the required check GREEN — the exact obfuscation this guards against.

The module is loaded by path and its functions driven directly so every branch
(shell classification, pipe detection, the opt-out and pipefail escapes, step
iteration, the actions/-dir glob and main()'s exit code) is asserted in
isolation, with discovery redirected at the module dir constants so the real
repo's workflows never leak into a case.
"""

import subprocess
import sys
from pathlib import Path

import yaml

from tests._helpers import HOOKS_DIR, load_hook

SRC = HOOKS_DIR / "check_workflow_pipefail.py"
cwp = load_hook("check_workflow_pipefail.py", "check_workflow_pipefail")


def test_repo_root_is_the_current_working_directory():
    # The workflow lints anchor discovery at the repo being scanned: pre-commit runs
    # them from the consumer repo root, so REPO_ROOT is cwd and WORKFLOWS_DIR /
    # ACTIONS_DIR hang off it. (The other cases override these, which would mask a
    # bad module-level default.)
    assert cwp.REPO_ROOT == Path.cwd()
    assert cwp.WORKFLOWS_DIR == Path.cwd() / ".github" / "workflows"
    assert cwp.ACTIONS_DIR == Path.cwd() / ".github" / "actions"


def _analyze(text: str) -> list[str]:
    # analyze() returns (line, message); the detection tests assert on messages.
    return [msg for _line, msg in cwp.analyze(yaml.safe_load(text))]


def _write(dirpath: Path, name: str, body: str) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    path = dirpath / name
    path.write_text(body)
    return path


# ── _is_posix_shell ──────────────────────────────────────────────────────
def test_is_posix_shell_default_is_bash():
    assert cwp._is_posix_shell(None) is True


def test_is_posix_shell_empty_string_is_default_bash():
    assert cwp._is_posix_shell("   ") is True


def test_is_posix_shell_accepts_known_shells():
    assert cwp._is_posix_shell("bash") is True
    assert cwp._is_posix_shell("sh -e {0}") is True
    assert cwp._is_posix_shell("/usr/bin/zsh {0}") is True


def test_is_posix_shell_rejects_non_shells():
    assert cwp._is_posix_shell("python") is False
    assert cwp._is_posix_shell("pwsh -Command") is False
    assert cwp._is_posix_shell("node {0}") is False


# ── _shell_has_pipefail ──────────────────────────────────────────────────
def test_shell_has_pipefail_default_and_bare_bash():
    assert cwp._shell_has_pipefail(None) is True
    assert cwp._shell_has_pipefail("bash") is True


def test_shell_has_pipefail_explicit_flag():
    assert cwp._shell_has_pipefail("bash -eo pipefail {0}") is True


def test_shell_has_pipefail_false_for_plain_sh_and_custom_bash():
    assert cwp._shell_has_pipefail("sh") is False
    assert cwp._shell_has_pipefail("bash -e {0}") is False


def test_shell_has_pipefail_string_below_bash_is_not_pipefail():
    # `s == "bash"` must be an equality, not `s <= "bash"`: a shell name that sorts
    # lexicographically at or below "bash" but isn't it (and has no "pipefail") still
    # lacks pipefail. Kills the Eq->LtE mutant on the comparison.
    assert cwp._shell_has_pipefail("aaa") is False


# ── _pipeline_nodes (AST-based pipe detection) ───────────────────────────
def _pipe_lines(script: str) -> list[str]:
    """The source line where each detected pipeline begins — the observable the
    message quotes, reconstructed from the pipeline nodes."""
    lines = script.split("\n")
    return [
        lines[n.start_point[0]].strip() for n in cwp._pipeline_nodes(cwp.parse(script))
    ]


def test_pipelines_detects_real_pipe_only():
    # Only a genuine pipeline is a pipe: `||` (logical or) and a plain command are
    # not, and their source lines never appear.
    assert _pipe_lines("cat x | tee y\nfoo || bar\nbaz\n") == ["cat x | tee y"]


def test_pipelines_ignores_clobber_redirect_and_logical_or():
    assert _pipe_lines("echo hi >| file") == []
    assert _pipe_lines("foo || bar") == []


def test_pipelines_detects_fd_glued_and_pipe_amp():
    # `2>&1| tee` (FD redirect glued to the pipe) and `|&` are real masking pipes.
    assert _pipe_lines("cmd 2>&1| tee y") == ["cmd 2>&1| tee y"]
    assert _pipe_lines("cmd |& tee y") == ["cmd |& tee y"]


def test_pipelines_ignores_pipe_in_string_comment_and_heredoc():
    # A `|` inside a string, a `#` comment, or a heredoc body (quoted or not) is
    # data, not a pipeline — the grammar never yields a pipeline node for it.
    assert _pipe_lines('echo "a | b"') == []
    assert _pipe_lines("echo a  # b | c") == []
    assert _pipe_lines("cat <<EOF\na | b\nEOF\n") == []
    assert _pipe_lines("cat <<'EOF'\na | b\nEOF\n") == []


def test_pipelines_keeps_intro_pipe_and_drops_body_pipe():
    # A pipe on the heredoc INTRO line applies to the command and is reported by its
    # source line; the body's own `|` is dropped; a pipe after the body is real.
    assert _pipe_lines("cat <<EOF | tee\nbody | x\nEOF\nreal | tee\n") == [
        "cat <<EOF | tee",
        "real | tee",
    ]


def test_pipelines_reports_source_order():
    # Two pipes on distinct lines are returned first-to-last, so `_check_script`
    # quotes the FIRST — pins the order the message depends on.
    assert _pipe_lines("alpha | tee a\nbeta | tee b") == [
        "alpha | tee a",
        "beta | tee b",
    ]


# ── _first_pipefail_byte (AST-based `set -o pipefail` detection + order) ──
def test_first_pipefail_byte_set_for_a_real_set_command():
    assert cwp._first_pipefail_byte(cwp.parse("set -o pipefail\na | b")) == 0
    assert (
        cwp._first_pipefail_byte(cwp.parse("set -euo pipefail\na | b")) == 0
    )  # bundle


def test_first_pipefail_byte_none_for_disable_comment_and_heredoc_body():
    # `set +o pipefail` DISABLES it; a comment mention and a heredoc-body mention
    # are not commands — none must read as enabling pipefail.
    assert cwp._first_pipefail_byte(cwp.parse("set +o pipefail\na | b")) is None
    assert cwp._first_pipefail_byte(cwp.parse("# set -o pipefail\na | b")) is None
    assert (
        cwp._first_pipefail_byte(cwp.parse("cat <<EOF\nset -o pipefail\nEOF")) is None
    )


def test_pipefail_after_the_pipe_does_not_clear():
    # A `set -o pipefail` that runs AFTER the masking pipe protects nothing — the
    # step is still flagged (byte offset of the set command is > the pipe's).
    assert cwp._check_script("foo | tee log\nset -o pipefail", "sh", "job x (run)")
    # And a `set -o pipefail` BEFORE the pipe does clear it.
    assert (
        cwp._check_script("set -o pipefail\nfoo | tee log", "sh", "job x (run)") == []
    )


# ── _allow_optout (AST-based comment detection) ──────────────────────────
def test_allow_optout_true_only_for_a_real_shell_comment():
    assert cwp._allow_optout("cat x | tee y  # allow-no-pipefail: intended") is True
    # In a string / heredoc body the marker is data, not a comment — must not opt out.
    assert cwp._allow_optout('echo "allow-no-pipefail"\ncat x | tee y') is False
    assert (
        cwp._allow_optout("cat <<EOF\nallow-no-pipefail\nEOF\ncat x | tee y") is False
    )


# ── blind spots the old char-by-char tokenizer missed ────────────────────
# Each is red on the old quote/heredoc state machine (which desynced and lost the
# real pipe) and green on the bash grammar. Driven through _check_script so the
# assertion is on the observable violation, not an internal.
def test_blindspot_escaped_quote_pipe_is_now_flagged():
    # `echo "a\"" | tee y`: the old counter closed the string on the escaped `\"`,
    # so the real `| tee y` was swallowed as string data → FALSE NEGATIVE.
    assert cwp._check_script(r'echo "a\"" | tee y', "sh", "loc") != []


def test_blindspot_ansi_c_string_pipe_is_now_flagged():
    # `foo $'\'' | tee y`: `$'\''` is one ANSI-C literal quote; the old machine
    # read the escaped `\'` as a quote toggle and lost the pipe → FALSE NEGATIVE.
    assert cwp._check_script("foo $'\\'' | tee y", "sh", "loc") != []


def test_blindspot_nested_substitution_pipe_text_is_exact():
    # Nested `"` inside `$(…)` desynced the old machine, garbling the reported pipe
    # to `echo $f | tee y`. The grammar reports the true command verbatim.
    out = cwp._check_script('echo "$(cat "$f")" | tee y', "sh", "loc")
    assert len(out) == 1 and '$(cat "$f")' in out[0]


def test_blindspot_backtick_substitution_pipe_text_is_exact():
    out = cwp._check_script('x=`echo "a"` | tee y', "sh", "loc")
    assert len(out) == 1 and '`echo "a"`' in out[0]


# ── _default_shell ───────────────────────────────────────────────────────
def test_default_shell_finds_job_then_workflow():
    job = {"defaults": {"run": {"shell": "sh"}}}
    workflow = {"defaults": {"run": {"shell": "bash"}}}
    assert cwp._default_shell(job, workflow) == "sh"
    assert cwp._default_shell({}, workflow) == "bash"


def test_default_shell_skips_non_dict_scope_and_keeps_scanning():
    # The `continue` past a non-dict scope must NOT be a `break`: a malformed first
    # scope cannot abort the walk before a valid later scope sets the shell.
    assert (
        cwp._default_shell("notadict", {"defaults": {"run": {"shell": "sh"}}}) == "sh"
    )


def test_default_shell_none_when_unset_or_malformed():
    assert cwp._default_shell({}, {}) is None
    assert cwp._default_shell(None, "notadict") is None
    assert cwp._default_shell({"defaults": None}) is None
    assert cwp._default_shell({"defaults": {"run": None}}) is None
    assert cwp._default_shell({"defaults": {"run": {"shell": ["x"]}}}) is None


# ── _check_script ────────────────────────────────────────────────────────
def test_check_script_skips_non_string_and_non_shell():
    assert cwp._check_script(None, None, "loc") == []
    assert cwp._check_script("a | b", "python", "loc") == []


def test_check_script_safe_when_pipefail_present():
    assert cwp._check_script("a | b", "bash", "loc") == []  # shell has pipefail
    assert cwp._check_script("set -o pipefail\na | b", "sh", "loc") == []  # command
    assert cwp._check_script("set -euo pipefail\na | b", "sh", "loc") == []  # bundle
    assert cwp._check_script("a | b  # allow-no-pipefail: x", "sh", "loc") == []


def test_check_script_comment_mention_of_pipefail_does_not_whitelist():
    # A bare "pipefail" word in a comment is NOT `set -o pipefail`; the real pipe
    # below it must still be flagged.
    out = cwp._check_script("# no pipefail wanted here\ncat x | tee y", "sh", "loc")
    assert len(out) == 1 and "cat x | tee y" in out[0]


def test_check_script_set_plus_o_pipefail_does_not_whitelist():
    # `set +o pipefail` DISABLES pipefail — it must not be read as enabling it.
    out = cwp._check_script("set +o pipefail\ncat x | tee y", "sh", "loc")
    assert len(out) == 1


def test_check_script_pipefail_inside_heredoc_does_not_whitelist():
    # `set -o pipefail` in a heredoc BODY is data, not a command; the outer pipe
    # stays flagged.
    script = "cat <<EOF\nset -o pipefail\nEOF\nreal | tee y"
    out = cwp._check_script(script, "sh", "loc")
    assert len(out) == 1 and "real | tee y" in out[0]


def test_check_script_no_false_positive_on_multiline_quote_or_heredoc():
    assert cwp._check_script('echo "a | b\nc"', "sh", "loc") == []
    assert cwp._check_script("cat <<EOF\ndata | more\nEOF", "sh", "loc") == []


def test_check_script_no_false_positive_on_pipe_inside_quoted_heredoc():
    # A `|` inside a QUOTED-delimiter heredoc body is data, not a pipe — must not
    # fire. (Positive marker for the same fix lives in the flags test below.)
    assert cwp._check_script("cat <<'EOF'\ndata | more\nEOF", "sh", "loc") == []
    assert cwp._check_script('cat <<"EOF"\ndata | more\nEOF', "sh", "loc") == []


def test_check_script_flags_real_pipe_outside_quoted_heredoc():
    # The fix must still SEE a genuine masking pipe that sits AFTER a quoted heredoc:
    # dropping the body must not drop the code around it. Pairs with the negative
    # above so neither passes vacuously.
    out = cwp._check_script("cat <<'EOF'\na | b\nEOF\nreal | tee y", "sh", "loc")
    assert len(out) == 1 and "real | tee y" in out[0]


def test_allow_optout_only_counts_a_real_comment():
    # The opt-out must be a real `#` comment — not the marker buried in a string, a
    # piped command's data, or a heredoc body — else a spurious hit disables the
    # check and a real pipe sails through (fail-open).
    assert cwp._allow_optout("cat x | tee y  # allow-no-pipefail: intended") is True
    assert cwp._allow_optout('echo "allow-no-pipefail"\ncat x | tee y') is False
    assert (
        cwp._allow_optout("cat <<EOF\nallow-no-pipefail\nEOF\ncat x | tee y") is False
    )


def test_check_script_string_borne_optout_does_not_whitelist():
    # A quoted `allow-no-pipefail` (data, not a comment) must NOT excuse the real
    # pipe below it.
    out = cwp._check_script('echo "allow-no-pipefail"\ncat x | tee y', "sh", "loc")
    assert len(out) == 1 and "cat x | tee y" in out[0]


def test_check_script_comment_optout_whitelists():
    # Positive marker: the SAME marker in a real trailing comment does opt out, so
    # the negative above isn't passing because the marker is simply never honored.
    assert (
        cwp._check_script("cat x | tee y  # allow-no-pipefail: ok", "sh", "loc") == []
    )


def test_check_script_safe_when_no_pipe():
    assert cwp._check_script("echo hi\nfoo || bar", "sh", "loc") == []


def test_check_script_flags_unguarded_pipe():
    out = cwp._check_script("cat x | tee y", "sh", "job j (run)")
    assert len(out) == 1
    assert "job j (run): pipes (`cat x | tee y`)" in out[0]
    assert "set -o pipefail" in out[0]


def test_check_script_reports_the_first_pipe_line():
    # Two distinct pipe lines: the message must quote the FIRST, not the last —
    # pins the index so `pipes[0]` can't drift to `pipes[-1]`.
    out = cwp._check_script("alpha | tee a\nbeta | tee b", "sh", "loc")
    assert len(out) == 1
    assert "`alpha | tee a`" in out[0]
    assert "beta" not in out[0]


# ── _iter_steps ──────────────────────────────────────────────────────────
def test_iter_steps_non_list_is_empty():
    assert cwp._iter_steps(None, {}, {}) == []


def test_iter_steps_skips_non_dict_and_pure_uses_steps():
    steps = ["not-a-dict", {"uses": "actions/checkout@v4"}]
    assert cwp._iter_steps(steps, {}, {}) == []


def test_iter_steps_skips_non_dict_step_and_keeps_scanning():
    # The `continue` past a non-dict step must NOT be a `break`: a stray scalar in the
    # steps list cannot swallow a real run step that follows it.
    # Hand-built dicts carry no `__line__`, so the step line is None.
    steps = ["not-a-dict", {"run": "a | b", "shell": "sh"}]
    assert cwp._iter_steps(steps, {}, {}) == [(None, "a | b", "sh", "run")]


def test_iter_steps_extracts_runcmd_as_pipefail_less():
    steps = [{"with": {"runCmd": "bash x | tee y"}}]
    assert cwp._iter_steps(steps, {}, {}) == [(None, "bash x | tee y", "sh", "runCmd")]


def test_iter_steps_run_uses_step_shell_over_default():
    workflow = {"defaults": {"run": {"shell": "bash"}}}
    steps = [{"run": "a | b", "shell": "sh"}]
    assert cwp._iter_steps(steps, workflow, {}) == [(None, "a | b", "sh", "run")]


def test_iter_steps_run_falls_back_to_default_shell():
    workflow = {"defaults": {"run": {"shell": "sh"}}}
    steps = [{"run": "a | b"}]
    assert cwp._iter_steps(steps, workflow, {}) == [(None, "a | b", "sh", "run")]


# ── analyze ──────────────────────────────────────────────────────────────
def test_analyze_non_dict_doc():
    assert cwp.analyze("just a string") == []
    assert cwp.analyze(None) == []


def test_analyze_flags_runcmd_tee_without_pipefail():
    doc = "jobs:\n  j:\n    steps:\n      - with:\n          runCmd: bash s.sh | tee log\n"
    out = _analyze(doc)
    assert len(out) == 1 and "runCmd" in out[0]


def test_analyze_clean_when_runcmd_sets_pipefail():
    doc = (
        "jobs:\n  j:\n    steps:\n      - with:\n"
        "          runCmd: 'set -o pipefail; bash s.sh | tee log'\n"
    )
    assert _analyze(doc) == []


def test_analyze_default_run_pipe_is_safe():
    # No explicit shell → GitHub's default bash has pipefail → not flagged.
    assert _analyze("jobs:\n  j:\n    steps:\n      - run: cat x | grep y\n") == []


def test_analyze_skips_non_dict_jobs_and_missing_jobs():
    assert cwp.analyze({"jobs": {"j": "not-a-dict"}}) == []
    assert cwp.analyze({"name": "x"}) == []


def test_analyze_non_dict_job_does_not_abort_remaining_jobs():
    # The `continue` past a non-dict job must NOT be a `break`: a malformed first job
    # cannot hide a real violation in a later job.
    doc = {
        "jobs": {
            "j1": "not-a-dict",
            "j2": {"steps": [{"run": "cat x | tee y", "shell": "sh"}]},
        }
    }
    out = [msg for _line, msg in cwp.analyze(doc)]
    assert len(out) == 1 and "job j2 (run)" in out[0]


def test_analyze_flags_composite_action_sh_pipe():
    doc = (
        "name: a\nruns:\n  using: composite\n  steps:\n"
        "    - run: cat x | tee y\n      shell: sh\n"
    )
    out = _analyze(doc)
    assert len(out) == 1 and "composite action (run)" in out[0]


# ── check_file ───────────────────────────────────────────────────────────
def test_check_file_reads_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(cwp, "REPO_ROOT", tmp_path)
    path = _write(
        tmp_path / ".github" / "workflows",
        "bad.yaml",
        "jobs:\n  j:\n    steps:\n      - run: cat x | tee y\n        shell: sh\n",
    )
    out = cwp.check_file(path)
    assert len(out) == 1
    line, message = out[0]
    assert line == 4  # the `- run: cat x | tee y` step line
    assert "job j (run)" in message


def test_check_file_reports_unparseable_yaml(tmp_path):
    # Unparseable input can't be verified, so it must not silently read as
    # "no violations" — that would be the exact false-green this tool exists to catch.
    path = _write(tmp_path, "broken.yaml", "key: [unterminated\n")
    out = cwp.check_file(path)
    assert len(out) == 1
    line, message = out[0]
    assert line is None
    assert "could not parse as YAML" in message


# ── workflow_files ───────────────────────────────────────────────────────
def test_workflow_files_includes_actions_dir(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    _write(wf, "a.yaml", "on:\n  push:\n")
    actions = tmp_path / ".github" / "actions" / "x"
    _write(actions, "action.yml", "name: x\n")
    monkeypatch.setattr(cwp, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(cwp, "ACTIONS_DIR", tmp_path / ".github" / "actions")
    assert {p.name for p in cwp.workflow_files()} == {"a.yaml", "action.yml"}


def test_workflow_files_skips_absent_actions_dir(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    _write(wf, "a.yaml", "on:\n  push:\n")
    monkeypatch.setattr(cwp, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(cwp, "ACTIONS_DIR", tmp_path / "nonexistent")
    assert [p.name for p in cwp.workflow_files()] == ["a.yaml"]


# ── main ─────────────────────────────────────────────────────────────────
def _point_at(tmp_path, monkeypatch):
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cwp, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cwp, "WORKFLOWS_DIR", wf)
    monkeypatch.setattr(cwp, "ACTIONS_DIR", tmp_path / "nonexistent")
    return wf


def test_main_returns_zero_when_clean(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(wf, "ok.yaml", "jobs:\n  j:\n    steps:\n      - run: cat x | grep y\n")
    assert cwp.main() == 0
    assert "ERROR" not in capsys.readouterr().out


def test_main_reports_and_fails_on_violation(tmp_path, monkeypatch, capsys):
    wf = _point_at(tmp_path, monkeypatch)
    _write(
        wf,
        "bad.yaml",
        "jobs:\n  j:\n    steps:\n      - with:\n          runCmd: x | tee y\n",
    )
    assert cwp.main() == 1
    out = capsys.readouterr().out
    # The annotation is now navigable: file= and line= attributes are present.
    assert "::error file=.github/workflows/bad.yaml,line=4::" in out
    assert "1 pipefail violation(s) found" in out


def test_run_as_main_exits_nonzero_on_violation(tmp_path):
    # The `if __name__ == "__main__":` guard must actually fire `sys.exit(main())`
    # when executed directly. REPO_ROOT is cwd, so run the real script with cwd set to
    # a hermetic tree holding one violating workflow; the process must exit non-zero.
    _write(
        tmp_path / ".github" / "workflows",
        "bad.yaml",
        "jobs:\n  j:\n    steps:\n      - with:\n          runCmd: x | tee y\n",
    )
    result = subprocess.run(
        [sys.executable, str(SRC)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "1 pipefail violation(s) found" in result.stdout

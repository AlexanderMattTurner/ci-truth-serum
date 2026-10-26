"""A file the grammar cannot read must never report clean.

tree-sitter never raises on malformed input: it recovers into `ERROR` nodes and
keeps going, and that recovery is not local in effect — one unparsed construct
drops the nodes a detector matches on for the rest of the file. A grammar-based
lint therefore returns the same empty list for "read it, found nothing" and for
"never saw it", and `run_source_checks` cannot tell those apart on its own.

These cases pin the two halves: `parse` stays permissive for the callers that
legitimately hand it non-bash, and the whole-file path refuses.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import load_hook

bash_ast = load_hook("_cts_bash_ast.py", "bash_ast_for_unparseable")
linecheck = load_hook("_cts_linecheck.py", "linecheck_for_unparseable")
exit_suppression = load_hook(
    "check_exit_suppression.py", "exit_suppression_for_unparseable"
)

# A `]` inside a parameter-expansion pattern. tree-sitter-bash 0.25.1 reads it as
# a closing bracket and loses the rest of the file; `${workload%"]"*}` is the
# behaviour-identical spelling that parses.
COLLAPSE_TRIGGER = 'workload="${workload%%]*}"\n'
# An idiom check_exit_suppression reports, so its absence is a measurable loss.
VIOLATION = "some_command || true\n"


def test_a_detector_goes_silent_after_an_unparsed_construct() -> None:
    """The loss this refusal exists for: the SAME violation, seen in one file and
    missed in the other, with only an earlier unparsed line between them."""
    assert exit_suppression.violations(f"#!/bin/bash\n{VIOLATION}") == [2]
    assert (
        exit_suppression.violations(f"#!/bin/bash\n{COLLAPSE_TRIGGER}{VIOLATION}") == []
    )


def test_parse_stays_permissive_for_callers_that_hand_it_non_bash() -> None:
    """A workflow `run:` block carries `${{ … }}`, which is GitHub Actions syntax,
    and a fragment lifted out of its file is incomplete by construction. Both
    reach `parse`, and refusing either would break the lints that read them."""
    for text in ('bash "${{ steps.staged.outputs.dir }}/x.sh"\n', "cat <<EOF\n"):
        assert bash_ast.parse(text) is not None


def test_assert_parseable_refuses_and_names_the_line() -> None:
    bash_ast.assert_parseable(f"#!/bin/bash\n{VIOLATION}")
    with pytest.raises(bash_ast.UnparseableShellError) as excinfo:
        bash_ast.assert_parseable(f"#!/bin/bash\n{COLLAPSE_TRIGGER}{VIOLATION}")
    # The line number is what makes the message actionable — it names the one
    # construct to rewrite, not just the file.
    assert "line 2" in str(excinfo.value)


def test_assert_parseable_reports_a_byte_offset_true_to_the_original_file() -> None:
    """The reported offset must index SCRIPT's own bytes, not the astral-folded
    copy the grammar actually parses. A supplementary-plane character (an emoji)
    is 4 UTF-8 bytes in SCRIPT and only 3 in the U+FFFD stand-in the grammar
    sees, so reading `node.start_byte` straight off the parsed tree would
    undercount every offset that follows such a character by one byte each."""
    baseline_comment = "# plain\n"
    astral_comment = "# plain\U0001f600\n"

    def offset_of(comment_line: str) -> int:
        script = f"#!/bin/bash\n{comment_line}{COLLAPSE_TRIGGER}{VIOLATION}"
        with pytest.raises(bash_ast.UnparseableShellError) as excinfo:
            bash_ast.assert_parseable(script)
        match = re.search(r"byte offset (?P<offset>\d+)", str(excinfo.value))
        assert match is not None
        return int(match.group("offset"))

    delta = offset_of(astral_comment) - offset_of(baseline_comment)
    assert delta == len(astral_comment.encode("utf-8")) - len(
        baseline_comment.encode("utf-8")
    )


def test_the_driver_reports_an_unparseable_shell_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """rc=1 with the refusal, not rc=0 with silence."""
    script = tmp_path / "x.bash"
    script.write_text(f"#!/bin/bash\n{COLLAPSE_TRIGGER}{VIOLATION}", encoding="utf-8")
    status = linecheck.run_source_checks(
        [str(script)], lambda text, _path: exit_suppression.violations(text), "msg"
    )
    assert status == 1
    assert "could not parse this file" in capsys.readouterr().err


def test_a_parseable_shell_file_is_unaffected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The refusal must not change a verdict the grammar could already reach."""
    script = tmp_path / "y.bash"
    script.write_text(f"#!/bin/bash\n{VIOLATION}", encoding="utf-8")
    status = linecheck.run_source_checks(
        [str(script)], lambda text, _path: exit_suppression.violations(text), "msg"
    )
    assert status == 1
    assert str(script) + ":2: msg" in capsys.readouterr().err


GRAMMAR_HIDDEN = """
import importlib.util, sys

class Hide:
    def find_spec(self, name, path=None, target=None):
        if name.startswith("tree_sitter"):
            raise ImportError("hidden for this probe: " + name)
        return None

sys.meta_path.insert(0, Hide())
spec = importlib.util.spec_from_file_location("probe_linecheck", {path!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(module.run_source_checks([{yaml!r}], lambda t, p: [], "msg"))
"""


def test_the_driver_loads_and_runs_without_the_bash_grammar(tmp_path: Path) -> None:
    """About fifty checks import `_cts_linecheck`, and most read YAML or Python and
    are handed no shell. A module-scope grammar import puts `tree_sitter_bash`
    in every one of their pre-commit environments, and each one without it dies
    on import — which is how two hooks broke in CI."""
    doc = tmp_path / "w.yaml"
    doc.write_text("on: push\njobs: {}\n", encoding="utf-8")
    probe = GRAMMAR_HIDDEN.format(path=linecheck.__file__, yaml=str(doc))
    done = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "0"


def test_a_non_shell_file_is_never_parse_checked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate keys on the path being shell, so a YAML or Python argument keeps
    reaching its own detector — the workflow lints hand this driver files the
    bash grammar was never meant to read."""
    doc = tmp_path / "w.yaml"
    doc.write_text("on: push\njobs: {}\n", encoding="utf-8")
    status = linecheck.run_source_checks([str(doc)], lambda _t, _p: [], "msg")
    assert status == 0
    assert capsys.readouterr().err == ""


# ── every shell check's own `main` refuses, not only the shared driver ───────
# Each of these held a hand-written read loop that skipped the refusal, so the
# file above reported clean through the check's real entry point.
_MAIN_CHECKS = (
    "check_truncating_pr_json",
    "check_versionless_install",
    "check_pinned_downloads",
    "check_gh_slurp_jq",
    "check_stderr_merge_parse",
    "check_echo_fallback",
    "check_flag_arity",
)


@pytest.mark.parametrize("name", _MAIN_CHECKS)
def test_each_shell_check_main_refuses_an_unparseable_file(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    check = load_hook(f"{name}.py", f"{name}_for_unparseable_main")
    script = tmp_path / "x.sh"
    script.write_text(f"#!/bin/bash\n{COLLAPSE_TRIGGER}{VIOLATION}", encoding="utf-8")
    assert check.main([str(script)]) == 1
    assert "could not parse this file" in capsys.readouterr().err


def test_the_dead_function_check_refuses_an_unparseable_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A cross-file check: a file the grammar cannot read contributes no
    definitions, and a verdict built without it is not a pass."""
    check = load_hook("check_dead_shell_functions.py", "dead_fn_for_unparseable")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    script = tmp_path / "x.sh"
    script.write_text(f"#!/bin/bash\n{COLLAPSE_TRIGGER}{VIOLATION}", encoding="utf-8")
    assert check.main(["--repo-root", str(tmp_path), str(script)]) == 1
    assert "could not parse this file" in capsys.readouterr().err


# ── a workflow's `run:` scripts: one reader, which honours `shell:` ──────────
_STEP = "jobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: pip install requests\n"


def _workflow(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".github" / "workflows" / "w.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_a_bash_run_script_is_read_at_its_step_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    check = load_hook("check_versionless_install.py", "versionless_for_run_scripts")
    path = _workflow(tmp_path, _STEP)
    assert check.main([str(path)]) == 1
    assert f"{path}:5:" in capsys.readouterr().err


def test_a_non_bash_run_script_is_not_read_as_bash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `pwsh` script is another language; reading it with the bash grammar
    invents findings. Non-vacuity: the bash step above reports."""
    check = load_hook("check_versionless_install.py", "versionless_for_pwsh")
    path = _workflow(tmp_path, _STEP + "        shell: pwsh\n")
    assert check.main([str(path)]) == 0
    assert capsys.readouterr().err == ""


def test_a_workflow_that_will_not_parse_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The copies of this reader returned no scripts for broken YAML, so the
    check reported a clean pass over a file it never read."""
    check = load_hook("check_versionless_install.py", "versionless_for_bad_yaml")
    path = _workflow(tmp_path, _STEP + "  b: [unclosed\n")
    assert check.main([str(path)]) == 1
    assert "could not read this file as YAML" in capsys.readouterr().err

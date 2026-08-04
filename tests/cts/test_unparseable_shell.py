"""A file the grammar cannot read must never report clean.

tree-sitter never raises on malformed input: it recovers into `ERROR` nodes and
keeps going, and that recovery is not local in effect — one unparsed construct
drops the nodes a detector matches on for the rest of the file. A grammar-based
lint therefore returns the same empty list for "read it, found nothing" and for
"never saw it", and `run_source_checks` cannot tell those apart on its own.

These cases pin the two halves: `parse` stays permissive for the callers that
legitimately hand it non-bash, and the whole-file path refuses.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import load_hook

bash_ast = load_hook("_bash_ast.py", "bash_ast_for_unparseable")
linecheck = load_hook("_linecheck.py", "linecheck_for_unparseable")
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
    """About fifty checks import `_linecheck`, and most read YAML or Python and
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

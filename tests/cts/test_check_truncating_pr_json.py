"""Tests for ci_truth_serum/check_truncating_pr_json.py — the lint refusing a
``gh pr view``/``gh pr list`` call that reads a ``--json`` connection field
gh caps at 100 with no cursor.

Drives ``violations()`` directly, and ``main()`` for the argv/exit-code
contract and the workflow-YAML routing shape shared with check_gh_slurp_jq.py.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_truncating_pr_json.py", "check_truncating_pr_json")


@pytest.mark.parametrize("field", sorted(mod._TRUNCATING))
def test_fires_on_every_truncating_field(field: str) -> None:
    """Driven from the module's own field set, so a field added there without
    a case here still has to pass this test rather than going unexercised."""
    assert mod.violations(f'gh pr view "$PR" --json {field}\n') == [1]


@pytest.mark.parametrize(
    "line",
    [
        'touches="$(gh pr view "$pr" --repo "$GH_REPO" --json files --jq \'.files\')"',
        'commits="$(retry_stdout gh pr view "$n" --repo "$REPO" --json commits)"',
        'gh pr view "$PR" --json title,body,author,files',
        # the `=` spelling, and a quoted value
        "gh pr view 3 --json=commits",
        "gh pr view 3 --json 'title,files'",
        # a listing truncates the same way
        "gh pr list --state open --json number,files",
        # a backslash-continued invocation is ONE command
        'gh pr view "$PR" \\\n  --repo "$R" \\\n  --json files\n',
        # a wrapper does not change what the wrapped program reads
        "retry_stdout gh pr view 3 --json commits",
    ],
)
def test_fires_on_real_call_shapes(line: str) -> None:
    assert mod.violations(line) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # bounded connections
        'gh pr view "$PR" --json number,labels,author,headRefOid',
        'gh pr view "$PR" --json title,body,author',
        # the remedy this lint names
        "gh api --paginate \"repos/$R/pulls/$PR/files\" --jq '.[].filename'",
        # a single object needs no list at all
        "gh api \"repos/$R/commits/$sha\" --jq '.commit.committer.date'",
        # an EXPANDED field set is not knowable here
        'gh pr view "$PR_NUMBER" --repo "$target_repo" --json "$3" --jq \'[.]\'',
        'gh pr view 3 --json "$fields"',
        # not a pr read
        'gh api --paginate "repos/$R/issues/$n/timeline"',
        'gh run view "$id" --json jobs',
        # a comment, and a trailing comment quoting the banned form
        "# gh pr view --json files truncates at 100",
        'true  # gh pr view "$PR" --json files',
        # an unquoted word list under a message command is a sentence
        "echo gh pr view 3 --json files",
        # the inert body of a quoted-delimiter heredoc is text the script prints
        "cat <<'EOF' >/tmp/help\ngh pr view 3 --json files\nEOF",
        # same-line annotation, with the reason the marker requires
        "gh pr view 3 --json files  # truncating-pr-json-ok: PR is known small",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_a_bare_annotation_without_a_reason_does_not_exempt() -> None:
    assert mod.violations("gh pr view 3 --json files  # truncating-pr-json-ok\n") == [1]


def test_two_reads_on_one_line_report_once() -> None:
    text = "gh pr view 1 --json files; gh pr view 2 --json commits\n"
    assert mod.violations(text) == [1]


def test_opt_out_on_line_above() -> None:
    text = "# truncating-pr-json-ok: justified\ngh pr view 3 --json files\n"
    assert mod.violations(text) == []


def test_opt_out_does_not_reach_past_an_intervening_line() -> None:
    text = "# truncating-pr-json-ok: something else\ndo_a\ngh pr view 3 --json files\n"
    assert mod.violations(text) == [3]


def test_non_vacuity_the_data_cases_still_fire_as_real_commands() -> None:
    assert mod.violations("gh pr view 3 --json files\n") == [1]
    assert mod.violations(
        "echo gh pr view 3 --json files\ngh pr view 3 --json files\n"
    ) == [2]


# ── extensibility flag ─────────────────────────────────────────────────────
def test_field_flag_extends_the_built_in_set() -> None:
    text = "gh pr view 3 --json statusCheckRollup\n"
    assert mod.violations(text) == []
    assert mod.violations(text, truncating=frozenset({"statusCheckRollup"})) == [1]


# ── Python: subprocess.run(["gh", "pr", …]) ─────────────────────────────────
@pytest.mark.parametrize("field", sorted(mod._TRUNCATING))
def test_python_fires_on_every_truncating_field(field: str) -> None:
    """Driven from the module's own field set, the same way the shell case
    above is, so a field added there without a Python case fails here too."""
    src = f'subprocess.run(["gh", "pr", "view", pr, "--json", "{field}"])\n'
    assert mod.python_violations(src) == [1]


@pytest.mark.parametrize(
    "line",
    [
        # a listing, the `=` spelling, and a bare `args=` keyword
        'subprocess.run(["gh", "pr", "list", "--json", "files"])',
        'subprocess.run(["gh", "pr", "view", pr, "--json=commits"])',
        'subprocess.run(args=["gh", "pr", "view", pr, "--json", "files"])',
        # several fields, one of them truncating
        'subprocess.run(["gh", "pr", "view", pr, "--json", "title,files"])',
        # a wrapped call still starts on the `subprocess.run(` line
        'subprocess.run(\n    ["gh", "pr", "view", pr, "--json", "files"]\n)',
    ],
)
def test_python_fires_on_real_call_shapes(line: str) -> None:
    assert mod.python_violations(line) == [1]


@pytest.mark.parametrize(
    "src",
    [
        # bounded fields
        'subprocess.run(["gh", "pr", "view", pr, "--json", "number,labels"])',
        # a `pr edit`, not a `view`/`list` read
        'subprocess.run(["gh", "pr", "edit", pr_number, "--add-label", label], '
        "check=False)",
        # an unresolvable field value (a variable, not a literal) is unknown,
        # never read as clean nor as a violation by guessing
        'subprocess.run(["gh", "pr", "view", pr, "--json", fields])',
        # not `gh` at all
        'subprocess.run(["git", "pr", "view", "--json", "files"])',
        # a `gh` call this check does not model (`gh run view`)
        'subprocess.run(["gh", "run", "view", run_id, "--json", "jobs"])',
        # same-line annotation, with the required reason
        'subprocess.run(["gh", "pr", "view", pr, "--json", "files"])  '
        "# truncating-pr-json-ok: fixture PR of 2 files",
    ],
)
def test_python_clean_calls_do_not_fire(src: str) -> None:
    assert mod.python_violations(src) == []


@pytest.mark.parametrize(
    "src",
    [
        'subprocess.run("gh pr view $PR --json files", shell=True)',
        "subprocess.run(args='gh pr list --json commits', shell=True)",
        'subprocess.run(\n    "gh pr view 3 --json title,reviews",\n    shell=True,\n)',
    ],
)
def test_python_fires_on_a_whole_command_line_written_as_one_string(src: str) -> None:
    """A `shell=True` string is a command line, not an argv, so the argv walker
    cannot see it and no shell file holds the text. The shell grammar reads the
    literal instead."""
    assert mod.python_violations(src) == [1]


@pytest.mark.parametrize(
    "src",
    [
        # bounded fields, so the shell grammar reports nothing
        'subprocess.run("gh pr view $PR --json number,labels", shell=True)',
        # not a `gh pr view`/`list` read
        'subprocess.run("git log --json files", shell=True)',
        # the annotation is honoured on the Python call, not inside the string
        'subprocess.run("gh pr view 3 --json files", shell=True)  '
        "# truncating-pr-json-ok: fixture PR of 2 files",
    ],
)
def test_python_clean_command_line_strings_do_not_fire(src: str) -> None:
    assert mod.python_violations(src) == []


def test_python_field_flag_extends_the_built_in_set() -> None:
    src = 'subprocess.run(["gh", "pr", "view", pr, "--json", "statusCheckRollup"])\n'
    assert mod.python_violations(src) == []
    assert mod.python_violations(src, truncating=frozenset({"statusCheckRollup"})) == [
        1
    ]


def test_python_opt_out_on_line_above() -> None:
    src = (
        "# truncating-pr-json-ok: justified\n"
        'subprocess.run(["gh", "pr", "view", pr, "--json", "files"])\n'
    )
    assert mod.python_violations(src) == []


def test_python_non_vacuity_the_annotated_case_still_fires_when_stale() -> None:
    src = (
        "# truncating-pr-json-ok: something else\n"
        "do_a()\n"
        'subprocess.run(["gh", "pr", "view", pr, "--json", "files"])\n'
    )
    assert mod.python_violations(src) == [3]


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text('gh pr view "$PR" --json files\n')
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path: Path) -> None:
    p = tmp_path / "s.sh"
    p.write_text('gh pr view "$PR" --json title,body\n')
    assert mod.main([str(p)]) == 0


def test_main_field_flag_adds_a_hit(tmp_path: Path) -> None:
    p = tmp_path / "s.sh"
    p.write_text("gh pr view 3 --json statusCheckRollup\n")
    assert mod.main([str(p)]) == 0
    assert mod.main(["--field", "statusCheckRollup", str(p)]) == 1


def test_main_routes_workflow_yaml_to_run_blocks(tmp_path: Path, capsys) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    bad = workflows / "ci.yaml"
    bad.write_text(
        "jobs:\n"
        "  a:\n"
        "    steps:\n"
        "      - run: echo hello\n"
        "      - name: read\n"
        "        run: |\n"
        '          gh pr view "$PR" --json files\n',
        encoding="utf-8",
    )
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:5:" in capsys.readouterr().err


def test_main_routes_python_files_through_the_ast_arm(tmp_path: Path, capsys) -> None:
    bad = tmp_path / "review.py"
    bad.write_text(
        'subprocess.run(["gh", "pr", "view", pr, "--json", "files"])\n',
        encoding="utf-8",
    )
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:1:" in capsys.readouterr().err

    good = tmp_path / "clean.py"
    good.write_text(
        'subprocess.run(["gh", "pr", "view", pr, "--json", "title,body"])\n',
        encoding="utf-8",
    )
    assert mod.main([str(good)]) == 0


def test_main_skips_non_workflow_yaml(tmp_path: Path, capsys) -> None:
    other = tmp_path / "config.yaml"
    other.write_text("note: gh pr view --json files is truncating\n", encoding="utf-8")
    assert mod.main([str(other)]) == 0
    assert capsys.readouterr().err == ""


def test_main_fails_loudly_on_pathological_input(tmp_path: Path, capsys) -> None:
    huge = tmp_path / "huge.sh"
    huge.write_text("true " + "| true " * 3000 + "\n", encoding="utf-8")
    assert mod.main([str(huge)]) == 1
    assert "pipe bytes" in capsys.readouterr().err

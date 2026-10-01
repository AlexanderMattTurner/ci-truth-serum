"""Tests for ci_truth_serum/check_main_push.py — the lint that refuses a shell
``git push`` whose destination is a protected branch.

Drives ``violations()`` and ``workflow_violations()`` directly so each rule is
asserted alone, then drives ``main()`` over real files for the CLI contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_main_push.py", "check_main_push")


@pytest.mark.parametrize(
    "text",
    [
        "git push --no-verify origin HEAD:main",
        # The fully-qualified destination is the same ref.
        'git push origin "HEAD:refs/heads/main"',
        # A one-sided refspec names the same ref on both ends.
        "git push origin main",
        # A force push carries a leading `+`.
        "git push --force origin +HEAD:main",
        # `:main` deletes the branch, which also writes it.
        "git push origin :main",
        # The second default protected branch.
        "git push origin HEAD:master",
        # A continued push is ONE command, so the refspec two lines down belongs
        # to the `git push` on the first.
        "git push --no-verify \\\n  origin \\\n  HEAD:main",
        # A wrapper in front does not change what git does.
        "sudo git push origin main",
        "run_as_root git push origin main",
        "/usr/bin/git push origin main",
        # git's own global options stand between `git` and `push`.
        'git -C "$repo" -c push.default=current push origin HEAD:main',
        # A computed SOURCE still lands on a literal destination.
        'git push origin "$sha:main"',
        # A `$(…)` remote keeps its place, so `main` is still the refspec.
        "git push $(remote_name) main",
        # A push option's value is skipped, and the refspec after it still reads.
        "git push -o ci.skip origin main",
        # Inside a substitution, a condition, or a pipeline it still runs.
        'out="$(git push origin HEAD:main 2>&1)"',
        "if ! git push origin main; then exit 1; fi",
    ],
)
def test_fires_on_a_push_to_a_protected_branch(text: str) -> None:
    assert mod.violations(text) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # A branch push is what the rule asks for.
        'git push -u origin "HEAD:refs/heads/${BRANCH}"',
        "git push --no-verify origin HEAD:refs/heads/metrics-history",
        # `main` as the SOURCE: the destination is another branch.
        "git push origin main:experiment",
        # A branch NAMED after main is a different ref.
        "git push origin HEAD:main-rewrite",
        # A destination that holds an expansion is unknown.
        'git push origin "HEAD:${TARGET:-main}"',
        "git push origin HEAD:$(default_branch)",
        # One positional word is the remote, not a refspec.
        "git push main",
        # `tag main` names a tag.
        "git push origin tag main",
        # A negative refspec excludes a ref.
        "git push origin '^main'",
        # A push option's VALUE is not a refspec.
        "git push origin -o main topic",
        # Other git verbs reach main without writing it.
        "git fetch origin main",
        "git rebase origin/main",
        "git log --grep push main",
        "git merge-base --is-ancestor HEAD origin/main",
        # A comment and a trailing comment run nothing.
        "# never git push origin HEAD:main",
        "true  # git push origin HEAD:main",
        # A word list under a message command is a sentence.
        "echo git push origin HEAD:main",
        "printf '%s\\n' git push origin main",
        # A lookup names git without running it.
        "command -v git push origin main",
        # A same-line opt-out with a reason.
        "git push origin HEAD:main  # main-push-ok: the release reads the tag off main",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_a_message_string_naming_the_push_is_text() -> None:
    """Probe 1: the banned idiom inside a logger's message string."""
    text = 'log_warn "run git push origin HEAD:main by hand if this fails"\n'
    assert mod.violations(text) == []


def test_a_heredoc_body_naming_the_push_is_text() -> None:
    """Probe 2: the banned idiom inside a heredoc body written to a file."""
    text = "cat <<'EOF' > help.txt\ngit push origin HEAD:main\nEOF\n"
    assert mod.violations(text) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The comment block directly above counts, even when it wraps.
        (
            "# main-push-ok: the release\n# reads it off main\ngit push origin main\n",
            [],
        ),
        ("# main-push-ok: the release reads it off main\ngit push origin main\n", []),
        # A marker with no reason states nothing.
        ("git push origin HEAD:main  # main-push-ok", [1]),
        ("git push origin HEAD:main  # main-push-ok:", [1]),
        # A code line between the marker and the push ends the block.
        ("# main-push-ok: the release\ndo_a\ndo_b\ngit push origin main\n", [4]),
        # A longer token that merely contains the marker is another annotation.
        ("git push origin main  # not-main-push-ok: x", [1]),
    ],
)
def test_the_opt_out_needs_a_reason_beside_the_push(text: str, expected) -> None:
    assert mod.violations(text) == expected


def test_two_pushes_on_one_line_report_once() -> None:
    assert mod.violations("git push origin main; git push origin HEAD:main\n") == [1]


def test_each_push_reports_its_own_line() -> None:
    text = "set -e\ngit push origin main\nmake\ngit push origin HEAD:master\n"
    assert mod.violations(text) == [2, 4]


def test_branches_replace_the_defaults() -> None:
    release = frozenset({"release"})
    assert mod.violations("git push origin HEAD:release", release) == [1]
    assert mod.violations("git push origin HEAD:main", release) == []


def test_a_git_command_takes_git_arguments() -> None:
    text = "git_with_token push origin HEAD:main"
    assert mod.violations(text) == []
    assert mod.violations(text, gits=frozenset({"git_with_token"})) == [1]


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        # Destination: the destination half decides, not the word `main`.
        ("git push origin HEAD:main", "git push origin main:topic"),
        # Subcommand: git's global options are skipped, and the verb must be push.
        ("git -C main push origin main", "git -C main fetch origin main"),
        # Remote: the first positional word is the remote, never a refspec.
        ("git push origin main", "git push main"),
        # Message: a printing command's words are text.
        ("git push origin main", "echo git push origin main"),
        # Expansion: a computed destination is unknown.
        ("git push origin HEAD:main", 'git push origin "HEAD:$main"'),
    ],
)
def test_each_rule_decides_a_verdict(text: str, clean: str) -> None:
    """Non-vacuity: each pair differs in one rule, and the verdict flips with it."""
    assert (mod.violations(text), mod.violations(clean)) == ([1], [])


_WORKFLOW = """\
on: push
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - name: build
        run: make
      - name: publish
        run: |
          git config user.name bot
          git push origin HEAD:main
      - run: git push origin HEAD:topic
"""


def test_a_run_block_reports_the_workflow_line() -> None:
    assert mod.workflow_violations(_WORKFLOW) == [11]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # A quoted one-line run value.
        ("steps:\n  - run: 'git push origin main'\n", [2]),
        # A plain one-line run value.
        ("steps:\n  - run: git push origin main\n", [2]),
        # A YAML comment directly above the step's run line.
        ("steps:\n  # main-push-ok: the release\n  - run: git push origin main\n", []),
        # A script comment inside the run value.
        (
            "steps:\n  - run: |\n      # main-push-ok: the release\n"
            "      git push origin main\n",
            [],
        ),
        # A marker inside a quoted VALUE is content, not a comment.
        (
            'steps:\n  - name: "x  # main-push-ok: not a comment"\n'
            "    run: git push origin main\n",
            [3],
        ),
        # A heredoc body inside a run value is text.
        (
            "steps:\n  - run: |\n      cat <<'EOF' > help.txt\n"
            "      git push origin main\n      EOF\n",
            [],
        ),
        # A key other than `run:` is not a script.
        ("steps:\n  - name: git push origin main\n", []),
    ],
)
def test_workflow_run_values(text: str, expected: list[int]) -> None:
    assert mod.workflow_violations(text) == expected


def test_main_reports_shell_and_workflow_files(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yaml").write_text(
        _WORKFLOW, encoding="utf-8"
    )
    (tmp_path / "release.sh").write_text("git push origin main\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "run: git push origin main\n", encoding="utf-8"
    )
    files = [".github/workflows/ci.yaml", "release.sh", "config.yaml"]
    assert mod.main(files) == 1
    err = capsys.readouterr().err
    assert ".github/workflows/ci.yaml:11: `git push` lands on a protected branch" in err
    assert "release.sh:1: " in err
    assert "config.yaml" not in err
    assert "# main-push-ok: <reason>" in err


def test_main_takes_the_branch_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.sh").write_text("git push origin HEAD:trunk\n", encoding="utf-8")
    assert mod.main(["a.sh"]) == 0
    assert mod.main(["--branch", "trunk", "a.sh"]) == 1


def test_main_refuses_an_empty_file_list(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err

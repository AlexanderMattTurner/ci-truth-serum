"""Tests for ci_truth_serum/check_unscoped_tool_grant.py — the (security) pre-commit
lint that flags a file-tool rule which is not really path-scoped in a Claude Code
`--allowedTools` grant.

Two classes, driven through ``check_file`` on real workflow files:

  BARE   `Read`/`Grep`/`Glob` (opt out `allow-unscoped-read-grant`) and
         `Write`/`Edit`/`MultiEdit`/`NotebookEdit` (opt out
         `allow-unscoped-write-grant`) with no `(...)` — a whole-tool grant that
         overrides the per-path working-directory check.
  INERT  a path rule under any tool name but `Read`/`Edit` — accepted by the CLI
         and then never consulted. NO opt-out.

Every assertion is on the verdict ``check_file`` returns (line numbers, and which
class's message came back), never on the lint's source text.
"""

from pathlib import Path

import pytest

from tests._helpers import load_hook

ug = load_hook("check_unscoped_tool_grant.py", "check_unscoped_tool_grant")

# An absolute path in a rule needs TWO leading slashes; one anchors at the CLI's cwd.
_ABS = "//home/runner/work/_temp/review.json"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body)
    return path


def _run_wf(grant: str) -> str:
    """A one-step workflow whose `run:` command carries GRANT, on line 6."""
    return f"on:\n  push:\njobs:\n  j:\n    steps:\n      - run: claude {grant}\n"


def _env_wf(entry: str) -> str:
    """A workflow whose job `env:` carries ENTRY (a `KEY: value` pair), on line 6."""
    return f"on:\n  push:\njobs:\n  j:\n    env:\n      {entry}\n"


def _verdict(tmp_path: Path, body: str) -> list[tuple[int | None, str]]:
    return ug.check_file(_write(tmp_path, body))


def _lines(tmp_path: Path, body: str) -> list[int | None]:
    return [line for line, _ in _verdict(tmp_path, body)]


def _is_bare(message: str) -> bool:
    return "WHOLE-TOOL grant" in message


def _is_inert(message: str) -> bool:
    return "NOTHING CONSULTS" in message


# ── the BARE class, member by member ────────────────────────────────────────
@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob"])
def test_fires_on_each_bare_read_tool(tmp_path, tool):
    """Each member of the read alternation on its own, so dropping one reds. The
    write half is already scoped, so only the read finding may come back."""
    found = _verdict(tmp_path, _run_wf(f'--allowedTools "{tool},Edit(./**)"'))
    assert [line for line, _ in found] == [6]
    message = found[0][1]
    assert _is_bare(message)
    assert f"`{tool}`" in message
    assert ug.READ_OPT_OUT in message


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit", "NotebookEdit"])
def test_fires_on_each_bare_file_editing_tool(tmp_path, tool):
    found = _verdict(tmp_path, _run_wf(f'--allowedTools "Read(./**),{tool}"'))
    assert [line for line, _ in found] == [6]
    message = found[0][1]
    assert _is_bare(message)
    assert f"`{tool}`" in message
    assert ug.WRITE_OPT_OUT in message


def test_a_bare_tool_among_path_scoped_siblings_still_fires(tmp_path):
    """The scoped siblings do not launder it: the whole-tool rule is applied after
    the per-path check and overrides its verdict."""
    grant = f'--allowedTools "Read(./**),Edit({_ABS}),MultiEdit"'
    assert _lines(tmp_path, _run_wf(grant)) == [6]


def test_both_halves_bare_are_reported_as_two_findings_on_one_line(tmp_path):
    """Each half answers to its own slug, so each is its own finding — fixing one
    cannot reveal the other only on a later CI cycle."""
    found = _verdict(tmp_path, _run_wf('--allowedTools "Read,Write"'))
    assert [line for line, _ in found] == [6, 6]
    assert [ug.READ_OPT_OUT in m for _, m in found] == [True, False]
    assert [ug.WRITE_OPT_OUT in m for _, m in found] == [False, True]


# ── the INERT class, member by member ───────────────────────────────────────
@pytest.mark.parametrize("tool", ["Write", "Grep", "Glob", "MultiEdit", "NotebookEdit"])
def test_fires_on_each_inert_path_rule(tmp_path, tool):
    """A path rule under a name the file-permission check never keys on. Note this
    also pins WHICH class claimed `Grep(<path>)`/`Glob(<path>)`: the bare-read match
    is blocked by the `(`, so the finding must be the inert one."""
    grant = f'--allowedTools "Read(./**),{tool}({_ABS})"'
    found = _verdict(tmp_path, _run_wf(grant))
    assert [line for line, _ in found] == [6]
    message = found[0][1]
    assert _is_inert(message)
    assert f"`{tool}(<path>)`" in message
    assert "NO opt-out" in message


def test_an_inert_rule_is_the_only_finding_when_nothing_is_bare(tmp_path):
    grant = f'--allowedTools "Read(./**),Write({_ABS}),Bash(shfmt:*)"'
    found = _verdict(tmp_path, _run_wf(grant))
    assert [(line, _is_inert(m)) for line, m in found] == [(6, True)]


# ── the spellings that really do scope ──────────────────────────────────────
@pytest.mark.parametrize(
    "grant",
    [
        '--allowedTools "Read(./**),Edit(./**)"',
        f'--allowedTools "Read(./**),Edit({_ABS})"',
        '--allowedTools "Read(./**),Bash,Edit(./**)"',
        '--allowed-tools "Read(./**),Edit(./**)"',
        # grants naming no file tool at all
        '--allowedTools "Bash(gh pr view:*),Bash(gh pr edit:*)"',
        '--allowedTools "Bash(shfmt:*),WebFetch,TodoWrite"',
        # the grant list held behind an expression reference
        '--allowedTools "${{ env.CLAUDE_ALLOWED_TOOLS }}"',
    ],
)
def test_scoped_grants_are_clean(tmp_path, grant):
    assert _verdict(tmp_path, _run_wf(grant)) == []


def test_a_two_slash_absolute_path_is_accepted(tmp_path):
    assert _verdict(tmp_path, _run_wf(f'--allowedTools "Edit({_ABS})"')) == []


def test_a_single_slash_path_is_deliberately_not_flagged(tmp_path):
    """A single leading slash is NOT a broken rule — it anchors the pattern at the
    CLI's own working directory, which is a working (if easily mistaken) meaning:
    against CLI 2.1.220, `Edit(/f.txt)` permitted an edit of `<cwd>/f.txt`. Only an
    author who MEANT a filesystem-absolute path is bitten, and nothing in the text
    distinguishes the two intents — so the two-slash requirement is stated in the
    message rather than guessed at by a detector."""
    assert (
        _verdict(tmp_path, _run_wf('--allowedTools "Read(/src/**),Edit(/src/**)"'))
        == []
    )


@pytest.mark.parametrize(
    "grant",
    [
        # names that merely START with a covered tool name
        '--allowedTools "ReadMcpResource,EditorConfig,WriteFile,GlobPattern"',
        '--allowedTools "Reader,Grepper,Globber,Editable,MultiEditor"',
        # ...and names that END with one: the leading word character is what the
        # lookbehind rejects
        '--allowedTools "PreWrite,XRead,MyGlob"',
    ],
)
def test_word_boundaries_keep_the_lint_off_longer_names(tmp_path, grant):
    assert _verdict(tmp_path, _run_wf(grant)) == []


# ── where a grant is written ───────────────────────────────────────────────
def test_the_hyphenated_flag_alias_is_covered(tmp_path):
    assert _lines(tmp_path, _run_wf('--allowed-tools "Read(./**),Write"')) == [6]


def test_a_space_separated_grant_is_covered(tmp_path):
    """`claude_args: "--model x --allowedTools Bash Read Write"` is the same grant
    in the CLI's other accepted spelling."""
    grant = "--model claude-sonnet-4-6 --allowedTools Bash Read Write Edit Glob Grep"
    found = _verdict(tmp_path, _run_wf(grant))
    assert [line for line, _ in found] == [6, 6]


@pytest.mark.parametrize(
    "entry",
    [
        'ALLOWED_TOOLS: "Read,Edit(./**)"',
        'CLAUDE_ALLOWED_TOOLS: "Read,Edit(./**)"',
        'AUTOFIX_ALLOWED_TOOLS: "Read,Edit(./**),Bash(shfmt:*)"',
    ],
)
def test_an_env_var_holding_the_grant_is_covered(tmp_path, entry):
    """Moving the list into an env var a later step passes to the CLI must not
    escape the check."""
    assert _lines(tmp_path, _env_wf(entry)) == [6]


def test_a_disallowed_tools_env_key_is_not_a_grant(tmp_path):
    """`DISALLOWED_TOOLS` holds a DENY list, where a bare tool name is the widest
    (and desired) denial — "scoping" it would narrow what is refused."""
    assert _verdict(tmp_path, _env_wf('DISALLOWED_TOOLS: "Read,Write,Edit"')) == []


def test_a_commented_out_grant_does_not_fire(tmp_path):
    """A grant merely TALKED ABOUT in a YAML comment is not a grant the job runs."""
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        '      # --allowedTools "Read,Grep,Glob,Write"  (never do this)\n'
        '      - run: claude --allowedTools "Read(./**),Edit(./**)"\n'
    )
    assert _verdict(tmp_path, body) == []


def test_prose_naming_the_tools_outside_a_grant_does_not_fire(tmp_path):
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        '      - run: echo "Read, Grep and Glob already do that"\n'
        '      - run: echo "Write, Edit and NotebookEdit are the write half"\n'
    )
    assert _verdict(tmp_path, body) == []


# ── the opt-outs ───────────────────────────────────────────────────────────
def test_the_read_opt_out_clears_a_bare_read(tmp_path):
    grant = '--allowedTools "Read,Edit(./**)"  # allow-unscoped-read-grant: bare Bash already reads everything'
    assert _verdict(tmp_path, _run_wf(grant)) == []


def test_the_write_opt_out_clears_a_bare_write(tmp_path):
    grant = '--allowedTools "Read(./**),Write"  # allow-unscoped-write-grant: the product IS an arbitrary edit'
    assert _verdict(tmp_path, _run_wf(grant)) == []


def test_the_read_opt_out_does_not_clear_a_bare_write(tmp_path):
    """The two slugs are independent: the annotation covers only its own half."""
    grant = '--allowedTools "Read,Write"  # allow-unscoped-read-grant: bare Bash already reads everything'
    found = _verdict(tmp_path, _run_wf(grant))
    assert [line for line, _ in found] == [6]
    assert ug.WRITE_OPT_OUT in found[0][1]


def test_the_write_opt_out_does_not_clear_a_bare_read(tmp_path):
    grant = '--allowedTools "Read,Write"  # allow-unscoped-write-grant: the product IS an arbitrary edit'
    found = _verdict(tmp_path, _run_wf(grant))
    assert [line for line, _ in found] == [6]
    assert ug.READ_OPT_OUT in found[0][1]


def test_both_opt_outs_together_clear_a_line_bare_on_both_halves(tmp_path):
    grant = (
        '--allowedTools "Read,Write"  # allow-unscoped-read-grant: bare Bash reads it all'
        "  # allow-unscoped-write-grant: and writes it all"
    )
    assert _verdict(tmp_path, _run_wf(grant)) == []


@pytest.mark.parametrize(
    "slug", ["allow-unscoped-read-grant", "allow-unscoped-write-grant"]
)
def test_a_reasonless_annotation_does_not_opt_out(tmp_path, slug):
    """A bare marker states nothing, so it does not suppress."""
    found = _verdict(tmp_path, _run_wf(f'--allowedTools "Read,Write"  # {slug}'))
    assert [line for line, _ in found] == [6, 6]


@pytest.mark.parametrize(
    ("slug", "grant"),
    [
        ("allow-unscoped-read-grant", '--allowedTools "Read,Edit(./**)"'),
        ("allow-unscoped-write-grant", '--allowedTools "Read(./**),Write"'),
    ],
)
def test_an_annotation_on_the_preceding_line_opts_out(tmp_path, slug, grant):
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        f"      # {slug}: bare Bash beside it already reaches every path\n"
        f"      - run: claude {grant}\n"
    )
    assert _verdict(tmp_path, body) == []


def test_both_opt_outs_on_separate_comment_lines_above_clear_the_grant(tmp_path):
    """One grant can trip BOTH classes, and only one annotation fits the single
    line directly above it. Placement is `annotation_window`'s rule — the grant
    line plus the whole unbroken comment block above — so both reasons land, each
    on its own line, and each stays readable prose."""
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        "      # This step installs packages, so it needs a general Bash grant.\n"
        "      # allow-unscoped-read-grant: bare Bash beside it already reads every path\n"
        "      # allow-unscoped-write-grant: bare Bash beside it already writes every path\n"
        '      - run: claude --allowedTools "Bash,Read,Write"\n'
    )
    assert _verdict(tmp_path, body) == []


def test_a_blank_line_breaks_the_comment_block_and_the_opt_out(tmp_path):
    """Non-vacuity for the test above: the window stops at the first line that is
    not a comment. An annotation written about something else therefore stays
    with it, and does not reach a grant further down the file."""
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        "      # allow-unscoped-read-grant: this reason belongs to something else\n"
        "\n"
        '      - run: claude --allowedTools "Read,Edit(./**)"\n'
    )
    assert [line for line, _ in _verdict(tmp_path, body)] == [8]


def test_an_annotation_inside_a_run_block_scalar_opts_out(tmp_path):
    """In a block scalar the `#` is content, not a YAML comment — the annotation
    must still be read from the source line."""
    body = (
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        "      - run: |\n"
        '          claude --allowedTools "Read,Edit(./**)"  # allow-unscoped-read-grant: bare Bash reads it all\n'
    )
    assert _verdict(tmp_path, body) == []


@pytest.mark.parametrize(
    "slug",
    ["allow-unscoped-read-grant-legacy", "really-allow-unscoped-write-grant"],
)
def test_a_neighbouring_slug_does_not_disarm_this_one(tmp_path, slug):
    """A longer slug that merely contains this one is a DIFFERENT annotation; one
    lint's opt-out must never clear another's."""
    grant = f'--allowedTools "Read,Write"  # {slug}: some other lint'
    assert [line for line, _ in _verdict(tmp_path, _run_wf(grant))] == [6, 6]


@pytest.mark.parametrize(
    "slug",
    ["allow-unscoped-read-grant", "allow-unscoped-write-grant"],
)
def test_no_annotation_clears_an_inert_path_rule(tmp_path, slug):
    """The inert class has no opt-out: an unconsulted rule is not a judgement call a
    reason can excuse, it is a rule that does nothing."""
    grant = f'--allowedTools "Read(./**),Write(./**)"  # {slug}: a stated reason'
    found = _verdict(tmp_path, _run_wf(grant))
    assert [(line, _is_inert(m)) for line, m in found] == [(6, True)]


# ── whole-file behaviour ───────────────────────────────────────────────────
def test_each_offending_line_is_reported_by_its_own_number(tmp_path):
    body = (
        "on:\n"  # 1
        "  push:\n"  # 2
        "jobs:\n"  # 3
        "  j:\n"  # 4
        "    steps:\n"  # 5
        '      - run: claude --allowedTools "Read,Grep,Glob"\n'  # 6 bare read
        '      - run: claude --allowedTools "Read(./**),Edit(./**)"\n'  # 7 clean
        '      - run: claude --allowedTools "Read(./**),NotebookEdit"\n'  # 8 bare write
        "      # Read and Write in prose\n"  # 9 comment
        '      - run: claude --allowedTools "Read(./**),Glob(./**)"\n'  # 10 inert
    )
    assert _lines(tmp_path, body) == [6, 8, 10]


def test_unparseable_yaml_is_reported_as_a_violation(tmp_path):
    """A file the parser cannot read must not come back clean — "no findings" on the
    very artifact under test would be a false green."""
    found = _verdict(tmp_path, "on: [push\njobs: {\n")
    assert len(found) == 1
    line, message = found[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_a_non_mapping_document_is_clean(tmp_path):
    assert _verdict(tmp_path, "- a\n- b\n") == []


# ── main ───────────────────────────────────────────────────────────────────
def _point_at(tmp_path, monkeypatch) -> Path:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ug, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ug, "WORKFLOWS_DIR", workflows)
    monkeypatch.setattr(ug, "ACTIONS_DIR", tmp_path / "nonexistent")
    return workflows


def test_main_reports_every_offending_line_and_exits_nonzero(
    tmp_path, monkeypatch, capsys
):
    workflows = _point_at(tmp_path, monkeypatch)
    (workflows / "bad.yaml").write_text(
        "on:\n  push:\njobs:\n  j:\n    steps:\n"
        '      - run: claude --allowedTools "Read,Grep,Glob"\n'
        '      - run: claude --allowedTools "Read(./**),Glob(./**)"\n'
    )
    (workflows / "ok.yaml").write_text(
        _run_wf('--allowedTools "Read(./**),Bash,Edit(./**)"')
    )

    assert ug.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/bad.yaml,line=6::" in out
    assert "::error file=.github/workflows/bad.yaml,line=7::" in out
    assert "ok.yaml" not in out
    assert "2 violation(s) found" in out


def test_main_is_silent_and_exits_zero_when_every_grant_is_scoped(
    tmp_path, monkeypatch, capsys
):
    """Non-vacuity for the test above: the same driver over a clean tree must
    produce no diagnostic, so the exit status tracks the grants and not the run."""
    workflows = _point_at(tmp_path, monkeypatch)
    (workflows / "ok.yaml").write_text(
        _run_wf('--allowedTools "Read(./**),Bash,Edit(./**)"')
    )

    assert ug.main() == 0
    assert "::error" not in capsys.readouterr().out

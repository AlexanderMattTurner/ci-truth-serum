"""Tests for ci_truth_serum/check_cwd_scoped_git.py — the guard against a git
call that names no repository and so acts on whatever directory the process
happens to be in.

Ported from agent-glovebox's cwd-scoped-git.py, minus its grandfathered
baseline ratchet: this version has no baseline, so every hit fails. Drives
``violations()`` for the argv-reading rules and ``main()`` for the argv/exit-
code contract and the ``--read-only-subcommand`` extension flag.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_cwd_scoped_git.py", "check_cwd_scoped_git")


def _problems(source: str) -> list[int]:
    return mod.violations(source)


@pytest.mark.parametrize(
    "argv",
    [
        '["git", *args]',
        '["git", "merge", "--abort"]',
        '["git", "reset", "--hard", ref]',
        '["git", "checkout", "-f", ref]',
        '["git", "clean", "-fd"]',
        # A -C that arrives AFTER the subcommand is not the repository this
        # call acts on: git has already chosen one by then.
        '["git", "merge", "-C", repo, "--abort"]',
    ],
)
def test_a_git_call_that_names_no_repository_is_refused(argv: str) -> None:
    assert _problems(f"import subprocess\nsubprocess.run({argv})\n")


def test_an_argv_passed_as_the_args_keyword_is_read_too() -> None:
    """``subprocess.run(args=[...])`` is the same call with the argv named, so
    a reader that only walks positional arguments would let it through unseen."""
    assert _problems('import subprocess\nsubprocess.run(args=["git", "merge"])\n')


@pytest.mark.parametrize(
    "call",
    [
        'subprocess.run(["git", "-C", repo, *args])',
        'subprocess.run(["git", *args], cwd=repo)',
        # A read-only subcommand needs no repository.
        'subprocess.run(["git", "rev-parse", "HEAD"])',
        'subprocess.run(["git", "--no-pager", "log", "-1"])',
        # `-c` takes its value as the NEXT element. Read as the subcommand, an
        # unresolvable value made every `git -c` call look able to write.
        'subprocess.run(["git", "-c", f"http.sslCAInfo={ca}", "ls-remote", url])',
        'subprocess.run(["git", "-c", setting, "-c", other, "rev-parse", "HEAD"])',
        # `--option=value` carries its own value, so the element after it is
        # not skipped.
        'subprocess.run(["git", "--config-env=http.proxy=P", "ls-remote", url])',
    ],
)
def test_a_call_that_names_its_repository_or_only_reads_is_allowed(call: str) -> None:
    assert not _problems(f"import subprocess\n{call}\n")


@pytest.mark.parametrize(
    "argv",
    [
        # The value skip must not swallow a WRITING subcommand into a global's slot.
        '["git", "-c", setting, "merge", "--abort"]',
        # A `-c` whose value is the last element leaves no subcommand to read,
        # and a skip that ran off the end would report the argv as running
        # nothing.
        '["git", "-c", setting, unresolvable]',
    ],
)
def test_a_global_option_s_value_never_hides_the_subcommand(argv: str) -> None:
    assert _problems(f"import subprocess\nsubprocess.run({argv})\n")


@pytest.mark.parametrize(
    "argv",
    [
        '["git", "clone", url]',
        '["git", "clone", "--quiet", "--no-hardlinks", str(origin), str(work)]',
        '["git", "init", str(path)]',
        '["git", "init", "--quiet", "--bare", str(origin)]',
        '["git", "init", "--quiet", "-b", "main", str(seed)]',
    ],
)
def test_a_subcommand_that_creates_its_own_repository_needs_no_naming(
    argv: str,
) -> None:
    assert not _problems(f"import subprocess\nsubprocess.run({argv})\n")


@pytest.mark.parametrize(
    "argv",
    [
        "['git', 'init']",
        "['git', 'init', '-q']",
        # `-b main` ends the argv in a BRANCH name, not a path.
        "['git', 'init', '-b', 'main']",
        # A literal relative path is still the process directory's neighbour.
        "['git', 'init', 'scratch']",
        # The subcommand is unresolvable, so `clone`/`init` cannot be read off it.
        '["git", *args, str(path)]',
    ],
)
def test_an_init_that_could_mean_the_process_directory_is_still_refused(
    argv: str,
) -> None:
    assert _problems(f"import subprocess\nsubprocess.run({argv})\n")


def test_the_marker_exempts_the_call_it_sits_above() -> None:
    source = (
        "import subprocess\n"
        "# cwd-git-ok: this one really does mean the process directory\n"
        'subprocess.run(["git", *args])\n'
    )
    assert not _problems(source)


def test_the_marker_on_the_same_line_exempts_the_call() -> None:
    source = (
        "import subprocess\n"
        'subprocess.run(["git", *args])  # cwd-git-ok: process directory\n'
    )
    assert not _problems(source)


def test_a_bare_marker_with_no_reason_does_not_exempt() -> None:
    """A marker without a reason is indistinguishable from a forgotten call
    site — the shared annotation matcher requires one."""
    source = "import subprocess\n# cwd-git-ok\nsubprocess.run(['git', *args])\n"
    assert _problems(source)


def test_an_unclassified_subcommand_fails_closed() -> None:
    """A git verb nobody has put in READ_ONLY must be treated as able to
    write, so the guard cannot be widened by adding a subcommand and
    nothing else."""
    assert _problems('import subprocess\nsubprocess.run(["git", "brand-new-verb"])\n')


def test_an_argv_with_no_subcommand_at_all_is_allowed() -> None:
    assert not _problems('import subprocess\nsubprocess.run(["git"])\n')


def test_an_argv_that_is_only_flags_has_no_subcommand_to_flag() -> None:
    assert not _problems('import subprocess\nsubprocess.run(["git", "--bare"])\n')


def test_a_syntax_error_scans_as_no_hits() -> None:
    """A file this check cannot parse names no problems rather than crashing
    the scan over one bad file."""
    assert mod.violations("def broken(:\n") == []


# ── --read-only-subcommand extension ───────────────────────────────────────


def test_read_only_subcommand_extends_the_default_set() -> None:
    src = "import subprocess\nsubprocess.run(['git', 'house-verb'])\n"
    assert mod.violations(src) != []
    assert mod.violations(src, mod.READ_ONLY | frozenset({"house-verb"})) == []


def test_the_default_read_only_set_is_never_narrowed_by_extension() -> None:
    """Extending must never drop a built-in read-only verb — the set is a
    union, not a replacement."""
    extended = mod.READ_ONLY | frozenset({"house-verb"})
    assert "rev-parse" in extended
    assert not mod.violations(
        "import subprocess\nsubprocess.run(['git', 'rev-parse', 'HEAD'])\n",
        extended,
    )


# ── main: argv/exit-code contract, and the flag ─────────────────────────────


def test_main_reports_and_exits_1_on_a_hit(tmp_path, capsys) -> None:
    p = tmp_path / "s.py"
    p.write_text('import subprocess\nsubprocess.run(["git", "merge", "--abort"])\n')
    assert mod.main([str(p)]) == 1
    assert f"{p}:2:" in capsys.readouterr().err


def test_main_clean_file_exits_0(tmp_path) -> None:
    p = tmp_path / "s.py"
    p.write_text('import subprocess\nsubprocess.run(["git", "-C", "r", "merge"])\n')
    assert mod.main([str(p)]) == 0


def test_main_with_no_files_refuses_and_exits_2(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_flags_only_with_no_files_also_refuses(capsys) -> None:
    """The empty-scan refusal must fire even when flags are present but no
    file follows them — argv is non-empty, so ``run_file_cli`` alone would
    not catch this."""
    assert mod.main(["--read-only-subcommand", "house-verb"]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_main_flag_reaches_the_scan_and_clears_a_house_verb(tmp_path, capsys) -> None:
    p = tmp_path / "s.py"
    p.write_text("import subprocess\nsubprocess.run(['git', 'house-verb'])\n")
    assert mod.main([str(p)]) == 1
    assert mod.main(["--read-only-subcommand", "house-verb", str(p)]) == 0


def test_main_reports_a_gone_file_as_no_hits(tmp_path) -> None:
    assert mod.main([str(tmp_path / "gone.py")]) == 0


# ── run_file_cli / empty-argv contract ───────────────────────────────────────


def test_module_run_with_truly_empty_argv_exits_2() -> None:
    import subprocess
    import sys

    from tests._helpers import HOOKS_DIR

    script = HOOKS_DIR / "check_cwd_scoped_git.py"
    proc = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr

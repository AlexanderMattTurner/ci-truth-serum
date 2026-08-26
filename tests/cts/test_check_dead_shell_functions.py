"""Tests for ci_truth_serum/check_dead_shell_functions.py — the lint that flags
a shell function this tree defines but nothing calls.

Drives ``find_dead()`` against a REAL git repo (the reference sweep comes from
`git ls-files`) and ``main()`` for the argv/exit-code contract.
"""

import subprocess
import sys
from pathlib import Path

from tests._helpers import REPO_ROOT, init_test_repo, load_hook

mod = load_hook("check_dead_shell_functions.py", "check_dead_shell_functions")


def _repo(tmp_path: Path) -> Path:
    init_test_repo(tmp_path)
    return tmp_path


def _write(repo: Path, rel: str, text: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _add(repo: Path) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)


def _p(repo: Path, rel: str) -> str:
    """An absolute argv path for REL — find_dead()/main() read the file
    directly, so a bare repo-relative string only works when the process
    CWD happens to already be REPO, which a test must not assume."""
    return str(repo / rel)


# ── the core question: is a function referenced anywhere in the tree? ──────
def test_uncalled_function_is_flagged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "dead_fn() {\n  echo bye\n}\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [(d.name, d.lineno) for d in dead] == [("dead_fn", 1)]


def test_function_called_in_the_same_file_is_live(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "live_fn() {\n  echo hi\n}\nlive_fn\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_function_called_from_a_different_tracked_file_is_live(tmp_path: Path) -> None:
    """Reference scope is the whole tracked tree, not just the argv files —
    a caller in a sibling script counts."""
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "helper() {\n  echo hi\n}\n")
    _write(repo, "run.sh", "source lib.sh\nhelper\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_function_called_only_from_a_workflow_run_block_is_live(tmp_path: Path) -> None:
    """A workflow `run:` block is not shell by suffix, but its text still
    counts as a reference — a caller need not be a shell file."""
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "deploy() {\n  echo hi\n}\n")
    _write(repo, "workflow.yaml", "jobs:\n  x:\n    steps:\n      - run: deploy\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_function_called_only_from_a_test_file_is_still_dead(tmp_path: Path) -> None:
    """A test file is excluded from the reference scan, so a function called
    only from tests/ counts as having no PRODUCTION caller."""
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "helper() {\n  echo hi\n}\n")
    _write(repo, "tests/test_lib.sh", "helper\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [(d.name, d.lineno) for d in dead] == [("helper", 1)]


def test_function_mentioned_only_in_a_doc_is_still_dead(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "helper() {\n  echo hi\n}\n")
    _write(repo, "docs/guide.md", "Call `helper` to do the thing.\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [(d.name, d.lineno) for d in dead] == [("helper", 1)]


# ── always-live ──────────────────────────────────────────────────────────
def test_main_is_always_live_by_default(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", 'main() {\n  echo hi\n}\nmain "$@"\n')
    _add(repo)
    # main() calls itself via `main "$@"` anyway, but the allowlist covers it
    # even when the file has no self-invocation.
    _write(repo, "entry.sh", "main() {\n  echo hi\n}\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "entry.sh")], repo) == []


def test_custom_always_live_name_suppresses(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "on_exit() {\n  echo bye\n}\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo, frozenset({"on_exit"})) == []
    assert mod.find_dead([_p(repo, "lib.sh")], repo) != []


# ── constructed-name dispatch ────────────────────────────────────────────
def test_dispatch_marker_suppresses_a_constructed_call(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo,
        "lib.sh",
        'run() {\n  local n="$1"\n  "ck_${n}"\n}\nck_hello() {\n  echo hi\n}\n',
    )
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    # `run` itself is still dead — only `ck_hello` gets the dispatch pass.
    assert [d.name for d in dead] == ["run"]


def test_dispatch_marker_needs_an_alnum_before_the_underscore(tmp_path: Path) -> None:
    """A leading-underscore name would produce the bare marker `_${`, which
    matches any `word_${var}` expansion and would spare every such function."""
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "_hello() {\n  echo hi\n}\nfoo_${bar}\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [d.name for d in dead] == ["_hello"]


# ── the grammar decides what a definition is ────────────────────────────
def test_signature_inside_a_heredoc_body_is_not_a_definition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(
        repo,
        "lib.sh",
        "cat <<EOF > doc.txt\nheredoc_fn() {\n  echo x\n}\nEOF\n",
    )
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_signature_inside_a_message_string_is_not_a_definition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", 'gb_warn "define foo() { :; } yourself"\n')
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_commented_out_signature_is_not_a_definition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "# old_fn() {\n#   echo x\n# }\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "lib.sh")], repo) == []


def test_name_mentioned_only_in_a_comment_is_still_dead(tmp_path: Path) -> None:
    """Comments are stripped from the reference scan too, so a doc-header
    restating the name cannot mask a dead function."""
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "# calls helper internally\nhelper() {\n  echo hi\n}\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [(d.name, d.lineno) for d in dead] == [("helper", 2)]


def test_function_keyword_form_is_recognized(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "function dead_fn {\n  echo bye\n}\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [(d.name, d.lineno) for d in dead] == [("dead_fn", 1)]


# ── scope: argv files, production shell only ────────────────────────────
def test_a_test_file_on_argv_defines_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "tests/test_lib.sh", "dead_fn() {\n  echo bye\n}\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "tests/test_lib.sh")], repo) == []


def test_a_non_shell_argv_file_defines_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "notes.md", "dead_fn() {\n  echo bye\n}\n")
    _add(repo)
    assert mod.find_dead([_p(repo, "notes.md")], repo) == []


def test_two_dead_functions_are_both_reported_sorted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "b_fn() {\n  :\n}\na_fn() {\n  :\n}\n")
    _add(repo)
    dead = mod.find_dead([_p(repo, "lib.sh")], repo)
    assert [d.name for d in dead] == ["a_fn", "b_fn"]


# ── main() argv/exit-code contract ──────────────────────────────────────
def test_main_empty_argv_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_dead_shell_functions"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr


def test_main_flags_only_no_files_exits_2(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert mod.main(["--repo-root", str(repo)]) == 2


def test_main_reports_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "dead_fn() {\n  echo bye\n}\n")
    _add(repo)
    assert mod.main(["--repo-root", str(repo), _p(repo, "lib.sh")]) == 1
    assert "lib.sh:1:" in capsys.readouterr().err


def test_main_clean_tree_exits_zero(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "live_fn() {\n  echo hi\n}\nlive_fn\n")
    _add(repo)
    assert mod.main(["--repo-root", str(repo), _p(repo, "lib.sh")]) == 0


def test_main_always_live_flag(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _write(repo, "lib.sh", "on_exit() {\n  echo bye\n}\n")
    _add(repo)
    assert (
        mod.main(
            ["--repo-root", str(repo), "--always-live", "on_exit", _p(repo, "lib.sh")]
        )
        == 0
    )


def test_main_skips_unreadable_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert mod.main(["--repo-root", str(repo), _p(repo, "nope.sh")]) == 0

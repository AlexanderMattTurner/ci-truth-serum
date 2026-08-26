"""Tests for ci_truth_serum/check_shell_source_declarations.py — the lint that
requires every `source`/`.` statement to be one shellcheck can follow.

Drives ``violations()`` for the parsing/resolution rules and ``main()`` for the
argv/exit-code contract. ``violations()`` takes the sourcing file's TEXT
directly, but resolution still walks the real filesystem, so every case below
gives it a `path=` under `tmp_path` (not necessarily written) and creates only
the target files a passing case needs to find.
"""

import subprocess
import sys
from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

mod = load_hook("check_shell_source_declarations.py", "check_shell_source_declarations")


def _mk(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _main_at(tmp_path: Path, rel: str) -> str:
    """A `path=` argument for a sourcing file at REL under TMP_PATH, without
    writing it — `violations()` reads its TEXT from the caller, but still
    resolves a target against this path's own directory."""
    return str(tmp_path / rel)


# ── directive resolves ────────────────────────────────────────────────────
def test_directive_resolving_against_own_directory_passes(tmp_path: Path) -> None:
    _mk(tmp_path, "lib/foo.sh", "true\n")
    src = '#!/bin/bash\n# shellcheck source=foo.sh\nsource "$DIR/foo.sh"\n'
    assert mod.violations(_main_at(tmp_path, "lib/main.sh"), src, tmp_path, []) == []


def test_directive_resolving_against_repo_root_passes(tmp_path: Path) -> None:
    _mk(tmp_path, "shared/foo.sh", "true\n")
    src = '# shellcheck source=shared/foo.sh\nsource "$DIR/foo.sh"\n'
    assert mod.violations(_main_at(tmp_path, "bin/main.sh"), src, tmp_path, []) == []


def test_directive_resolving_against_search_path_passes(tmp_path: Path) -> None:
    _mk(tmp_path, "hooks/foo.sh", "true\n")
    src = '# shellcheck source=foo.sh\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, ["hooks"]) == []


def test_search_path_given_as_absolute(tmp_path: Path) -> None:
    hooks = tmp_path / "elsewhere" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "foo.sh").write_text("true\n", encoding="utf-8")
    src = '# shellcheck source=foo.sh\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, [str(hooks)]) == []


def test_directive_naming_a_missing_path_is_flagged(tmp_path: Path) -> None:
    src = '# shellcheck source=nope.sh\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [2]
    assert "source=nope.sh" in hits[0][1]


# ── undeclared / disable ───────────────────────────────────────────────────
def test_computed_target_with_no_directive_is_undeclared(tmp_path: Path) -> None:
    src = 'source "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [1]
    assert "no `# shellcheck source=<path>` directive" in hits[0][1]


def test_disable_sc1090_suppresses_undeclared(tmp_path: Path) -> None:
    src = '# shellcheck disable=SC1090\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_disable_sc1091_suppresses_undeclared(tmp_path: Path) -> None:
    src = '# shellcheck disable=SC1091\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_disable_in_a_comma_list_still_counts(tmp_path: Path) -> None:
    src = '# shellcheck disable=SC2034,SC1090\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_unrelated_disable_code_does_not_suppress(tmp_path: Path) -> None:
    src = '# shellcheck disable=SC2034\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [2]


# ── literal targets ────────────────────────────────────────────────────────
def test_literal_target_with_no_directive_needs_no_directive(tmp_path: Path) -> None:
    _mk(tmp_path, "bin/foo.sh", "true\n")
    src = "source foo.sh\n"
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_literal_target_that_resolves_nowhere_is_flagged(tmp_path: Path) -> None:
    src = "source nope.sh\n"
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [1]
    assert "source nope.sh" in hits[0][1]


def test_directive_takes_priority_over_a_resolving_literal(tmp_path: Path) -> None:
    """A directive naming a bad path is still a finding even when the
    statement's own literal text would have resolved on its own — shellcheck
    reads the directive, not the statement, once one is present."""
    _mk(tmp_path, "bin/foo.sh", "true\n")
    src = "# shellcheck source=nope.sh\nsource foo.sh\n"
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [2]


# ── absolute targets are never findings ────────────────────────────────────
def test_dev_null_directive_is_never_a_finding(tmp_path: Path) -> None:
    src = '# shellcheck source=/dev/null\nsource "$DIR/foo.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_absolute_literal_target_is_never_a_finding(tmp_path: Path) -> None:
    path = _main_at(tmp_path, "bin/main.sh")
    src = "source /etc/does/not/exist.sh\n"
    assert mod.violations(path, src, tmp_path, []) == []


# ── the one-line-parent chain ───────────────────────────────────────────────
def test_directive_governs_a_source_after_a_one_line_and(tmp_path: Path) -> None:
    _mk(tmp_path, "bin/foo.sh", "true\n")
    src = (
        "# shellcheck source=foo.sh\n"
        '[[ -f "$DIR/foo.sh" ]] &&\n'
        '  source "$DIR/foo.sh"\n'
    )
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_directive_does_not_leak_to_a_later_statement(tmp_path: Path) -> None:
    """A directive governs only the statement it sits above — a second,
    undeclared `source` two lines later gets no free pass from it."""
    _mk(tmp_path, "bin/foo.sh", "true\n")
    src = '# shellcheck source=foo.sh\nsource "$DIR/foo.sh"\nsource "$DIR/bar.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, src, tmp_path, [])
    assert [line for line, _ in hits] == [3]


# ── shape checks: is this really a source statement? ───────────────────────
def test_source_quoted_in_a_string_is_not_a_statement(tmp_path: Path) -> None:
    src = 'echo "source $DIR/x.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_source_inside_a_message_call_is_not_a_statement(tmp_path: Path) -> None:
    src = 'gb_warn "source $DIR/x.sh"\n'
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_source_inside_a_heredoc_body_is_not_a_statement(tmp_path: Path) -> None:
    src = "cat <<EOF > doc.txt\nsource $DIR/x.sh\nEOF\n"
    path = _main_at(tmp_path, "bin/main.sh")
    assert mod.violations(path, src, tmp_path, []) == []


def test_dot_command_is_recognized_as_source(tmp_path: Path) -> None:
    path = _main_at(tmp_path, "bin/main.sh")
    hits = mod.violations(path, '. "$DIR/foo.sh"\n', tmp_path, [])
    assert [line for line, _ in hits] == [1]


# ── main() argv/exit-code contract ──────────────────────────────────────────
def test_main_empty_argv_exits_2() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "ci_truth_serum.check_shell_source_declarations"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "no files to scan" in proc.stderr


def test_main_flags_only_no_files_exits_2(tmp_path: Path) -> None:
    assert mod.main(["--repo-root", str(tmp_path)]) == 2


def test_main_reports_and_exits_nonzero(tmp_path: Path, capsys) -> None:
    p = _mk(tmp_path, "main.sh", 'source "$DIR/foo.sh"\n')
    assert mod.main(["--repo-root", str(tmp_path), str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path: Path) -> None:
    _mk(tmp_path, "foo.sh", "true\n")
    p = _mk(tmp_path, "main.sh", "source foo.sh\n")
    assert mod.main(["--repo-root", str(tmp_path), str(p)]) == 0


def test_main_search_path_flag_resolves(tmp_path: Path) -> None:
    _mk(tmp_path, "hooks/foo.sh", "true\n")
    p = _mk(tmp_path, "main.sh", '# shellcheck source=foo.sh\nsource "$DIR/foo.sh"\n')
    assert (
        mod.main(["--repo-root", str(tmp_path), "--search-path", "hooks", str(p)]) == 0
    )


def test_main_skips_unreadable_path(tmp_path: Path) -> None:
    assert mod.main(["--repo-root", str(tmp_path), str(tmp_path / "nope.sh")]) == 0


def test_main_repo_root_defaults_to_git_toplevel(capsys) -> None:
    """No `--repo-root` falls back to `git rev-parse --show-toplevel`, not a
    ported constant — this repo's own tracked shell files are the input."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ci_truth_serum.check_shell_source_declarations",
            "x.sh",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0  # x.sh does not exist — unreadable paths are skipped

"""Tests for hooks/check_substitution_exit_swallow.py — the pre-commit lint that
bans a structured-data producer (jq/yq) feeding a shell loop through a construct
that discards the producer's exit status (`done < <(jq …)` / `jq … | while`).

Drives `violations()` directly so each rule is asserted in isolation.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_substitution_exit_swallow.py"
mod = load_hook("check_substitution_exit_swallow.py", "check_substitution_exit_swallow")


# ── process-substitution stdin redirect: `… done < <(jq …)` ──────────────────
@pytest.mark.parametrize(
    "line",
    [
        # the canonical bug: a while-read loop fed by `< <(jq …)`
        'while IFS= read -r d; do allow "$d"; done < <(jq -r ".providers[]" "$f")',
        # yq is the other curated producer
        'while read -r x; do :; done < <(yq ".a[]" "$f")',
        # mapfile / readarray consumer, same swallow
        'mapfile -t hosts < <(jq -r ".hosts[]" "$f")',
        "readarray -t v < <(yq e '.x' f.yaml)",
        # `command jq` prefix must still be seen
        'while read -r d; do :; done < <(command jq -r ".a" "$f")',
        # extra whitespace between `<` and `<(`
        'done <    <(jq ".a" "$f")',
    ],
)
def test_fires_on_process_substitution(line: str) -> None:
    assert mod.violations(line) == [1]


# ── pipe-into-while: `jq … | while read` ─────────────────────────────────────
@pytest.mark.parametrize(
    "line",
    [
        'jq -r ".providers[]" "$f" | while read -r d; do allow "$d"; done',
        "yq '.a[]' f.yaml | while IFS= read -r x; do :; done",
        # a leading command separator puts jq at a command position
        'set -e; jq -r ".a" "$f" | while read -r d; do :; done',
        # `command jq` prefix
        'command jq ".a" "$f" | while read -r d; do :; done',
        # producer right after a `do`/`then` block keyword
        'if true; then jq ".a" f | while read -r d; do :; done; fi',
    ],
)
def test_fires_on_pipe_into_while(line: str) -> None:
    assert mod.violations(line) == [1]


# ── member-by-member: the curated set fires, the excluded set does not ────────
@pytest.mark.parametrize("producer", ["jq", "yq"])
def test_curated_producers_fire(producer: str) -> None:
    proc_sub = f'while read -r d; do :; done < <({producer} ".a" "$f")'
    pipe = f'{producer} ".a" "$f" | while read -r d; do :; done'
    assert mod.violations(proc_sub) == [1]
    assert mod.violations(pipe) == [1]


@pytest.mark.parametrize("producer", ["grep", "cat", "find", "sed", "awk"])
def test_excluded_producers_are_clean(producer: str) -> None:
    # These have routine, intentional non-zero exits (no-match, permission), so
    # flagging them would be noise — they are deliberately outside the set.
    proc_sub = f'while read -r d; do :; done < <({producer} ".a" "$f")'
    pipe = f'{producer} ".a" "$f" | while read -r d; do :; done'
    assert mod.violations(proc_sub) == []
    assert mod.violations(pipe) == []


# ── clean patterns ───────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        # the correct capture-then-iterate pattern observes jq's failure
        'out="$(jq -r ".a[]" "$f")" || die "jq failed"\n'
        'while IFS= read -r d; do :; done <<<"$out"',
        # here-string consumer (not a proc-sub), producer already captured
        'while read -r d; do :; done <<<"$providers"',
        # process substitution as a command ARGUMENT (not a stdin redirect) —
        # `diff <(jq a) <(jq b)` compares outputs; not the swallow this bans
        "diff <(jq '.a' x.json) <(jq '.a' y.json)",
        # jq is NOT the last stage before `while` — a deliberate false-negative
        # (some other command feeds the loop), so it must not fire
        'jq ".a" "$f"; other | while read -r d; do :; done',
        # whole-line comment, not real code
        "# while read; do :; done < <(jq .a f)  documents the bug",
        # a construct quoted inside a printed message is an example
        'echo "bad: done < <(jq .a f)"',
        'die "jq .a f | while read fails open"',
        # a heredoc `<<` (no gap) is not a `< <(` proc-sub redirect
        "cat <<EOF",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


# ── opt-out annotation (reason REQUIRED) ─────────────────────────────────────
def test_same_line_annotation_with_reason_suppresses() -> None:
    line = (
        'mapfile -t h < <(jq -r ".h[]" "$f")  '
        "# allow-substitution-exit: empty ⇒ fewer allowed hosts, fail-safe"
    )
    assert mod.violations(line) == []


def test_preceding_line_annotation_with_reason_suppresses() -> None:
    text = (
        "# allow-substitution-exit: empty ⇒ more restrictive, safe\n"
        'mapfile -t h < <(jq -r ".h[]" "$f")'
    )
    assert mod.violations(text) == []


@pytest.mark.parametrize(
    "annotation",
    [
        "# allow-substitution-exit",  # bare, no colon
        "# allow-substitution-exit:",  # colon, no reason
        "# allow-substitution-exit:   ",  # colon, only whitespace
    ],
)
def test_reasonless_annotation_does_not_suppress(annotation: str) -> None:
    line = f'mapfile -t h < <(jq -r ".h[]" "$f")  {annotation}'
    assert mod.violations(line) == [1]


def test_annotation_two_lines_above_does_not_suppress() -> None:
    text = '# allow-substitution-exit: reason\nx=1\nmapfile -t h < <(jq -r ".h[]" "$f")'
    assert mod.violations(text) == [3]


# ── main() wiring: exit code + file:line message ─────────────────────────────
def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour is covered in test_linecheck.py; here we
    only pin that main() emits THIS message with a navigable file:line prefix."""
    bad = tmp_path / "bad.sh"
    bad.write_text(
        'while read -r d; do :; done < <(jq -r ".a[]" "$f")\n', encoding="utf-8"
    )
    assert mod.main([str(bad)]) == 1
    err = capsys.readouterr().err
    assert f"{bad}:1:" in err
    assert "jq/yq exit status is discarded" in err


def test_main_clean_file_returns_zero(tmp_path: Path) -> None:
    good = tmp_path / "good.sh"
    good.write_text(
        'out="$(jq -r ".a[]" "$f")" || die\nwhile read -r d; do :; done <<<"$out"\n',
        encoding="utf-8",
    )
    assert mod.main([str(good)]) == 0


def _is_shell(path: Path) -> bool:
    """Match the pre-commit hook's `types: [shell]` selection."""
    if path.suffix in (".bash", ".sh"):
        return True
    if path.suffix:
        return False
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except (OSError, IndexError):
        return False
    return bool(first) and first[0].startswith("#!") and "sh" in first[0]


def test_own_shell_tree_is_clean() -> None:
    """ci-truth-serum's own shell hooks must pass the lint. Scoped to hooks/."""
    tracked = subprocess.check_output(
        ["git", "ls-files", "hooks/"], text=True, cwd=REPO_ROOT
    ).split()
    offenders = []
    for rel in tracked:
        path = REPO_ROOT / rel
        if not _is_shell(path):
            continue
        hits = mod.violations(path.read_text(encoding="utf-8", errors="replace"))
        offenders += [f"{rel}:{n}" for n in hits]
    assert offenders == [], f"unannotated jq/yq exit-swallowing construct: {offenders}"

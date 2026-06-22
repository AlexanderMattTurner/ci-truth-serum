"""Tests for hooks/check_stderr_suppression.py — the pre-commit lint that bans
stderr suppression on container launch/build commands.

Drives `violations()` directly so each rule is asserted in isolation.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

_SRC = HOOKS_DIR / "check_stderr_suppression.py"
mod = load_hook("check_stderr_suppression.py", "check_stderr_suppression")


@pytest.mark.parametrize(
    "line",
    [
        'devcontainer up "${args[@]}" 2>/dev/null || rc=$?',
        "devcontainer build . 2>/dev/null",
        "docker compose -f x.yml up -d 2> /dev/null",
        "docker compose build app 2>/dev/null",
        "docker-compose up 2>/dev/null",
        "docker build -t img . 2>/dev/null",
        "docker buildx build --platform linux/amd64 . 2>/dev/null",
        # &>/dev/null (stdout+stderr) is equally opaque on a launch command
        'devcontainer up "${args[@]}" &>/dev/null || rc=$?',
        "docker compose -f x.yml up -d &> /dev/null",
        "docker build -t img . &>/dev/null",
    ],
)
def test_fires_on_literal_launchers(line: str) -> None:
    assert mod.violations(line) == [1]


def test_fires_on_array_variable_launcher() -> None:
    # The invocation line names no launcher — only the array does. The two-pass
    # scan must connect `DC=(docker compose …)` to `"${DC[@]}" up`.
    text = 'DC=(docker compose -p proj -f x.yml)\n"${DC[@]}" up -d 2>/dev/null\n'
    assert mod.violations(text) == [2]


def test_array_build_verb_also_fires() -> None:
    text = 'COMPOSE=(docker-compose)\n"${COMPOSE[@]}" build 2>/dev/null\n'
    assert mod.violations(text) == [2]


def test_fires_on_array_variable_launcher_ampersand() -> None:
    text = 'DC=(docker compose -p proj -f x.yml)\n"${DC[@]}" up -d &>/dev/null\n'
    assert mod.violations(text) == [2]


@pytest.mark.parametrize(
    "text",
    [
        # opt-out annotation on the same line (both suppression forms)
        "docker compose up 2>/dev/null  # allow-stderr-suppress: probe only",
        "docker compose up &>/dev/null  # allow-stderr-suppress: probe only",
        # whole-line comment, not real code
        "# docker compose up 2>/dev/null is bad",
        # suppression but not a launch/build verb (a probe/exec)
        "docker compose exec app test -f /x 2>/dev/null",
        "command -v docker 2>/dev/null",
        "command -v docker &>/dev/null",
        # a launch with no suppression
        "docker compose up -d",
        # array launcher invoked without suppression
        'DC=(docker compose)\n"${DC[@]}" up -d',
        # an unrelated array (not a launcher) used with `up`/`build` words
        'opts=(--build)\n"${opts[@]}" 2>/dev/null',
        # `--build` is a flag to `run`, not the `build` subcommand — don't fire
        "docker compose run --build svc 2>/dev/null",
        # a launcher quoted inside a printed message is an example, not a command
        'echo "run: docker compose up 2>/dev/null"',
        'warn "docker build -t img . 2>/dev/null fails on a bad base"',
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


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


def test_main_wires_violations_and_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """main() runs this script's detector through the shared loop with its own
    message. The generic loop behaviour is covered once in test_linecheck.py;
    here we only pin that main() emits THIS message."""
    bad = tmp_path / "bad.sh"
    bad.write_text("docker build -t img . 2>/dev/null\n", encoding="utf-8")
    assert mod.main([str(bad)]) == 1
    assert f"{bad}:1: stderr suppressed" in capsys.readouterr().err


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
    assert offenders == [], (
        f"unannotated launch-command stderr suppression: {offenders}"
    )

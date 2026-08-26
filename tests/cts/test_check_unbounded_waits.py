"""Tests for ci_truth_serum/check_unbounded_waits.py — the lint that bans a bare
remote `git` call (ls-remote/fetch/clone/push/pull) with no wall-clock bound.

Drives ``violations()`` for the parsing rules and ``main()`` for the argv/exit-
code contract.
"""

import pytest

from tests._helpers import load_hook

mod = load_hook("check_unbounded_waits.py", "check_unbounded_waits")


@pytest.mark.parametrize(
    "line",
    [
        # one bare invocation per remote subcommand
        "git fetch origin",
        "git ls-remote origin",
        "git clone https://example.com/x y",
        "git push origin main",
        "git pull --ff-only origin main",
        # indentation does not excuse it
        "      git fetch origin",
        # transparent leading keywords are stripped: git is still the first
        # real word
        "if ! git fetch origin; then",
        "while ! git pull origin main; do sleep 1; done",
        # env-assignment prefixes do not bound the command
        "FOO=bar git fetch origin",
        'GIT_SSH_COMMAND="ssh -i k" git push origin main',
        # a value-taking global option consumes its value; the subcommand
        # still counts
        'git -C "$repo" fetch origin',
        "git -c protocol.version=2 push origin main",
        "git --git-dir=/r/.git fetch origin",
        # command substitution opens a fresh command word
        'out="$(git ls-remote origin)"',
        # after a boolean/pipe separator, git is a new simple command
        "check_ok && git fetch origin",
        "prep | git push origin main",
        # an unregistered wrapper does not bound it — sudo passes straight
        # through, and only a REGISTERED bounding wrapper suppresses the hit
        "sudo git fetch origin",
        'export_bounded git ls-remote "$remote"',
    ],
)
def test_fires_on_unbounded_remote_git(line: str) -> None:
    assert mod.violations(line) == [1]


@pytest.mark.parametrize(
    "text",
    [
        # a bounding wrapper placed before git bounds it
        "timeout 30 git fetch origin",
        'timeout "${_TIMEOUT:-30}" git push origin main',
        "sudo timeout 30 git fetch origin",
        # a different identifier, not `git` at all
        "git_remote fetch origin",
        # a dynamic subcommand is not a literal remote verb
        'git "$@"',
        "git ${subcmd} origin",
        # local subcommands never wedge and are out of scope
        "git rev-parse HEAD",
        'git -C "$root" status --porcelain',
        "git log --oneline -1",
        "git commit -m msg",
        "git worktree add -q wt -b b base",
        # a near-miss token is not the subcommand
        "git fetchall origin",
        "mygit fetch origin",
        # git inside a message string is an ARGUMENT — the command word is
        # echo/die, never git
        'echo "run git fetch origin manually"',
        'die "cannot reach origin (git ls-remote exited $rc)"',
        # git quoted inside a non-message command's argument
        'grep -q "(git fetch origin)" "$log"',
        # env-assignment whose value merely mentions git
        'MSG="git push origin failed"',
        # other CLIs are out of scope
        'sbx exec "$name" some-cmd',
        "docker info",
        # same-line opt-out annotation (reason required)
        "git fetch origin  # allow-unbounded: fetches a local mirror, no network",
        # a comment citing the banned form is documentation, not code
        "# git fetch origin would hang on a wedged remote",
        # no git at all
        "curl -sS https://example.com",
        # the inert body of a quoted-delimiter heredoc is text the script prints
        "cat <<'EOF' >/tmp/help\ngit fetch origin\nEOF",
    ],
)
def test_clean_lines_do_not_fire(text: str) -> None:
    assert mod.violations(text) == []


def test_backslash_continuation_is_one_logical_command() -> None:
    text = "git \\\n  fetch origin\n"
    assert mod.violations(text) == [1]


def test_wrapper_across_continuation_is_bounded() -> None:
    text = "timeout 30 \\\n  git fetch origin\n"
    assert mod.violations(text) == []


def test_non_vacuity_the_data_cases_still_fire_as_real_commands() -> None:
    """Positive marker for the message/heredoc clean cases above: the very same
    words, spelled as a real command, still fire."""
    assert mod.violations("git fetch origin\n") == [1]
    assert mod.violations(
        'echo "run git fetch origin manually"\ngit fetch origin\n'
    ) == [2]


# ── opt-out placement ─────────────────────────────────────────────────────
def test_opt_out_requires_a_reason() -> None:
    assert mod.violations("git fetch origin  # allow-unbounded:\n") == [1]


def test_opt_out_on_local_path_clone() -> None:
    text = (
        "# allow-unbounded: clone from a local path, no network\ngit clone -- /a /b\n"
    )
    assert mod.violations(text) == []


def test_opt_out_does_not_reach_past_an_intervening_line() -> None:
    text = "# allow-unbounded: something else\ndo_a_real_thing\ngit fetch origin\n"
    assert mod.violations(text) == [3]


# ── extensibility flags ────────────────────────────────────────────────────
def test_bounding_wrapper_flag_extends_the_built_in_set() -> None:
    text = "retry_bounded 30 git fetch origin\n"
    assert mod.violations(text) == [1]
    assert (
        mod.violations(text, bounding_wrappers=frozenset({"timeout", "retry_bounded"}))
        == []
    )


def test_remote_subcommand_flag_extends_the_built_in_set() -> None:
    text = "git bundle-fetch origin\n"
    assert mod.violations(text) == []
    assert mod.violations(
        text, remote_subcommands=frozenset({"fetch", "bundle-fetch"})
    ) == [1]


# ── main ─────────────────────────────────────────────────────────────────
def test_main_reports_and_exits_nonzero(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text("git fetch origin\n")
    assert mod.main([str(p)]) == 1
    assert f"{p}:1:" in capsys.readouterr().err


def test_main_clean_file_exits_zero(tmp_path) -> None:
    p = tmp_path / "s.sh"
    p.write_text("timeout 30 git fetch origin\n")
    assert mod.main([str(p)]) == 0


def test_main_bounding_wrapper_flag_suppresses_a_hit(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text("retry_bounded 30 git fetch origin\n")
    assert mod.main([str(p)]) == 1
    assert mod.main(["--bounding-wrapper", "retry_bounded", str(p)]) == 0


def test_main_remote_subcommand_flag_adds_a_hit(tmp_path, capsys) -> None:
    p = tmp_path / "s.sh"
    p.write_text("git bundle-fetch origin\n")
    assert mod.main([str(p)]) == 0
    assert mod.main(["--remote-subcommand", "bundle-fetch", str(p)]) == 1

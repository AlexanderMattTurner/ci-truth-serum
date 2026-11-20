"""Tests for ci_truth_serum/check_path_list_closure.py — the lint that flags a
hand-kept list of shipped repo paths that omits a path the shipped code names.

Drives ``find()`` against a REAL git repo, because `git ls-files` decides what a
member and a reference name, and ``main()`` for the argv and exit-code contract.
"""

import subprocess
from pathlib import Path

import pytest

from tests._helpers import init_test_repo, load_hook

mod = load_hook("check_path_list_closure.py", "check_path_list_closure")

# The motivating shape: a payload that ships `bin/lib` whole and picks three
# files out of `.claude/hooks` by hand.
_PAYLOAD = (
    "PAYLOAD=(\n"
    "  bin/lib\n"
    "  # the masker the recorder loads\n"
    "  .claude/hooks/secret_env_vars.py\n"
    "  .claude/hooks/redactor_config.py\n"
    ")\n"
)

_SPAWN = 'def env(root):\n    return {"MONITOR": f"{root}/.claude/hooks/monitor.py"}\n'


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    init_test_repo(tmp_path)
    tracked = {
        ".claude/hooks/secret_env_vars.py": "",
        ".claude/hooks/redactor_config.py": "",
        ".claude/hooks/monitor.py": "",
        "bin/other.sh": "#!/bin/bash\n",
        **files,
    }
    for rel, text in tracked.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _omitted(repo: Path, list_file: str = "bin/lib/env.bash") -> list[tuple]:
    findings, refused = mod.find([str(repo / list_file)], repo)
    assert refused is False
    return [
        (f.listing.name, f.path, [(s.consumer, s.line) for s in f.sites])
        for f in findings
    ]


# ── the core question: does shipped code name a sibling the list left out? ──
def test_f_string_tail_after_a_root_is_an_omission(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/spawn.py", 2)])
    ]


def test_listing_the_path_clears_the_omission(tmp_path: Path) -> None:
    listed = _PAYLOAD.replace(")\n", "  .claude/hooks/monitor.py\n)\n")
    repo = _repo(tmp_path, {"bin/lib/env.bash": listed, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo) == []


def test_a_literal_append_adds_its_members(tmp_path: Path) -> None:
    appended = _PAYLOAD + "PAYLOAD+=(.claude/hooks/monitor.py)\n"
    repo = _repo(tmp_path, {"bin/lib/env.bash": appended, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo) == []


@pytest.mark.parametrize(
    "spelling",
    [
        'root / ".claude" / "hooks" / "monitor.py"',
        'os.path.join(root, ".claude", "hooks", "monitor.py")',
        '".claude/hooks/monitor.py"',
        '"./.claude/hooks/monitor.py"',
    ],
)
def test_python_spellings_of_the_same_path(tmp_path: Path, spelling: str) -> None:
    consumer = f"import os\n\nPROBE = {spelling}\n"
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/probe.py": consumer})
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/probe.py", 3)])
    ]


@pytest.mark.parametrize(
    "line",
    [
        'python3 "$root/.claude/hooks/monitor.py"',
        "python3 ${root}/.claude/hooks/monitor.py",
        "python3 '.claude/hooks/monitor.py'",
    ],
)
def test_shell_spellings_of_the_same_path(tmp_path: Path, line: str) -> None:
    repo = _repo(
        tmp_path,
        {"bin/lib/env.bash": _PAYLOAD, "bin/lib/run.bash": f"#!/bin/bash\n{line}\n"},
    )
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/run.bash", 2)])
    ]


@pytest.mark.parametrize(
    "body",
    [
        "# python3 .claude/hooks/monitor.py\n",
        "cat >out <<'EOF'\n.claude/hooks/monitor.py\nEOF\n",
        'echo "start .claude/hooks/monitor.py first"\n',
        'python3 "$root/$sub/monitor.py"\n',
    ],
)
def test_shell_text_that_is_not_a_path_literal_is_quiet(
    tmp_path: Path, body: str
) -> None:
    """A comment, a heredoc body and a message are not code that names a path,
    and a path with a second expansion in it is computed."""
    repo = _repo(
        tmp_path,
        {"bin/lib/env.bash": _PAYLOAD, "bin/lib/run.bash": f"#!/bin/bash\n{body}"},
    )
    assert _omitted(repo) == []


def test_a_docstring_is_prose(tmp_path: Path) -> None:
    consumer = '"""Start .claude/hooks/monitor.py first."""\n\n"""x"""\n'
    consumer += '\ndef f():\n    """.claude/hooks/monitor.py"""\n'
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/probe.py": consumer})
    assert _omitted(repo) == []


# ── the precision rules: what a finding needs besides a missing path ──────────
def test_a_directory_the_list_never_picks_from_is_quiet(tmp_path: Path) -> None:
    """`docs/` holds no member, so a path there is a separate decision."""
    consumer = 'GUIDE = "docs/guide.md"\n'
    repo = _repo(
        tmp_path,
        {
            "bin/lib/env.bash": _PAYLOAD,
            "bin/lib/probe.py": consumer,
            "docs/guide.md": "",
        },
    )
    assert _omitted(repo) == []


def test_a_directory_member_does_not_pick_its_parent(tmp_path: Path) -> None:
    """`bin/lib` is a member, so `bin/other.sh` beside it is not an omission."""
    consumer = '#!/bin/bash\n"$root/bin/other.sh"\n'
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/run.bash": consumer})
    assert _omitted(repo) == []


def test_a_consumer_the_list_does_not_ship_is_quiet(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "tools/spawn.py": _SPAWN})
    assert _omitted(repo) == []


def test_an_untracked_path_is_not_an_omission(tmp_path: Path) -> None:
    consumer = 'X = ".claude/hooks/gone.py"\n'
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/probe.py": consumer})
    assert _omitted(repo) == []


@pytest.mark.parametrize(
    "listing",
    [
        # Two members: a pair, not a manifest.
        "P=(bin/lib .claude/hooks/secret_env_vars.py)\n",
        # A glob member makes the list a pattern set.
        "P=(bin/lib .claude/hooks/*.py .claude/hooks/redactor_config.py)\n",
        # A member that names nothing tracked.
        "P=(bin/lib .claude/hooks/secret_env_vars.py .claude/hooks/nope.py)\n",
        # No directory member: the list ships no tree of code.
        (
            "P=(.claude/hooks/secret_env_vars.py .claude/hooks/redactor_config.py"
            " bin/other.sh)\n"
        ),
        # A computed member: the list is not literal.
        "P=(bin/lib .claude/hooks/secret_env_vars.py $EXTRA)\n",
    ],
)
def test_a_list_that_is_not_a_path_list_does_not_qualify(
    tmp_path: Path, listing: str
) -> None:
    repo = _repo(tmp_path, {"bin/lib/env.bash": listing, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo) == []


def test_a_list_in_a_test_file_is_not_read(tmp_path: Path) -> None:
    """A test's list names what the test scans, not what ships."""
    listing = (
        "SCANNED = (\n    'bin/lib',\n    '.claude/hooks/secret_env_vars.py',\n"
        "    '.claude/hooks/redactor_config.py',\n)\n"
    )
    repo = _repo(tmp_path, {"tests/test_scan.py": listing, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo, "tests/test_scan.py") == []


@pytest.mark.parametrize("wrapper", ["frozenset({...})", "(...)", "[...]", "{...}"])
def test_python_collection_literals_are_path_lists(
    tmp_path: Path, wrapper: str
) -> None:
    members = (
        '"bin/lib", ".claude/hooks/secret_env_vars.py", '
        '".claude/hooks/redactor_config.py"'
    )
    listing = f"PAYLOAD: frozenset[str] = {wrapper.replace('...', members)}\n"
    repo = _repo(tmp_path, {"bin/lib/ship.py": listing, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo, "bin/lib/ship.py") == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/spawn.py", 2)])
    ]


def test_one_omission_is_one_finding_with_every_site(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        {
            "bin/lib/env.bash": _PAYLOAD,
            "bin/lib/spawn.py": _SPAWN,
            "bin/lib/doctor.py": 'P = ".claude/hooks/monitor.py"\n',
        },
    )
    assert _omitted(repo) == [
        (
            "PAYLOAD",
            ".claude/hooks/monitor.py",
            [("bin/lib/doctor.py", 1), ("bin/lib/spawn.py", 2)],
        )
    ]


# ── the opt-out ───────────────────────────────────────────────────────────────
def test_opt_out_on_the_consumer_line_clears_that_line(tmp_path: Path) -> None:
    doctor = (
        "# path-list-closure-ok: the host doctor probes its own checkout\n"
        'P = ".claude/hooks/monitor.py"\n'
    )
    repo = _repo(
        tmp_path,
        {
            "bin/lib/env.bash": _PAYLOAD,
            "bin/lib/spawn.py": _SPAWN,
            "bin/lib/doctor.py": doctor,
        },
    )
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/spawn.py", 2)])
    ]


def test_opt_out_without_a_reason_does_not_clear(tmp_path: Path) -> None:
    doctor = 'P = ".claude/hooks/monitor.py"  # path-list-closure-ok\n'
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/doctor.py": doctor})
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/doctor.py", 1)])
    ]


def test_opt_out_inside_a_string_does_not_clear(tmp_path: Path) -> None:
    doctor = (
        'HINT = "# path-list-closure-ok: write this"\nP = ".claude/hooks/monitor.py"\n'
    )
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/doctor.py": doctor})
    assert _omitted(repo) == [
        ("PAYLOAD", ".claude/hooks/monitor.py", [("bin/lib/doctor.py", 2)])
    ]


# ── main(): argv, exit codes, and loud refusal ────────────────────────────────
def test_main_reports_at_the_list_and_exits_1(tmp_path: Path, capsys) -> None:
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, "bin/lib/spawn.py": _SPAWN})
    rc = mod.main(["--repo-root", str(repo), str(repo / "bin/lib/env.bash")])
    err = capsys.readouterr().err
    assert rc == 1
    assert err.startswith(
        "bin/lib/env.bash:1: `PAYLOAD` omits `.claude/hooks/monitor.py`"
    )
    assert "  named at bin/lib/spawn.py:2\n" in err


def test_main_on_a_clean_list_exits_0(tmp_path: Path, capsys) -> None:
    listed = _PAYLOAD.replace(")\n", "  .claude/hooks/monitor.py\n)\n")
    repo = _repo(tmp_path, {"bin/lib/env.bash": listed, "bin/lib/spawn.py": _SPAWN})
    rc = mod.main(["--repo-root", str(repo), str(repo / "bin/lib/env.bash")])
    assert (rc, capsys.readouterr().err) == (0, "")


def test_main_with_no_files_refuses(capsys) -> None:
    assert mod.main([]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_an_unparseable_shipped_consumer_is_refused_loudly(
    tmp_path: Path, capsys
) -> None:
    """A shipped file the grammar cannot read gives no references, so the run
    must not read as a pass."""
    listed = _PAYLOAD.replace(")\n", "  .claude/hooks/monitor.py\n)\n")
    repo = _repo(
        tmp_path,
        {"bin/lib/env.bash": listed, "bin/lib/broken.py": "def f(:\n"},
    )
    rc = mod.main(["--repo-root", str(repo), str(repo / "bin/lib/env.bash")])
    assert rc == 1
    assert capsys.readouterr().err.startswith("bin/lib/broken.py: ")


def test_an_unparseable_list_file_is_refused_not_raised(tmp_path: Path) -> None:
    """Found by the fuzz suite: `find` raised SyntaxError on a list file that
    was not Python, so only `main`'s pre-check stood between it and a crash."""
    repo = _repo(tmp_path, {"bin/lib/ship.py": "P=(lib a/b.py c/d.py)\n"})
    findings, refused = mod.find([str(repo / "bin/lib/ship.py")], repo)
    assert (findings, refused) == ([], True)


# ── regressions: each case once reported a path that the list covers, or a
# path that is not the checkout's ──────────────────────────────────────────────
@pytest.mark.parametrize(
    ("rel", "text"),
    [
        ("bin/lib/run.bash", '#!/bin/bash\ncat "$HOME/.claude/hooks/monitor.py"\n'),
        ("bin/lib/run.bash", '#!/bin/bash\ncat "$TMPDIR/.claude/hooks/monitor.py"\n'),
        ("bin/lib/p.py", 'P = Path.home() / ".claude" / "hooks" / "monitor.py"\n'),
        ("bin/lib/p.py", 'P = f"{home}/.claude/hooks/monitor.py"\n'),
        (
            "bin/lib/p.py",
            'P = Path(__file__).parent / ".claude" / "hooks" / "monitor.py"\n',
        ),
    ],
)
def test_a_root_that_is_not_the_checkout_is_not_read(
    tmp_path: Path, rel: str, text: str
) -> None:
    repo = _repo(tmp_path, {"bin/lib/env.bash": _PAYLOAD, rel: text})
    assert _omitted(repo) == []


@pytest.mark.parametrize(
    ("rel", "listing"),
    [
        (
            "bin/lib/env.bash",
            _PAYLOAD + 'SHIPPED=("${PAYLOAD[@]}" .claude/hooks/monitor.py)\n',
        ),
        (
            "bin/lib/ship.py",
            'BASE = ("bin/lib", ".claude/hooks/secret_env_vars.py",'
            ' ".claude/hooks/redactor_config.py")\n'
            'SHIPPED = (*BASE, ".claude/hooks/monitor.py")\n',
        ),
    ],
)
def test_a_list_that_another_list_splices_in_is_not_read(
    tmp_path: Path, rel: str, listing: str
) -> None:
    repo = _repo(tmp_path, {rel: listing, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo, rel) == []


@pytest.mark.parametrize(
    "addition",
    [
        'P += [".claude/hooks/monitor.py"]',
        'P.append(".claude/hooks/monitor.py")',
        'P.extend([".claude/hooks/monitor.py"])',
    ],
)
def test_a_python_addition_adds_its_members(tmp_path: Path, addition: str) -> None:
    listing = (
        'P = ["bin/lib", ".claude/hooks/secret_env_vars.py",'
        f' ".claude/hooks/redactor_config.py"]\n{addition}\n'
    )
    repo = _repo(tmp_path, {"bin/lib/ship.py": listing, "bin/lib/spawn.py": _SPAWN})
    assert _omitted(repo, "bin/lib/ship.py") == []


def test_the_inner_steps_of_a_slash_chain_are_not_paths(tmp_path: Path) -> None:
    """`root / ".claude" / "hooks" / "gone.json"` once also spelled the prefix
    `.claude/hooks`, which is tracked, although the code names only an untracked
    file below it."""
    listing = "PAYLOAD=(bin/lib .claude/secret.py .claude/other.py)\n"
    consumer = 'P = root / ".claude" / "hooks" / "gone.json"\n'
    repo = _repo(
        tmp_path,
        {
            "bin/lib/env.bash": listing,
            "bin/lib/p.py": consumer,
            ".claude/secret.py": "",
            ".claude/other.py": "",
        },
    )
    assert _omitted(repo) == []


@pytest.mark.parametrize(
    "listing",
    [
        # A computed member spoils the name, so the append cannot stand in.
        'P=(bin/lib .claude/hooks/secret_env_vars.py "$x")\n'
        "P+=(bin/lib .claude/hooks/redactor_config.py config)\n",
        # A later `=` replaces an earlier append, the way bash runs it.
        "P+=(bin/lib .claude/hooks/secret_env_vars.py .claude/hooks/redactor_config.py)\n"
        'P=(bin/lib .claude/hooks/secret_env_vars.py "$x")\n',
    ],
)
def test_an_append_never_stands_in_for_an_unread_list(
    tmp_path: Path, listing: str
) -> None:
    repo = _repo(
        tmp_path,
        {"bin/lib/env.bash": listing, "bin/lib/spawn.py": _SPAWN, "config/a": ""},
    )
    assert _omitted(repo) == []

"""Tests for hooks/check_env_symmetry.py — the (extra) whole-tree scan that flags
a prefixed env var written but never read, or read but never written (the
half-finished-rename signature).

Drives the pure detectors (find_writes/find_reads/collect_optouts/analyze) on
fixture strings, and main() against a real throwaway git tree."""


import pytest

from tests._helpers import commit_all, init_test_repo, load_hook

es = load_hook("check_env_symmetry.py", "check_env_symmetry")

P = "GLOVEBOX_"


# ── find_writes ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "is_yaml", "expected"),
    [
        ("export GLOVEBOX_FOO=1", False, {"GLOVEBOX_FOO"}),
        ("GLOVEBOX_FOO=bar run_thing", False, {"GLOVEBOX_FOO"}),
        ('os.environ["GLOVEBOX_FOO"] = "x"', False, {"GLOVEBOX_FOO"}),
        ("env:\n  GLOVEBOX_FOO: value\n", True, {"GLOVEBOX_FOO"}),
        # a YAML `X: value` is only a write in a YAML file
        ("GLOVEBOX_FOO: value\n", False, set()),
    ],
)
def test_find_writes(text, is_yaml, expected):
    assert es.find_writes(text, P, is_yaml) == expected


@pytest.mark.parametrize(
    "text",
    [
        '[[ "$GLOVEBOX_FOO" == yes ]]',  # comparison, not assignment
        "echo $GLOVEBOX_FOO",  # expansion (a read), not a write
        "${GLOVEBOX_FOO:-default}",  # parameter expansion
    ],
)
def test_find_writes_ignores_reads_and_comparisons(text):
    assert es.find_writes(text, P, False) == set()


# ── find_reads ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("echo $GLOVEBOX_FOO", {"GLOVEBOX_FOO"}),
        ("${GLOVEBOX_FOO:-x}", {"GLOVEBOX_FOO"}),
        ('os.environ["GLOVEBOX_FOO"]', {"GLOVEBOX_FOO"}),
        ('os.environ.get("GLOVEBOX_FOO")', {"GLOVEBOX_FOO"}),
        ('os.getenv("GLOVEBOX_FOO")', {"GLOVEBOX_FOO"}),
        ("process.env.GLOVEBOX_FOO", {"GLOVEBOX_FOO"}),
        ('process.env["GLOVEBOX_FOO"]', {"GLOVEBOX_FOO"}),
    ],
)
def test_find_reads(text, expected):
    assert es.find_reads(text, P) == expected


def test_name_not_mis_split_by_lowercase():
    """GLOVEBOX_Foo is not the env var GLOVEBOX_F — a trailing lowercase letter
    means it is not an all-uppercase env token."""
    assert es.find_writes("GLOVEBOX_Foo=1", P, False) == set()
    assert es.find_reads("$GLOVEBOX_Foo", P) == set()


def test_unrelated_prefix_ignored():
    assert es.find_writes("export OTHER_FOO=1", P, False) == set()
    assert es.find_reads("$OTHER_FOO", P) == set()


# ── collect_optouts ──────────────────────────────────────────────────────────
def test_collect_optouts_requires_name_and_reason():
    assert es.collect_optouts(
        "# env-symmetry-ok: GLOVEBOX_EXT supplied by the CI environment"
    ) == {"GLOVEBOX_EXT"}
    # name but no reason → not an opt-out
    assert es.collect_optouts("# env-symmetry-ok: GLOVEBOX_EXT") == set()
    assert es.collect_optouts("# env-symmetry-ok:") == set()


# ── analyze ──────────────────────────────────────────────────────────────────
def test_write_only_is_flagged():
    result = es.analyze({"a.sh": "export GLOVEBOX_NEW=1\n"}, P)
    assert result == [("GLOVEBOX_NEW", "write-only", ["a.sh"])]


def test_read_only_is_flagged():
    result = es.analyze({"b.py": 'os.environ.get("GLOVEBOX_OLD")\n'}, P)
    assert result == [("GLOVEBOX_OLD", "read-only", ["b.py"])]


def test_symmetric_var_is_clean():
    src = {"a.sh": "GLOVEBOX_OK=1\n", "b.sh": 'echo "$GLOVEBOX_OK"\n'}
    assert es.analyze(src, P) == []


def test_write_and_read_across_files_pair_up():
    """The whole point: a write in one file and a read in another are symmetric."""
    src = {
        "writer.sh": "export GLOVEBOX_TOKEN=abc\n",
        "reader.py": 'os.getenv("GLOVEBOX_TOKEN")\n',
    }
    assert es.analyze(src, P) == []


def test_incomplete_rename_flags_both_halves():
    """Renamed in the writer but not the reader: the new name is write-only and
    the old name is read-only — both surface."""
    src = {
        "writer.sh": "export GLOVEBOX_NEWNAME=1\n",
        "reader.sh": 'echo "$GLOVEBOX_OLDNAME"\n',
    }
    assert es.analyze(src, P) == [
        ("GLOVEBOX_NEWNAME", "write-only", ["writer.sh"]),
        ("GLOVEBOX_OLDNAME", "read-only", ["reader.sh"]),
    ]


def test_optout_suppresses_named_var():
    src = {
        "a.sh": "export GLOVEBOX_EXT=1\n",
        "doc.md": "# env-symmetry-ok: GLOVEBOX_EXT consumed by an external tool\n",
    }
    assert es.analyze(src, P) == []


def test_files_are_sorted_and_deduped():
    src = {
        "z.sh": "export GLOVEBOX_X=1\n",
        "a.sh": "GLOVEBOX_X=2 run\n",
    }
    assert es.analyze(src, P) == [("GLOVEBOX_X", "write-only", ["a.sh", "z.sh"])]


# ── main() over a real git tree ──────────────────────────────────────────────
def test_main_flags_violation_in_tree(tmp_path, monkeypatch, capsys):
    init_test_repo(tmp_path)
    (tmp_path / "writer.sh").write_text("export GLOVEBOX_NEW=1\n")
    commit_all(tmp_path)
    monkeypatch.setattr(es, "REPO_ROOT", tmp_path)
    assert es.main(["--prefix", "GLOVEBOX_"]) == 1
    out = capsys.readouterr().out
    assert "GLOVEBOX_NEW" in out
    assert "WRITTEN" in out


def test_main_clean_tree_returns_zero(tmp_path, monkeypatch):
    init_test_repo(tmp_path)
    (tmp_path / "a.sh").write_text('export GLOVEBOX_OK=1\necho "$GLOVEBOX_OK"\n')
    commit_all(tmp_path)
    monkeypatch.setattr(es, "REPO_ROOT", tmp_path)
    assert es.main(["--prefix", "GLOVEBOX_"]) == 0


def test_main_ignores_untracked_files(tmp_path, monkeypatch):
    """Only tracked files are scanned — an untracked scratch file can't trip it."""
    init_test_repo(tmp_path)
    (tmp_path / "tracked.sh").write_text('export GLOVEBOX_OK=1\necho "$GLOVEBOX_OK"\n')
    commit_all(tmp_path)
    (tmp_path / "scratch.sh").write_text("export GLOVEBOX_UNTRACKED=1\n")
    monkeypatch.setattr(es, "REPO_ROOT", tmp_path)
    assert es.main(["--prefix", "GLOVEBOX_"]) == 0


def test_main_requires_prefix(tmp_path, monkeypatch, capsys):
    init_test_repo(tmp_path)
    commit_all(tmp_path)
    monkeypatch.setattr(es, "REPO_ROOT", tmp_path)
    with pytest.raises(SystemExit):
        es.main([])
    assert "prefix" in capsys.readouterr().err.lower()

"""Tests for ci_truth_serum/check_test_helper_kwargs.py — the lint that flags
a call in a scanned file that passes a keyword its resolvable helper `def`
does not accept.

Each case builds a small git-tracked tree and asks the real `findings()`/
`main()` for its verdict, so the tests bind to what the check reports rather
than to its source text.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import commit_all, init_test_repo, load_hook

mod = load_hook("check_test_helper_kwargs.py", "check_test_helper_kwargs")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    init_test_repo(tmp_path)
    return tmp_path


def _track(repo_dir: Path, rel: str, text: str) -> None:
    path = repo_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    commit_all(repo_dir, f"add {rel}")


def _problems(hits) -> list[tuple[str, str]]:
    return [(h.callee, h.problem) for h in hits]


def _findings(repo: Path, argv_rel: list[str], packages: tuple[str, ...] = ("tests",)):
    return mod.findings([str(repo / rel) for rel in argv_rel], repo, packages)


# --------------------------------------------------------------------------- #
# cross-file resolution — the argv contract this port adds.
# --------------------------------------------------------------------------- #
def test_flags_a_keyword_the_helper_does_not_declare(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    hits = _findings(repo, ["tests/test_x.py"])
    assert _problems(hits) == [("stub", "has no parameter `darwin`")]


def test_accepts_the_keyword_the_helper_declares(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, kvm=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_the_helper_module_is_resolved_even_when_never_named_on_argv(
    repo: Path,
) -> None:
    """`_h.py` is never scanned itself — only `test_x.py` is on argv — but its
    signature still governs the verdict, because the whole helper-package
    tree is walked to build the resolver."""
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    hits = _findings(repo, ["tests/test_x.py"])
    assert hits and hits[0].path.endswith("test_x.py")


def test_a_second_helper_package_is_resolved_too(repo: Path) -> None:
    _track(repo, "libhelpers/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from libhelpers._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    hits = _findings(repo, ["tests/test_x.py"], packages=("tests", "libhelpers"))
    assert _problems(hits) == [("stub", "has no parameter `darwin`")]


def test_a_package_not_named_in_helper_package_is_not_resolved(repo: Path) -> None:
    _track(repo, "libhelpers/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from libhelpers._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"], packages=("tests",)) == []


def test_a_relative_import_is_left_alone(repo: Path) -> None:
    """`from . import stub` names no module, so the callee cannot be resolved
    to a definite `def` — the check skips it rather than guessing."""
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from . import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_kwargs_signature_accepts_anything(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, **kw):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, anything=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_positional_only_name_is_not_a_keyword(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, /, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, p=2)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "has no parameter `p`")
    ]


def test_an_as_alias_resolves_to_the_name_the_source_module_defines(
    repo: Path,
) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub as helper\n\n\ndef test_a():\n"
        "    helper(1, darwin=True)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("helper", "has no parameter `darwin`")
    ]


def test_a_pytest_fixture_is_never_judged_against_its_def(repo: Path) -> None:
    _track(
        repo,
        "tests/test_x.py",
        "import pytest\n\n\n@pytest.fixture\ndef pr_repo(tmp_path):\n"
        "    def _make(path, msg):\n        return path, msg\n\n    return _make\n\n\n"
        "def test_a(pr_repo):\n    pr_repo('README.md', msg='docs: change')\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_mapping_expansion_supplies_names_the_check_cannot_see(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    extra = {'kvm': True}\n    stub(1, **extra)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_same_named_local_shadows_the_helper(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    stub = lambda **kw: kw\n    stub(darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_parameter_of_the_same_name_shadows_the_helper(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a(stub):\n    stub(darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_nested_def_of_the_same_name_shadows_the_helper(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    def stub(**kw):\n        return kw\n\n    stub(darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_lambda_parameter_of_the_same_name_shadows_the_helper(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    return sorted([], key=lambda stub: stub(1, darwin=True))\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_vararg_of_the_same_name_shadows_the_helper(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a(*stub):\n    stub(darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_an_allow_comment_opts_one_call_out(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    stub(1, darwin=True)  # allow-helper-kwargs: deliberate\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_an_allow_comment_on_the_line_above_also_opts_out(repo: Path) -> None:
    """`annotated_near`'s placement rule accepts a reason on the line above a
    single-line call, matching every other opt-out in this pack."""
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    # allow-helper-kwargs: deliberate\n    stub(1, darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_flags_a_missing_required_argument(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub()\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [("stub", "needs `p`")]


def test_a_default_makes_a_parameter_optional(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, q=2):\n    return p, q\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_keyword_only_parameter_without_a_default_is_required(repo: Path) -> None:
    _track(
        repo, "tests/_h.py", "def stub(*, kvm, quiet=True):\n    return kvm, quiet\n"
    )
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(quiet=False)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [("stub", "needs `kvm`")]


def test_flags_too_many_positional_arguments(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, 2)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "takes 1 positional argument(s), called with 2")
    ]


def test_a_star_args_signature_takes_any_count(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *rest):\n    return p, rest\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, 2, 3)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_star_expansion_hides_the_positional_count(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    items = [1, 2, 3]\n    stub(*items)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_mapping_expansion_may_supply_a_required_name(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm):\n    return p, kvm\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    extra = {'kvm': True}\n    stub(1, **extra)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_positional_argument_satisfies_its_parameter(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p, kvm\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_decorated_def_is_judged_on_keywords_but_not_arity(repo: Path) -> None:
    _track(
        repo,
        "tests/_h.py",
        "def deco(f):\n    return f\n\n\n@deco\ndef stub(p, *, kvm=False):\n"
        "    return p, kvm\n",
    )
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(darwin=True)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "has no parameter `darwin`")
    ]


def test_a_name_defined_twice_in_one_module_is_not_judged(repo: Path) -> None:
    _track(
        repo,
        "tests/test_x.py",
        "def home():\n    return 1\n\n\nHOME = home()\n\n\n"
        "def home(front, tmp):\n    return front, tmp\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_a_file_that_does_not_parse_is_skipped(repo: Path) -> None:
    """A tree holding one unparsable helper module still gets a verdict on the
    rest of it."""
    _track(repo, "tests/_broken.py", "def stub(:\n")
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "has no parameter `darwin`")
    ]


def test_a_same_module_def_is_resolved_without_an_import(repo: Path) -> None:
    _track(
        repo,
        "tests/test_x.py",
        "def stub(p, *, kvm=False):\n    return p\n\n\ndef test_a():\n"
        "    stub(1, darwin=True)\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "has no parameter `darwin`")
    ]


def test_an_own_def_wins_over_an_import_of_the_same_name(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef stub(p, *, darwin=False):\n    return p\n"
        "\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    assert _findings(repo, ["tests/test_x.py"]) == []


def test_an_allow_comment_without_a_reason_does_not_exempt(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n"
        "    stub(1, darwin=True)  # allow-helper-kwargs\n",
    )
    assert _problems(_findings(repo, ["tests/test_x.py"])) == [
        ("stub", "has no parameter `darwin`")
    ]


def test_a_missing_helper_package_fails_closed(repo: Path) -> None:
    """An rglob over a directory that does not exist yields nothing, so a
    wrong `--helper-package` would report zero findings and exit 0 forever —
    `main` refuses instead of silently scanning nothing."""
    _track(repo, "tests/test_x.py", "def test_a():\n    pass\n")
    with pytest.raises(SystemExit) as exc:
        mod.main(
            [
                str(repo / "tests/test_x.py"),
                "--repo-root",
                str(repo),
                "--helper-package",
                "absent",
            ]
        )
    assert "is not a directory" in str(exc.value)


# --------------------------------------------------------------------------- #
# main: argv/exit-code contract.
# --------------------------------------------------------------------------- #
def test_main_reports_a_finding_as_a_failure(repo: Path, capsys) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, darwin=True)\n",
    )
    rc = mod.main([str(repo / "tests/test_x.py"), "--repo-root", str(repo)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "test_x.py:5: stub() has no parameter `darwin`" in err
    assert "allow-helper-kwargs: <reason>" in err


def test_main_clean_tree_exits_0(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    _track(
        repo,
        "tests/test_x.py",
        "from tests._h import stub\n\n\ndef test_a():\n    stub(1, kvm=True)\n",
    )
    assert mod.main([str(repo / "tests/test_x.py"), "--repo-root", str(repo)]) == 0


def test_main_no_paths_exits_2_with_a_message(repo: Path, capsys) -> None:
    assert mod.main(["--repo-root", str(repo)]) == 2
    assert "no files to scan" in capsys.readouterr().err


def test_a_non_python_argv_path_is_ignored(repo: Path) -> None:
    _track(repo, "tests/_h.py", "def stub(p, *, kvm=False):\n    return p\n")
    readme = repo / "README.md"
    readme.write_text("stub(1, darwin=True)\n", encoding="utf-8")
    rc = mod.main([str(readme), "--repo-root", str(repo), "--helper-package", "tests"])
    assert rc == 0


def test_empty_argv_exits_2_via_cli_contract() -> None:
    result = subprocess.run(
        [sys.executable, mod.__file__], capture_output=True, check=False
    )
    assert result.returncode == 2


def test_the_real_tree_is_clean() -> None:
    """No grandfathered baseline exists, so this is the whole contract: every
    hit on this pack's own tests/ tree is a defect someone must fix."""
    repo_root = Path(__file__).resolve().parents[2]
    argv = [str(p) for p in (repo_root / "tests").rglob("*.py")]
    hits = mod.findings(argv, repo_root, ("tests",))
    assert hits == []

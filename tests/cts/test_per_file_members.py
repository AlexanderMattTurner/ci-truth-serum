"""What a `per_file` mark claims, and how each claim is held to account.

`run_tier` drives a marked member one file at a time, so each file is parsed
once and every member after the first reads the tree already in the cache. The
mark claims two things, and this module tests both.

FINDINGS. The member must report the same thing driven either way. One that
compares files to each other, counts across the tree, or sweeps for references
does not: driven per file it still exits 0 while reporting none of its
cross-file findings, which is the false green this package exists to refuse.

COST. The member's `main` must do no tree-scale work of its own, because here
that work runs once per file rather than once per run. Measured on this
repository, `check_duplicate_class_names` took 144x longer driven per file and
`check_test_helper_kwargs` 47x, both because `main` re-derived the tracked tree
on every call. Neither carries the mark.

The findings test cannot rest on this repository alone. The tree is clean — the
package lints itself — so comparing findings over it compares two empty sets and
proves nothing. `REFUSALS` below therefore carries a positive fixture for three
marked members, one per file class and detector style, so the per-file driving
is proven to report on each of those shapes. Every other mark rests on the sweep
over this tree, so a NEW mark is not proven to report until it gains a fixture
here.
"""

import contextlib
import importlib
import io
import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import HOOKS_DIR, REPO_ROOT, load_hook

registry = load_hook("_cts_registry.py", "cts_registry_per_file")
run_tier = load_hook("run_tier.py", "run_tier_per_file")

PER_FILE = sorted(registry.PER_FILE)
KIND = {c.module: c.kind for c in registry.CHECKS}


def _findings(module: str, argv: list[str]) -> set[str]:
    """Every line MODULE reports on stderr for ARGV, run the way `run_tier` runs it."""
    sys.path.insert(0, str(HOOKS_DIR))
    check = importlib.import_module(f"ci_truth_serum.{module}")
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        with contextlib.suppress(SystemExit):
            check.main(list(argv))
    return {line for line in err.getvalue().split("\n") if line.strip()}


@pytest.fixture(scope="module")
def tracked() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [str(REPO_ROOT / rel) for rel in out]


def test_a_marked_member_reports_the_same_findings_either_way(
    tracked: list[str],
) -> None:
    """Over this repository, the whole list and one file at a time agree.

    A regression net rather than the proof: this tree is clean, so most members
    report nothing here. `test_a_marked_member_can_be_made_to_report` is what
    stops that emptiness from being the whole test.
    """
    differed = {}
    compared: list[str] = []
    for module in PER_FILE:
        corpus = [path for path in tracked if run_tier.matches(path, KIND[module])]
        if not corpus:
            continue
        whole = _findings(module, corpus)
        per_file: set[str] = set()
        for path in corpus:
            per_file |= _findings(module, [path])
        compared.append(module)
        if whole != per_file:
            differed[module] = (
                sorted(whole - per_file)[:2],
                sorted(per_file - whole)[:2],
            )
    assert not differed, f"marked members that report differently per file: {differed}"
    # `assert not differed` alone also passes over a sweep that compared nothing:
    # a broken `matches`, an empty `PER_FILE`, or a `tracked` that returned no
    # files would each satisfy it while checking no member at all.
    assert compared, "no marked member had a file of its kind in this tree"


# One file each member objects to. The point is only that the member reports
# something, so the shortest refusal it has is the right fixture.
REFUSALS: dict[str, tuple[str, str]] = {
    "check_unspecified_encoding": ("x.py", "open('f')\n"),
    "check_bare_mkdir": ("x.sh", "#!/usr/bin/env bash\nmkdir -p /tmp/x\necho done\n"),
    "check_historical_comments": ("x.py", "# we used to return None here\nx = 1\n"),
}


@pytest.mark.parametrize("module", sorted(REFUSALS))
def test_a_marked_member_can_be_made_to_report(module: str, tmp_path: Path) -> None:
    """A member that reports nothing proves nothing above, so prove it reports.

    Each case is a member of a different file class and a different detector
    style — a Python AST pass, a shell grammar pass, a comment pass — so the
    per-file driving is exercised on each shape rather than on one.
    """
    assert module in registry.PER_FILE, f"{module} is no longer marked per_file"
    name, body = REFUSALS[module]
    target = tmp_path / name
    target.write_text(body, encoding="utf-8")

    second = tmp_path / f"second_{name}"
    second.write_text(body, encoding="utf-8")

    whole = _findings(module, [str(target), str(second)])
    assert whole, f"{module} did not object to its own fixture — the fixture is stale"
    assert len(whole) == 2, f"{module} reported {whole} over two copies of one refusal"

    per_file = _findings(module, [str(target)]) | _findings(module, [str(second)])
    # Two violating files, so both sides are non-empty and the comparison is
    # real. This is also what catches a member that accumulates across calls:
    # driven one file at a time it is called twice, and a module-level tally
    # would make the second call report the first file again.
    assert whole == per_file, (
        f"{module} reports differently per file. Only whole-list: "
        f"{sorted(whole - per_file)}. Only per-file: {sorted(per_file - whole)}."
    )


def test_a_marked_member_does_no_tree_scale_work_per_call(tmp_path: Path) -> None:
    """No marked member may shell out to git while judging one file.

    That is the cost the mark forbids, and it is what made the unmarked members
    slow: `main` re-derived the tracked tree on every call, which is once per
    file here. Watching for a `git` subprocess catches the whole class, where a
    timing bar would only catch it on a tree large enough to hurt.
    """
    sample = {
        "shell": ("probe.sh", "#!/usr/bin/env bash\necho hi\n"),
        "python": ("probe.py", "x = 1\n"),
        "markdown": ("probe.md", "# title\n"),
        "javascript": ("probe.mjs", "export const x = 1;\n"),
        "dockerfile": ("Dockerfile", "FROM scratch\n"),
    }
    for name, body in sample.values():
        (tmp_path / name).write_text(body, encoding="utf-8")

    real_run = subprocess.run
    called: list[str] = []

    def watch(cmd, *args, **kwargs):  # noqa: ANN001,ANN202
        if cmd and str(cmd[0]).endswith("git"):
            called.append(current[0])
        return real_run(cmd, *args, **kwargs)

    current = [""]
    ran_git = set()
    refused_a_lone_file = {}
    for module in PER_FILE:
        paths = [
            str(tmp_path / name)
            for name, _ in sample.values()
            if run_tier.matches(str(tmp_path / name), KIND[module])
        ]
        if not paths:
            continue
        current[0] = module
        subprocess.run = watch
        try:
            _findings(module, paths[:1])
        except Exception as blew_up:  # pylint: disable=broad-except
            # A member that cannot judge ONE file on its own is not a per-file
            # map either, whatever it does with the whole list. Reported here
            # rather than raised, so one bad mark names itself instead of
            # ending the run on a traceback from inside the member.
            refused_a_lone_file[module] = f"{type(blew_up).__name__}: {blew_up}"
        finally:
            subprocess.run = real_run
        ran_git |= set(called)
        called.clear()

    assert not ran_git, (
        "these members run git while judging one file, so driving them per file "
        f"runs it once per file: {sorted(ran_git)}"
    )
    assert not refused_a_lone_file, (
        "these members are marked `per_file` but raised when handed one file: "
        f"{refused_a_lone_file}"
    )

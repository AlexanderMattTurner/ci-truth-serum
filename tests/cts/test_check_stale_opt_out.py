"""Tests for ci_truth_serum/check_stale_opt_out.py — the check that reports an
opt-out annotation its own check no longer needs.

Each fixture holds one LIVE annotation (the check fires on its line when opt-outs
are ignored), one STALE annotation (nothing below it trips the check), and the
token written where it is not a comment at all (a string, a heredoc body, a quoted
YAML value). Each test asserts the exact stale set, so a live annotation called
stale and a non-comment called an annotation both fail it.
"""

import re
import subprocess
from pathlib import Path

import pytest

from tests._helpers import load_hook

mod = load_hook("check_stale_opt_out.py", "check_stale_opt_out")
linecheck = load_hook("_cts_linecheck.py", "_cts_linecheck_for_audit")

_REPORT = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+): the opt-out `(?P<token>[^`]+)`"
)

FIXTURES = {
    "shell": (
        "run.sh",
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        "make build || true # allow-exit-suppress: CI reports a failed build itself\n"
        "rm -f x 2>/dev/null || true # allow-exit-suppress: the output is discarded\n"
        "cat <<'EOF' >doc.txt\n"
        "run || true # allow-exit-suppress: a heredoc line is data\n"
        "EOF\n"
        'echo "annotate with # allow-exit-suppress: a reason"\n',
        {("run.sh", 4, "allow-exit-suppress")},
    ),
    "python": (
        "tests/test_wait.py",
        "import time\n"
        "\n"
        "\n"
        "def test_x():\n"
        "    # allow-sleep: the subject is the one-second granularity\n"
        "    time.sleep(1.1)\n"
        "    assert two_incidents()\n"
        "\n"
        "\n"
        "def test_y():\n"
        "    # allow-sleep: the sleep that was here is gone\n"
        "    assert True\n"
        '    hint = "see # allow-sleep: a string holds no comment"\n',
        {("tests/test_wait.py", 11, "allow-sleep")},
    ),
    "javascript": (
        "tests/clock.test.mjs",
        "const started = Date.now();\n"
        "// allow-wall-clock: the subject is the timeout\n"
        'assert.ok(Date.now() - started < 1000, "too slow");\n'
        "// allow-wall-clock: nothing below reads the clock\n"
        "assert.ok(true);\n"
        'const hint = "see // allow-wall-clock: a string holds no comment";\n',
        {("tests/clock.test.mjs", 4, "allow-wall-clock")},
    ),
    # A `run:` value that `per_run_script` reads as decoded text: the record
    # credits the whole value, and a YAML comment outside it reaches nothing.
    "workflow run script": (
        ".github/workflows/ci.yaml",
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: slurp\n"
        "        run: |\n"
        "          # allow-gh-slurp-jq: the pinned gh predates --slurp with --jq\n"
        '          gh api "repos/$R" --slurp --jq .\n'
        "      # allow-gh-slurp-jq: this step makes no gh call\n"
        '      - name: "# allow-gh-slurp-jq: a quoted value"\n'
        "        run: echo hello\n",
        {(".github/workflows/ci.yaml", 9, "allow-gh-slurp-jq")},
    ),
    # A check that reads the workflow file whole, in file line numbers.
    "workflow file": (
        ".github/workflows/release.yaml",
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          # main-push-ok: the release reads the tag off main\n"
        "          git push origin HEAD:main\n"
        "      - run: |\n"
        "          # main-push-ok: this step pushes a branch\n"
        "          git push origin HEAD:feature\n",
        {(".github/workflows/release.yaml", 9, "main-push-ok")},
    ),
    "prose": (
        "docs/tags.md",
        "<!-- allow-stray-markup: this page shows the literal tag -->\n"
        "</invoke>\n"
        "\n"
        "<!-- allow-stray-markup: no tag follows this line -->\n"
        "Plain text.\n",
        {("docs/tags.md", 4, "allow-stray-markup")},
    ),
}


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def _stale(err: str) -> set[tuple[str, int, str]]:
    return {
        (m["path"], int(m["line"]), m["token"])
        for m in map(_REPORT.match, err.splitlines())
        if m
    }


@pytest.fixture(name="repo")
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # check_argument_exit_swallow lists the tracked tree, so the cwd is a repo.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.parametrize("family", sorted(FIXTURES))
def test_reports_exactly_the_stale_annotation(
    family: str, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rel, text, expected = FIXTURES[family]
    _write(repo, rel, text)
    status = mod.main([rel])
    err = capsys.readouterr().err
    assert _stale(err) == expected, err
    assert status == 1


def test_a_file_whose_annotations_are_all_live_passes(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "ok.sh", "#!/bin/bash\nmake || true # allow-exit-suppress: why\n")
    assert mod.main(["ok.sh"]) == 0
    assert capsys.readouterr().err == ""


def test_a_file_the_check_refuses_fails_loud_and_is_not_judged(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The `]` in the expansion pattern collapses the bash grammar's parse.
    _write(
        repo,
        "broken.sh",
        '#!/bin/bash\nworkload="${workload%%]*}"\n'
        "echo hi # allow-exit-suppress: would be stale\n",
    )
    assert mod.main(["broken.sh"]) == 1
    err = capsys.readouterr().err
    assert _stale(err) == set()
    assert "broken.sh: " in err and "opt-outs not judged" in err


def test_an_audit_ignores_opt_outs_and_records_what_it_read() -> None:
    lines = ["make || true # allow-x: why", "rm f"]
    said = {1: "# allow-x: why"}
    assert linecheck.annotated_near(lines, 1, "allow-x", comments=said)
    with linecheck.audit_opt_outs() as record:
        assert not linecheck.annotated_near(lines, 1, "allow-x", comments=said)
        assert not linecheck.annotated_near(lines, 2, "allow-x", comments={})
        assert linecheck.annotated(said[1], "allow-y") is False
    assert (record.lines, record.whole_file) == ({(1, "allow-x")}, {"allow-y"})
    assert linecheck.annotated_near(lines, 1, "allow-x", comments=said)


def test_coverage_excludes_a_check_that_reads_files_outside_the_commit() -> None:
    covered = {owner.check.module for owner in mod.owners()}
    assert {"check_exit_suppression", "check_argument_exit_swallow"} <= covered
    assert "check_relative_imports" not in covered
    assert "check_path_gate_deps" not in covered

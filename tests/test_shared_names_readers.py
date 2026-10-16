"""Every reader of shared-names.json must find each name that it asks for.

A template sync replaced `shared-names.json` and `shared-names.bash` with the
template's copies, and those copies had no `commit_status_marks` key. This
repository still runs its own `auto-resolve/discover.py`, which reads that key
when it loads. So the discover job stopped with a KeyError on every pull
request. The node suites that run discover.py are not in this repository's
`pnpm test`, so the sync pull request merged with every check green.

These tests run each reader, so a sync that drops a name that a reader still
asks for fails on the sync pull request:

- A Python reader is a tracked `.py` file with the string `shared-names.json`
  in its syntax tree. The test loads it as a module, which runs its reads.
- A bash reader is a tracked shell file that sources `shared-names.bash`. The
  test sources it, and then checks each `_UPPER_CASE` variable that the file
  expands and does not assign. That is the naming form of every variable that
  `shared-names.bash` sets, and a reader can expand one inside a function,
  where sourcing alone does not reach it.
- A JS reader has no probe yet, so the test refuses one until it gets a probe.
"""

import ast
import re
import subprocess
import sys

import pytest

from ci_truth_serum import _cts_bash_ast, _cts_js_ast
from tests._helpers import REPO_ROOT

NAMES_FILE = "shared-names.json"
BASH_READER = "shared-names.bash"
# The form of every variable that shared-names.bash sets.
SHARED_VARIABLE = re.compile(r"_[A-Z][A-Z0-9_]*")


def _tracked(*patterns: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def _python_readers() -> list[str]:
    return [
        path
        for path in _tracked("*.py")
        if any(
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.endswith(NAMES_FILE)
            for node in ast.walk(
                ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
            )
        )
    ]


def _sources_shared_names(root) -> bool:
    return any(
        words[0] in ("source", ".") and any(BASH_READER in word for word in words[1:])
        for words in map(
            _cts_bash_ast.command_words, _cts_bash_ast.iter_nodes(root, "command")
        )
        if words
    )


def _bash_readers() -> dict[str, list[str]]:
    """Each bash reader, with the shared variables that it reads."""
    readers = {}
    for path in _tracked("*.sh", "*.bash"):
        if path.endswith(BASH_READER):
            continue
        root = _cts_bash_ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"))
        if not _sources_shared_names(root):
            continue
        assigned = {
            _cts_bash_ast.node_text(node.child_by_field_name("name"))
            for node in _cts_bash_ast.iter_nodes(root, "variable_assignment")
        }
        expanded = {
            _cts_bash_ast.node_text(name)
            for node in _cts_bash_ast.iter_nodes(root, "simple_expansion", "expansion")
            for name in node.named_children
            if name.type == "variable_name"
        }
        readers[path] = sorted(
            name for name in expanded - assigned if SHARED_VARIABLE.fullmatch(name)
        )
    return readers


def test_the_readers_are_found() -> None:
    # Non-vacuity: the tests below pass over an empty set.
    assert _python_readers() == [".github/scripts/auto-resolve/discover.py"]
    readers = _bash_readers()
    assert ".github/scripts/lib/commit-status-mark.bash" in readers
    assert all(readers.values()), readers


@pytest.mark.parametrize("path", _python_readers())
def test_a_python_reader_finds_its_names(path: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='reader')",
            str(REPO_ROOT / path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("path", "names"), sorted(_bash_readers().items()))
def test_a_bash_reader_finds_its_names(path: str, names: list[str]) -> None:
    checks = "".join(
        f'[[ -v {name} ]] || {{ echo "unset: {name}"; exit 1; }}\n' for name in names
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail\nsource "$1"\n{checks}',
            "_",
            str(REPO_ROOT / path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_js_file_reads_the_names() -> None:
    readers = [
        path
        for path in _tracked("*.js", "*.mjs", "*.cjs", "*.ts")
        if any(
            NAMES_FILE in _cts_bash_ast.node_text(node)
            for node in _cts_bash_ast.iter_nodes(
                _cts_js_ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"), path),
                "string",
                "template_string",
            )
        )
    ]
    assert readers == [], (
        f"{readers} read {NAMES_FILE}; add a probe for a JS reader to this file"
    )

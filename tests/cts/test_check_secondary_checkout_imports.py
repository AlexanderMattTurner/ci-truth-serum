"""Tests for ci_truth_serum/check_secondary_checkout_imports.py — the workflow
lint that fails a job running a Node script out of a secondary
`actions/checkout` `path:` tree while installing no Node dependencies, when
that script carries a bare import.

Drives `executions` (the YAML half), `scripts_run_by_shell` (the shell hop)
and `is_bare` directly, plus `main()` end to end against a real git repo under
`tmp_path` — `tracked_files()` shells out to `git ls-files`, so every fixture
is a real commit. The red case is the one the check exists for: a changelog
gate that `exec node`s a script importing `smol-toml` from a pinned copy of the
CI scripts, in a job that set up `uv` and nothing for Node.
"""

from pathlib import Path

import pytest

from tests._helpers import commit_all, init_test_repo, load_hook

mod = load_hook(
    "check_secondary_checkout_imports.py", "check_secondary_checkout_imports"
)

# The shape that went red: the workflow's own tree is never checked out, the
# scripts come from a `_ci_scripts` copy of the default branch, and the gate is
# a shell script whose last line hands a variable-built path to node.
GATE_SH = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    'SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
    'exec node "$SCRIPTS/checks/changelog-fragment.mjs"\n'
)
FRAGMENT_MJS = (
    'import { readFileSync } from "node:fs";\n'
    'import path from "path";\n'
    'import { parse as parseToml } from "smol-toml";\n'
    'import { fragments } from "./lib/fragments.mjs";\n'
    "console.log(parseToml(readFileSync(path.resolve('x.toml'), 'utf8')), fragments);\n"
)


def _workflow(
    *extra_steps: str,
    run: str = "bash _ci_scripts/.github/scripts/pr/changelog-gate.sh",
) -> str:
    steps = "".join(extra_steps)
    return (
        "on: pull_request\n"
        "jobs:\n"
        "  changelog_fragment:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: main\n"
        "          path: _ci_scripts\n"
        f"{steps}"
        "      - uses: ./.github/actions/uv-setup-retry\n"
        f"      - run: {run}\n"
    )


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _repo(tmp_path: Path, workflow: str, fragment: str = FRAGMENT_MJS) -> Path:
    init_test_repo(tmp_path)
    _write(tmp_path, ".github/workflows/pr-meta.yaml", workflow)
    _write(tmp_path, ".github/scripts/pr/changelog-gate.sh", GATE_SH)
    _write(tmp_path, ".github/scripts/checks/changelog-fragment.mjs", fragment)
    _write(
        tmp_path,
        ".github/scripts/checks/lib/fragments.mjs",
        "export const fragments = 1;\n",
    )
    commit_all(tmp_path)
    return tmp_path


# ── is_bare ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "specifier",
    ["smol-toml", "@octokit/rest", "yaml", "lodash/fp", "fs-extra?v=2"],
)
def test_a_package_specifier_is_bare(specifier: str):
    assert mod.is_bare(specifier)


@pytest.mark.parametrize(
    "specifier",
    [
        "./x.mjs",
        "../x.mjs",
        ".",
        "..",
        "/abs/x.mjs",
        "node:fs",
        "fs",
        "fs/promises",
        "path",
        "#internal/x",
        "data:text/javascript,1",
        "file:///x.mjs",
    ],
)
def test_a_relative_absolute_builtin_or_subpath_specifier_is_not_bare(specifier: str):
    assert not mod.is_bare(specifier)


def test_every_node_builtin_is_not_bare_with_or_without_the_prefix():
    assert mod.NODE_BUILTINS, (
        "the builtin set is empty — every bare check below would fire"
    )
    for name in mod.NODE_BUILTINS:
        assert not mod.is_bare(name), name
        assert not mod.is_bare(f"node:{name}"), name


# ── executions: the YAML half ─────────────────────────────────────────────
def test_a_secondary_checkout_job_with_no_install_yields_its_executed_script():
    [execution] = mod.executions(_workflow(), Path("pr-meta.yaml"))
    assert execution.job == "changelog_fragment"
    assert execution.directory == "_ci_scripts"
    assert execution.path == ".github/scripts/pr/changelog-gate.sh"
    assert execution.line == 11
    assert execution.checkout_line == 6


@pytest.mark.parametrize(
    "install_step",
    [
        "      - uses: actions/setup-node@abc\n",
        "      - uses: pnpm/action-setup@abc\n",
        "      - uses: oven-sh/setup-bun@abc\n",
        "      - run: npm ci\n",
        "      - run: npm install --no-audit\n",
        "      - run: pnpm install --frozen-lockfile\n",
        "      - run: corepack pnpm i\n",
        "      - run: yarn install --immutable\n",
        "      - run: yarn\n",
        "      - run: bun install\n",
    ],
)
def test_a_job_that_installs_node_dependencies_is_out_of_scope(install_step: str):
    assert mod.executions(_workflow(install_step), Path("w.yaml")) == []


@pytest.mark.parametrize(
    "not_an_install",
    [
        "      - run: npm run build\n",
        "      - run: npm test\n",
        "      - run: pnpm exec tsc\n",
        "      - run: yarn lint\n",
        "      - run: bun run x.ts\n",
    ],
)
def test_a_package_manager_command_that_installs_nothing_keeps_the_job_in_scope(
    not_an_install: str,
):
    assert len(mod.executions(_workflow(not_an_install), Path("w.yaml"))) == 1


@pytest.mark.parametrize(
    "run",
    [
        "node _ci_scripts/.github/scripts/checks/x.mjs",
        "exec node _ci_scripts/.github/scripts/checks/x.mjs --flag",
        "env FOO=1 node ./_ci_scripts/.github/scripts/checks/x.mjs",
        "bun run _ci_scripts/.github/scripts/checks/x.mjs",
        "tsx _ci_scripts/.github/scripts/checks/x.mjs",
        "./_ci_scripts/.github/scripts/checks/x.mjs",
        "_ci_scripts/.github/scripts/checks/x.mjs arg",
        'node "_ci_scripts/.github/scripts/checks/x.mjs"',
    ],
)
def test_each_way_of_running_a_script_out_of_the_tree_is_read(run: str):
    [execution] = mod.executions(_workflow(run=run), Path("w.yaml"))
    assert execution.path == ".github/scripts/checks/x.mjs"


@pytest.mark.parametrize(
    "run",
    [
        "node .github/scripts/checks/x.mjs",
        "node other_dir/.github/scripts/checks/x.mjs",
        'node "$DIR/.github/scripts/checks/x.mjs"',
        "node ${{ github.workspace }}/_ci_scripts/x.mjs",
        "ruff check _ci_scripts/.github/scripts/x.py",
        "cat _ci_scripts/README.md",
    ],
)
def test_a_command_that_runs_nothing_out_of_the_tree_yields_no_execution(run: str):
    assert mod.executions(_workflow(run=run), Path("w.yaml")) == []


def test_a_checkout_path_the_expression_language_decides_is_skipped():
    text = _workflow().replace("path: _ci_scripts", "path: ${{ env.DIR }}")
    assert mod.executions(text, Path("w.yaml")) == []


def test_a_checkout_of_another_repository_is_skipped():
    text = _workflow().replace(
        "          path: _ci_scripts\n",
        "          path: _ci_scripts\n          repository: other/tools\n",
    )
    assert mod.executions(text, Path("w.yaml")) == []


def test_a_checkout_into_the_workspace_itself_is_a_full_checkout():
    text = _workflow(run="node ./x.mjs").replace("path: _ci_scripts", "path: .")
    assert mod.executions(text, Path("w.yaml")) == []


def test_a_job_with_only_a_full_checkout_is_out_of_scope():
    text = _workflow().replace("          path: _ci_scripts\n", "")
    assert mod.executions(text, Path("w.yaml")) == []


def test_unparseable_yaml_is_none_not_an_empty_list():
    assert mod.executions("jobs: [\n", Path("w.yaml")) is None


def test_a_non_mapping_document_has_no_executions():
    assert mod.executions("- a\n- b\n", Path("w.yaml")) == []


# ── scripts_run_by_shell: the one shell hop ───────────────────────────────
_FILES = frozenset(
    {
        ".github/scripts/pr/changelog-gate.sh",
        ".github/scripts/checks/changelog-fragment.mjs",
        ".github/scripts/checks/lib/fragments.mjs",
    }
)


def test_a_variable_built_node_operand_resolves_by_its_literal_tail():
    assert mod.scripts_run_by_shell(
        GATE_SH, ".github/scripts/pr/changelog-gate.sh", _FILES
    ) == [".github/scripts/checks/changelog-fragment.mjs"]


def test_a_relative_node_operand_resolves_against_the_scripts_own_directory():
    text = "node ../checks/changelog-fragment.mjs\n"
    files = _FILES | {".github/scripts/pr/../checks/changelog-fragment.mjs"}
    # `../` is a literal segment, so the tail is tried as written first; the
    # suffix fallback then finds the one tracked file ending in the tail.
    assert mod.scripts_run_by_shell(
        text, ".github/scripts/pr/changelog-gate.sh", files
    ) == [".github/scripts/pr/../checks/changelog-fragment.mjs"]


def test_a_tail_naming_two_tracked_files_resolves_to_neither():
    files = _FILES | {"other/checks/changelog-fragment.mjs"}
    text = 'node "$X/checks/changelog-fragment.mjs"\n'
    assert mod.scripts_run_by_shell(text, "tools/run.sh", files) == []


def test_a_shell_script_that_runs_no_node_hands_over_nothing():
    text = "set -e\npython3 x.py\nexec bash other.sh\n"
    assert (
        mod.scripts_run_by_shell(text, ".github/scripts/pr/changelog-gate.sh", _FILES)
        == []
    )


# ── main(): end to end against a real repo ────────────────────────────────
def test_main_flags_the_bare_import_behind_one_shell_hop(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path, _workflow())
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/pr-meta.yaml,line=11::" in out
    assert (
        '`.github/scripts/checks/changelog-fragment.mjs:3` imports "smol-toml"' in out
    )
    # The builtin, the `path` builtin and the relative import are not reported.
    assert '"node:fs"' not in out
    assert '"path"' not in out
    assert "fragments.mjs" not in out


def test_main_passes_the_same_job_once_it_installs_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path, _workflow("      - run: pnpm install --frozen-lockfile\n"))
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_passes_the_same_import_run_from_the_main_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    workflow = _workflow(run="bash .github/scripts/pr/changelog-gate.sh").replace(
        "          path: _ci_scripts\n", ""
    )
    repo = _repo(tmp_path, workflow)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_passes_a_script_that_imports_only_builtins_and_relatives(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    fragment = FRAGMENT_MJS.replace(
        'import { parse as parseToml } from "smol-toml";\n', ""
    )
    repo = _repo(tmp_path, _workflow(), fragment=fragment)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_flags_a_javascript_file_run_directly_out_of_the_tree(tmp_path: Path):
    repo = _repo(
        tmp_path,
        _workflow(run="node _ci_scripts/.github/scripts/checks/changelog-fragment.mjs"),
    )
    assert mod.main(["--repo-root", str(repo)]) == 1


@pytest.mark.parametrize(
    "annotated",
    [
        # On the step that runs the script.
        "      # allow-secondary-checkout-import: the gate vendors smol-toml\n"
        "      - run: bash _ci_scripts/.github/scripts/pr/changelog-gate.sh\n",
        # On the checkout step whose tree it runs from.
        None,
    ],
)
def test_main_honours_the_annotation_on_either_step(
    tmp_path: Path, capsys: pytest.CaptureFixture, annotated: str | None
):
    if annotated is None:
        workflow = _workflow().replace(
            "      - uses: actions/checkout@abc\n",
            "      # allow-secondary-checkout-import: the gate vendors smol-toml\n"
            "      - uses: actions/checkout@abc\n",
        )
    else:
        workflow = _workflow().replace(
            "      - run: bash _ci_scripts/.github/scripts/pr/changelog-gate.sh\n",
            annotated,
        )
    repo = _repo(tmp_path, workflow)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_rejects_an_annotation_with_no_reason(tmp_path: Path):
    workflow = _workflow().replace(
        "      - run: bash",
        "      # allow-secondary-checkout-import:\n      - run: bash",
    )
    repo = _repo(tmp_path, workflow)
    assert mod.main(["--repo-root", str(repo)]) == 1


def test_main_reports_unparseable_yaml_as_a_violation(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path, "jobs: [\n")
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "could not parse as YAML" in capsys.readouterr().out


def test_main_passes_a_tree_with_no_secondary_checkout(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path, "on: push\njobs:\n  a:\n    steps:\n      - run: echo hi\n")
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""

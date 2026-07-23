"""Tests for ci_truth_serum/check_path_gate_deps.py — the lint requiring a decide job's
path filters to cover every file its gated jobs actually depend on (a filter
that omits a real dependency fails open exactly when that dependency changes).

Fixtures are real git repos in tmp dirs (``tracked_files`` shells out to
``git ls-files``) driving the real hook code end to end through ``main()`` /
``check_file``, plus targeted unit tests of the glob translation. Discovery is
redirected at the module's dir constants so the real repo never leaks in.
"""

import textwrap
from pathlib import Path

from tests._helpers import commit_all, init_test_repo, load_hook

cpgd = load_hook("check_path_gate_deps.py", "check_path_gate_deps")


# ── glob_to_regex ────────────────────────────────────────────────────────
def test_glob_matches_mirror_paths_filter_semantics():
    cases = [
        ("**/*.ts", "a.ts", True),  # `**/` matches zero segments
        ("**/*.ts", "deep/dir/a.ts", True),
        ("**/*.ts", "a.tsx", False),
        ("src/**", "src/a/b.txt", True),
        ("src/**", "srcx/a.txt", False),
        ("*.sh", "a.sh", True),
        ("*.sh", "dir/a.sh", False),  # `*` never crosses `/`
        ("a/**/b", "a/b", True),  # `**/` in the middle matches zero dirs
        ("a/**/b", "a/x/y/b", True),
        ("?.sh", "a.sh", True),
        ("?.sh", "ab.sh", False),
        (".github/**", ".github/scripts/x.sh", True),  # dotfiles match
        ("[ab].sh", "a.sh", True),
        ("[!ab].sh", "c.sh", True),
        ("[!ab].sh", "a.sh", False),
        ("package.json", "package.json", True),
        ("package.json", "sub/package.json", False),  # anchored to full path
    ]
    for pattern, path, expected in cases:
        assert bool(cpgd.glob_to_regex(pattern).search(path)) == expected, (
            pattern,
            path,
        )


# ── filter_patterns ──────────────────────────────────────────────────────
def test_filter_patterns_unions_groups_and_change_type_entries():
    spec = textwrap.dedent(
        """\
        run:
          - 'src/**'
          - added|modified: 'docs/**'
        other:
          - 'bin/*'
        """
    )
    assert cpgd.filter_patterns(spec) == ["src/**", "docs/**", "bin/*"]


def test_filter_patterns_rejects_non_mapping_spec():
    assert cpgd.filter_patterns("- just\n- a list\n") == []
    assert cpgd.filter_patterns(None) == []


# ── fixture machinery ────────────────────────────────────────────────────
def _workflow(filters: list[str], steps: str) -> str:
    filter_lines = "\n".join(f"          - '{f}'" for f in filters)
    return textwrap.dedent(
        """\
        name: x
        on:
          push:
        jobs:
          decide:
            uses: ./.github/workflows/decide-reusable.yaml
            with:
              filters: |
                run:
        {filters}
          work:
            needs: decide
            if: needs.decide.outputs.run == 'true'
            runs-on: ubuntu-latest
            steps:
        {steps}
        """
    ).format(filters=filter_lines, steps=textwrap.indent(steps.rstrip(), "      "))


def _repo(tmp_path: Path, monkeypatch, workflow: str, files: dict[str, str]) -> Path:
    """A committed git repo carrying WORKFLOW plus FILES, with the module's
    discovery constants pointed at it."""
    repo = tmp_path / "repo"
    init_test_repo(repo)
    wf_dir = repo / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    (wf_dir / "wf.yaml").write_text(workflow)
    for rel, content in files.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    commit_all(repo)
    monkeypatch.setattr(cpgd, "REPO_ROOT", repo)
    monkeypatch.setattr(cpgd, "WORKFLOWS_DIR", wf_dir)
    return repo


ACTION = {".github/actions/setup/action.yml": "name: setup\nruns:\n  using: node20\n"}
COMPOSITE_STEPS = "- uses: ./.github/actions/setup\n"


# ── (a) covered dependency passes ────────────────────────────────────────
def test_composite_covered_by_filters_passes(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _workflow([".github/actions/setup/**"], COMPOSITE_STEPS),
        ACTION,
    )
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


# ── (b) filters missing the composite fail, naming the dep ───────────────
def test_composite_not_covered_fails_naming_dep(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, _workflow(["src/**"], COMPOSITE_STEPS), ACTION)
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "job work (gated by decide)" in out
    assert "`.github/actions/setup`" in out
    assert ".github/actions/setup/action.yml" in out  # example unmatched file
    assert "'.github/actions/setup/**'" in out  # suggested filter fragment


# ── (c) partial coverage still fails, reporting the unmatched file ───────
def test_partial_coverage_of_dep_dir_fails(tmp_path, monkeypatch, capsys):
    files = dict(ACTION)
    files[".github/actions/setup/helper.js"] = "// helper\n"
    _repo(
        tmp_path,
        monkeypatch,
        _workflow([".github/actions/setup/action.yml"], COMPOSITE_STEPS),
        files,
    )
    assert cpgd.main() == 1
    assert ".github/actions/setup/helper.js" in capsys.readouterr().out


# ── (d) path-gate-ok suppression ─────────────────────────────────────────
def test_path_gate_ok_with_reason_suppresses(tmp_path, monkeypatch, capsys):
    steps = (
        "# path-gate-ok: .github/actions/setup composite is exercised elsewhere\n"
        + COMPOSITE_STEPS
    )
    _repo(tmp_path, monkeypatch, _workflow(["src/**"], steps), ACTION)
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


def test_path_gate_ok_without_reason_is_an_error(tmp_path, monkeypatch, capsys):
    steps = "# path-gate-ok: .github/actions/setup\n" + COMPOSITE_STEPS
    _repo(tmp_path, monkeypatch, _workflow(["src/**"], steps), ACTION)
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "has no reason" in out


# ── (e) gate-deps declared dependency ────────────────────────────────────
def test_gate_deps_comment_on_gated_job_unmatched_fails(tmp_path, monkeypatch, capsys):
    steps = "# gate-deps: bin/\n- run: uv run pytest\n"
    _repo(
        tmp_path,
        monkeypatch,
        _workflow(["src/**"], steps),
        {"bin/tool.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "`bin`" in out and "bin/tool.sh" in out


def test_gate_deps_comment_covered_passes(tmp_path, monkeypatch, capsys):
    steps = "# gate-deps: bin/\n- run: uv run pytest\n"
    _repo(
        tmp_path,
        monkeypatch,
        _workflow(["src/**", "bin/**"], steps),
        {"bin/tool.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


def test_gate_deps_comment_on_decide_job_attaches(tmp_path, monkeypatch, capsys):
    # The declaration may live on the decide job's block instead of the gated one.
    workflow = _workflow(["src/**"], "- run: uv run pytest\n").replace(
        "  decide:", "  decide:\n    # gate-deps: bin/", 1
    )
    _repo(tmp_path, monkeypatch, workflow, {"bin/tool.sh": "#!/bin/bash\n"})
    assert cpgd.main() == 1
    assert "`bin`" in capsys.readouterr().out


# ── (f) missing composite on disk: distinct hard error ───────────────────
def test_missing_local_action_is_distinct_error(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, _workflow(["src/**"], COMPOSITE_STEPS), {})
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "references missing local action `./.github/actions/setup`" in out


# ── scripts + one-hop transitivity ───────────────────────────────────────
def test_run_script_not_covered_fails(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _workflow(["src/**"], "- run: bash .github/scripts/foo.sh\n"),
        {".github/scripts/foo.sh": "#!/bin/bash\necho hi\n"},
    )
    assert cpgd.main() == 1
    assert "`.github/scripts/foo.sh`" in capsys.readouterr().out


def test_transitive_script_inclusion_is_a_dependency(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _workflow([".github/scripts/foo.sh"], "- run: bash .github/scripts/foo.sh\n"),
        {
            ".github/scripts/foo.sh": (
                "#!/bin/bash\nsource .github/scripts/lib.bash\n"
            ),
            ".github/scripts/lib.bash": "helper() { :; }\n",
        },
    )
    assert cpgd.main() == 1
    assert "`.github/scripts/lib.bash`" in capsys.readouterr().out


# ── (g) workflows without the decide pattern are skipped silently ────────
def test_non_decide_workflow_is_skipped(tmp_path, monkeypatch, capsys):
    workflow = textwrap.dedent(
        """\
        name: plain
        on:
          push:
        jobs:
          build:
            runs-on: ubuntu-latest
            steps:
              - run: bash .github/scripts/foo.sh
        """
    )
    _repo(tmp_path, monkeypatch, workflow, {})
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


# ── (h) unparseable YAML fails loud ──────────────────────────────────────
def test_unparseable_yaml_is_reported_as_violation(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, "jobs:\n  a: [unclosed\n", {})
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "could not parse as YAML" in out
    assert "::error file=.github/workflows/wf.yaml::" in out


# ── multi-gate union ─────────────────────────────────────────────────────
def test_dep_covered_by_any_referenced_gate_passes(tmp_path, monkeypatch, capsys):
    workflow = textwrap.dedent(
        """\
        name: x
        on:
          push:
        jobs:
          decide-a:
            uses: ./.github/workflows/decide-reusable.yaml
            with:
              filters: |
                run:
                  - 'src/**'
          decide-b:
            uses: ./.github/workflows/decide-reusable.yaml
            with:
              filters: |
                run:
                  - '.github/scripts/foo.sh'
          work:
            needs: [decide-a, decide-b]
            if: needs.decide-a.outputs.run == 'true' || needs.decide-b.outputs.run == 'true'
            runs-on: ubuntu-latest
            steps:
              - run: bash .github/scripts/foo.sh
        """
    )
    _repo(
        tmp_path,
        monkeypatch,
        workflow,
        {".github/scripts/foo.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


def test_job_needing_decide_without_gate_if_is_not_gated(tmp_path, monkeypatch, capsys):
    # `needs: decide` without an `if: needs.decide.outputs.run` reference always
    # runs (when decide succeeds), so it cannot fail open and is not checked.
    workflow = _workflow(["src/**"], "- run: bash .github/scripts/foo.sh\n").replace(
        "    if: needs.decide.outputs.run == 'true'\n", ""
    )
    _repo(
        tmp_path,
        monkeypatch,
        workflow,
        {".github/scripts/foo.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 0


# ── paths-regex decide variant ───────────────────────────────────────────
def _paths_regex_workflow(regex: str, steps: str) -> str:
    return textwrap.dedent(
        """\
        name: x
        on:
          push:
        jobs:
          decide:
            uses: ./.github/workflows/decide-reusable.yaml
            with:
              paths-regex: '{regex}'
          work:
            needs: decide
            if: needs.decide.outputs.run == 'true'
            runs-on: ubuntu-latest
            steps:
        {steps}
        """
    ).format(regex=regex, steps=textwrap.indent(steps.rstrip(), "      "))


def test_paths_regex_covering_composite_passes(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _paths_regex_workflow(r"^\.github/actions/setup/", COMPOSITE_STEPS),
        ACTION,
    )
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


def test_paths_regex_omitting_composite_fails_naming_dep(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _paths_regex_workflow(r"^src/", COMPOSITE_STEPS),
        ACTION,
    )
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "job work (gated by decide)" in out
    assert "`.github/actions/setup`" in out
    assert ".github/actions/setup/action.yml" in out
    assert out.count("::error") == 1


def test_empty_paths_regex_is_match_all_no_finding(tmp_path, monkeypatch, capsys):
    # An empty paths-regex is a keyword-only gate: path coverage is N/A, so no
    # dependency is ever reported uncovered.
    _repo(tmp_path, monkeypatch, _paths_regex_workflow("", COMPOSITE_STEPS), ACTION)
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


def test_paths_regex_covering_script_passes(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        _paths_regex_workflow(
            r"^\.github/scripts/", "- run: bash .github/scripts/foo.sh\n"
        ),
        {".github/scripts/foo.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 0
    assert capsys.readouterr().out == ""


# ── trailing-dot precision in script-ref capture ─────────────────────────
def test_trailing_dot_in_script_comment_is_stripped(tmp_path, monkeypatch, capsys):
    # A run body mentioning the script inside a sentence-ending comment must
    # resolve to the real path, not the dotted form.
    steps = (
        "- run: |\n"
        "    # Keep in sync with .github/scripts/foo.sh.\n"
        "    bash .github/scripts/foo.sh\n"
    )
    _repo(
        tmp_path,
        monkeypatch,
        _workflow(["src/**"], steps),
        {".github/scripts/foo.sh": "#!/bin/bash\n"},
    )
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "`.github/scripts/foo.sh`" in out
    assert "foo.sh.`" not in out


def test_trailing_dot_in_transitive_script_is_stripped(tmp_path, monkeypatch, capsys):
    # The one-hop scan of a referenced script also strips a dotted comment ref.
    _repo(
        tmp_path,
        monkeypatch,
        _workflow([".github/scripts/foo.sh"], "- run: bash .github/scripts/foo.sh\n"),
        {
            ".github/scripts/foo.sh": (
                "#!/bin/bash\n# see .github/scripts/lib.bash.\n"
            ),
            ".github/scripts/lib.bash": "helper() { :; }\n",
        },
    )
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "`.github/scripts/lib.bash`" in out
    assert "lib.bash.`" not in out


# ── error annotation shape ───────────────────────────────────────────────
def test_violation_is_line_anchored_to_the_gated_job(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, _workflow(["src/**"], COMPOSITE_STEPS), ACTION)
    assert cpgd.main() == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/wf.yaml,line=" in out
    assert "1 path-gate violation(s) found" in out

"""Tests for ci_truth_serum/check_sparse_checkout_closure.py — the workflow
lint that fails a `sparse-checkout:` job whose list misses a file its own
steps reach (directly, through a listed Python entry point's own local
imports, or through a `sparse-checkout-needs:` comment naming a file the
closure opens at run time).

Drives the module's functions directly (patterns, coverage, dependency
derivation) plus `main()` end to end against a real git repo under `tmp_path`
— `tracked_files()` shells out to `git ls-files`, so every fixture is a real
commit.
"""

from pathlib import Path

import pytest

from tests._helpers import commit_all, init_test_repo, load_hook

mod = load_hook("check_sparse_checkout_closure.py", "check_sparse_checkout_closure")


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    init_test_repo(tmp_path)
    return tmp_path


# ── _cone_covers / _noncone_covers / covers ───────────────────────────────
_NO_FILES: frozenset = frozenset()


def test_cone_covers_the_listed_directory_and_its_files():
    assert mod._cone_covers((".github/scripts",), ".github/scripts/x.py", _NO_FILES)
    assert not mod._cone_covers(
        (".github/scripts",), ".github/actions/x/run.sh", _NO_FILES
    )


def test_cone_covers_root_level_files_with_no_directory():
    assert mod._cone_covers((".github/scripts",), "README.md", _NO_FILES)


def test_cone_covers_a_files_ancestor_directory_rung():
    # A listed `.github/scripts` implies the files directly in `.github/`
    # itself (the ancestor rung git writes around `set A/B`).
    assert mod._cone_covers((".github/scripts",), ".github/tool-versions.sh", _NO_FILES)


def test_cone_covers_denies_the_ancestor_rung_for_a_listed_file():
    # Listing the FILE `.github/scripts/render.py` makes that file visible,
    # not every sibling in `.github/scripts/` — the rung is directory-only.
    files = frozenset({".github/scripts/render.py"})
    assert not mod._cone_covers(
        (".github/scripts/render.py",), ".github/scripts/_lockfiles.py", files
    )


def test_noncone_slashless_pattern_is_unanchored():
    # gitignore semantics: a bare name matches at any depth.
    assert mod._noncone_covers(("config",), "a/config/b.py")
    assert mod._noncone_covers(("config",), "config")
    assert not mod._noncone_covers(("config",), "a/configuration/b.py")


def test_noncone_slashed_pattern_is_anchored():
    assert mod._noncone_covers((".github/scripts",), ".github/scripts/x.py")
    assert not mod._noncone_covers((".github/scripts",), "a/.github/scripts/x.py")


def test_covers_dispatches_on_cone_flag():
    cone = mod.Checkout(Path("w.yaml"), "build", 1, (), (".github/scripts",), True)
    noncone = mod.Checkout(Path("w.yaml"), "build", 1, (), ("scripts",), False)
    assert mod.covers(cone, ".github/tool-versions.sh", _NO_FILES)
    assert mod.covers(noncone, "a/scripts/b.py", _NO_FILES)


# ── _path_token_re / _dependencies ────────────────────────────────────────
def test_path_token_re_matches_only_named_dirs():
    pattern = mod._path_token_re((".github/scripts",))
    assert pattern.findall("bash .github/scripts/run.sh") == [".github/scripts/run.sh"]
    assert pattern.findall("bash .github/actions/x/run.sh") == []


def test_path_token_re_with_no_dirs_matches_nothing():
    pattern = mod._path_token_re(())
    assert pattern.findall(".github/scripts/run.sh") == []


def test_dependencies_reads_run_text_and_local_composite_uses():
    window = (
        {"run": "bash .github/scripts/run.sh"},
        {"uses": "./.github/actions/x"},
    )
    deps = mod._dependencies(window, mod._path_token_re((".github/scripts",)))
    assert deps == {".github/scripts/run.sh", ".github/actions/x"}


# ── suppressions ───────────────────────────────────────────────────────────
def test_suppressions_accepts_a_marker_with_a_reason():
    text = "# sparse-checkout-ok: .github/actions/x the caller passes it in\n"
    with_reason, reasonless = mod.suppressions(text)
    assert with_reason == {".github/actions/x": "the caller passes it in"}
    assert reasonless == []


def test_suppressions_refuses_a_marker_with_no_reason():
    with_reason, reasonless = mod.suppressions("# sparse-checkout-ok: some/dep\n")
    assert with_reason == {}
    assert reasonless == ["some/dep"]


# ── checkouts() / _window ─────────────────────────────────────────────────
def _workflow_text(sparse: str, run: str, extra_steps: str = "") -> str:
    return (
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: actions/checkout@abc\n"
        "        with:\n"
        f"          sparse-checkout: |\n            {sparse}\n"
        f"      - run: {run}\n" + extra_steps
    )


def test_checkouts_skips_a_runtime_decided_pattern():
    text = _workflow_text("${{ inputs.x }}", "echo hi")
    assert mod.checkouts(text, Path("w.yaml")) == []


def test_checkouts_skips_a_wildcard_pattern():
    text = _workflow_text("*.py", "echo hi")
    assert mod.checkouts(text, Path("w.yaml")) == []


def test_checkouts_reads_cone_mode_default_true():
    text = _workflow_text(".github/scripts", "echo hi")
    [checkout] = mod.checkouts(text, Path("w.yaml"))
    assert checkout.cone is True
    assert checkout.patterns == (".github/scripts",)


def test_window_stops_at_a_later_unconditional_full_checkout():
    text = _workflow_text(
        ".github/scripts",
        "echo one",
        "      - uses: actions/checkout@abc\n"
        "        with:\n"
        "          ref: main\n"
        "      - run: echo two\n",
    )
    [checkout] = mod.checkouts(text, Path("w.yaml"))
    assert [s.get("run") for s in checkout.window] == ["echo one"]


def test_window_keeps_going_past_a_conditional_checkout():
    # The conditional checkout step itself never enters the window (it is not
    # a `run:` step); the window just keeps accumulating past it.
    text = _workflow_text(
        ".github/scripts",
        "echo one",
        "      - uses: actions/checkout@abc\n"
        "        if: github.event_name == 'push'\n"
        "        with:\n"
        "          ref: main\n"
        "      - run: echo two\n",
    )
    [checkout] = mod.checkouts(text, Path("w.yaml"))
    assert [s.get("run") for s in checkout.window] == ["echo one", "echo two"]


def test_window_excludes_a_step_sharing_the_conditional_checkouts_if():
    text = _workflow_text(
        ".github/scripts",
        "echo one",
        "      - uses: actions/checkout@abc\n"
        "        if: github.event_name == 'push'\n"
        "        with:\n"
        "          ref: main\n"
        "      - run: echo two\n"
        "        if: github.event_name == 'push'\n",
    )
    [checkout] = mod.checkouts(text, Path("w.yaml"))
    assert [s.get("run") for s in checkout.window] == ["echo one"]


def test_window_never_ends_on_a_path_scoped_checkout():
    # A `path:`-scoped checkout clones beside the tree rather than replacing
    # it, so it stays IN the window (as an ordinary non-`run:` step) instead
    # of ending it.
    text = _workflow_text(
        ".github/scripts",
        "echo one",
        "      - uses: actions/checkout@abc\n"
        "        with:\n"
        "          path: other\n"
        "      - run: echo two\n",
    )
    [checkout] = mod.checkouts(text, Path("w.yaml"))
    assert [s.get("run") for s in checkout.window] == ["echo one", None, "echo two"]


# ── main(): end to end against a real repo ────────────────────────────────
def test_main_flags_an_uncovered_dependency(tmp_path: Path):
    repo = _repo(tmp_path)
    # The checkout lists `.github/actions` only; the run step reaches a
    # script under the DEFAULT recognized dir, `.github/scripts`, instead.
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/actions", "bash .github/scripts/run.sh"),
    )
    _write(repo, ".github/scripts/run.sh", "echo hi\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1


def test_main_passes_a_covered_dependency(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts", "bash .github/scripts/run.sh"),
    )
    _write(repo, ".github/scripts/run.sh", "echo hi\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_follows_a_listed_entrypoints_local_imports(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts/render.py", 'python3 "$DIR/render.py"'),
    )
    _write(
        repo,
        ".github/scripts/render.py",
        "from _lockfiles import rule_for\n",
    )
    _write(repo, ".github/scripts/_lockfiles.py", "rule_for = 1\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1


_SOURCING_SCRIPT = (
    "#!/usr/bin/env bash\n"
    "# shellcheck source=.github/scripts/lib-retry.sh\n"
    'source "$(dirname "${BASH_SOURCE[0]}")/lib-retry.sh"\n'
)


def test_main_follows_a_listed_shell_entrypoints_source(tmp_path: Path):
    """The measured case: a list naming only the script, whose first `source`
    then dies on the runner with "No such file or directory"."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts/approve.sh", "bash .github/scripts/approve.sh"),
    )
    _write(repo, ".github/scripts/approve.sh", _SOURCING_SCRIPT)
    _write(repo, ".github/scripts/lib-retry.sh", "retry() { :; }\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1


def test_main_passes_when_the_sourced_library_is_also_listed(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts/approve.sh\n            .github/scripts/lib-retry.sh",
            "bash .github/scripts/approve.sh",
        ),
    )
    _write(repo, ".github/scripts/approve.sh", _SOURCING_SCRIPT)
    _write(repo, ".github/scripts/lib-retry.sh", "retry() { :; }\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_follows_a_source_chain_to_its_end(tmp_path: Path):
    """A library that sources a second library is the same hole one rung down."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts/approve.sh\n            .github/scripts/lib-retry.sh",
            "bash .github/scripts/approve.sh",
        ),
    )
    _write(repo, ".github/scripts/approve.sh", _SOURCING_SCRIPT)
    _write(
        repo,
        ".github/scripts/lib-retry.sh",
        '# shellcheck source=.github/scripts/lib-log.sh\nsource "$D/lib-log.sh"\n',
    )
    _write(repo, ".github/scripts/lib-log.sh", "log() { :; }\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1


def test_a_source_target_no_tracked_file_answers_adds_nothing(tmp_path: Path):
    """The last segment of an expansion-decided path is a GUESS. A guess that
    names nothing in the tree must add no dependency, or the check would report
    a hole no sparse-checkout list can close."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts/approve.sh", "bash .github/scripts/approve.sh"),
    )
    _write(
        repo,
        ".github/scripts/approve.sh",
        '#!/usr/bin/env bash\nsource "${HOME}/.nvm/nvm.sh"\n',
    )
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_source_targets_reads_the_path_not_the_command_word(tmp_path: Path):
    targets = mod._source_targets('source "$D/lib-retry.sh"\n')
    assert "source" not in targets
    assert targets == {"$D/lib-retry.sh"}


def test_main_passes_when_the_import_is_also_listed(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts/render.py\n            .github/scripts/_lockfiles.py",
            'python3 "$DIR/render.py"',
        ),
    )
    _write(repo, ".github/scripts/render.py", "from _lockfiles import rule_for\n")
    _write(repo, ".github/scripts/_lockfiles.py", "rule_for = 1\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_does_not_follow_a_py_file_no_interpreter_runs(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts", "ruff check .github/scripts/render.py"),
    )
    _write(repo, ".github/scripts/render.py", "from _lockfiles import rule_for\n")
    _write(repo, ".github/scripts/_lockfiles.py", "rule_for = 1\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_respects_a_dep_scoped_opt_out(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        "# sparse-checkout-ok: .github/actions/x deliberately optional\n"
        + _workflow_text(".github/scripts", "bash .github/actions/x/run.sh"),
    )
    _write(repo, ".github/actions/x/run.sh", "echo hi\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_flags_a_reasonless_opt_out(tmp_path: Path, capsys: pytest.CaptureFixture):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        "# sparse-checkout-ok: .github/actions/x\n"
        + _workflow_text(".github/scripts", "bash .github/actions/x/run.sh"),
    )
    _write(repo, ".github/actions/x/run.sh", "echo hi\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "has no reason" in capsys.readouterr().out


def test_main_extends_dep_dirs_with_a_repeated_flag(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts", "bash config/run.sh"),
    )
    _write(repo, "config/run.sh", "echo hi\n")
    commit_all(repo)
    # The default dep-dir set (`.github/scripts` alone) cannot see `config/`.
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert mod.main(["--repo-root", str(repo), "--dep-dir", "config"]) == 1


def test_main_flags_an_unreadable_workflow(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The module's core no-false-green contract: a workflow that does not
    parse as YAML is a VIOLATION, never a silent skip. `checkouts()` returns
    None on `yaml.YAMLError`, which `main()` must turn into an error and a
    nonzero exit — not read as "no checkouts to judge"."""
    repo = _repo(tmp_path)
    _write(repo, ".github/workflows/w.yaml", "jobs:\n  build:\n   x: [\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "could not parse as YAML" in capsys.readouterr().out


def test_main_is_silent_over_a_tree_with_no_sparse_checkout(tmp_path: Path):
    repo = _repo(tmp_path)
    _write(repo, ".github/workflows/w.yaml", "jobs:\n  build:\n    steps: []\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_notes_a_tree_with_no_workflow_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    repo = _repo(tmp_path)
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert "scanned nothing" in capsys.readouterr().err


# ── declared_needs(): a file the closure reaches names what it opens ──────
_PIN_READER = (
    "#!/usr/bin/env python3\n"
    '"""Read the pinned versions the report prints."""\n'
    "\n"
    "# sparse-checkout-needs: config/pins.toml\n"
    "def pins(root):\n"
    "    return (root / 'config/pins.toml').read_text(encoding='utf-8')\n"
)


def _needs_repo(tmp_path: Path, module_body: str, sparse: str) -> Path:
    """A repo whose job runs `.github/scripts/report.py`, which imports
    `.github/scripts/_root.py` — the module that opens a file at run time. The
    measured shape: the import walk reaches the module, and no import and no
    `source` reaches the file that module opens."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(sparse, "python3 .github/scripts/report.py"),
    )
    _write(repo, ".github/scripts/report.py", "from _root import pins\n")
    _write(repo, ".github/scripts/_root.py", module_body)
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    return repo


def test_declared_needs_reads_the_path_and_where_it_is_declared(tmp_path: Path):
    repo = _needs_repo(tmp_path, _PIN_READER, ".github/scripts")
    [need] = mod.declared_needs([".github/scripts/_root.py"], repo)
    assert (need.path, need.declarer, need.line) == (
        "config/pins.toml",
        ".github/scripts/_root.py",
        4,
    )


def test_declared_needs_reads_every_path_on_the_line(tmp_path: Path):
    repo = _needs_repo(
        tmp_path,
        "# sparse-checkout-needs: config/pins.toml config/rules.toml\n",
        ".github/scripts",
    )
    _write(repo, "config/rules.toml", "x = 1\n")
    commit_all(repo)
    needs = mod.declared_needs([".github/scripts/_root.py"], repo)
    assert [need.path for need in needs] == ["config/pins.toml", "config/rules.toml"]


def test_declared_needs_ignores_a_string_literal(tmp_path: Path):
    """Non-vacuity, and the reason `_cts_comments` picks the grammar: the same
    words inside a Python string are a value the program builds — a help text,
    an error message — not a claim about the tree. The comment form of the very
    same line is read (the first assertion), so the reader is not simply blind
    here."""
    declaration = "sparse-checkout-needs: config/pins.toml"
    repo = _needs_repo(tmp_path, f"# {declaration}\n", ".github/scripts")
    assert [n.path for n in mod.declared_needs([".github/scripts/_root.py"], repo)] == [
        "config/pins.toml"
    ]
    _write(repo, ".github/scripts/_root.py", f'HINT = "{declaration}"\n')
    assert mod.declared_needs([".github/scripts/_root.py"], repo) == []


def test_main_flags_a_declared_path_the_list_misses(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The measured failure: the list covered every file the job IMPORTS, and
    the job still died, because `_root.py` opens a file it never imports."""
    repo = _needs_repo(tmp_path, _PIN_READER, ".github/scripts")
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "misses `config/pins.toml`" in capsys.readouterr().out


def test_main_passes_when_the_declared_path_is_also_listed(tmp_path: Path):
    repo = _needs_repo(
        tmp_path, _PIN_READER, ".github/scripts\n            config/pins.toml"
    )
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_respects_an_opt_out_for_a_declared_path(tmp_path: Path):
    repo = _needs_repo(tmp_path, _PIN_READER, ".github/scripts")
    workflow = repo / ".github/workflows/w.yaml"
    workflow.write_text(
        "# sparse-checkout-ok: config/pins.toml the caller writes one first\n"
        + workflow.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_ignores_a_declaration_outside_the_jobs_closure(tmp_path: Path):
    """Scope: the scan reads the files this job reaches. A module no step of
    this job runs states nothing about this job's tree."""
    repo = _needs_repo(tmp_path, "x = 1\n", ".github/scripts")
    _write(repo, "other.py", "# sparse-checkout-needs: config/pins.toml\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0


def test_main_reads_a_declaration_in_a_sourced_shell_library(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The shell arm of the same hole, through the bash grammar: the library a
    script sources declares the data file it reads."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts/approve.sh\n            .github/scripts/lib-retry.sh",
            "bash .github/scripts/approve.sh",
        ),
    )
    _write(repo, ".github/scripts/approve.sh", _SOURCING_SCRIPT)
    _write(
        repo,
        ".github/scripts/lib-retry.sh",
        "# sparse-checkout-needs: config/retry.json\nretry() { :; }\n",
    )
    _write(repo, "config/retry.json", "{}\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "misses `config/retry.json`" in capsys.readouterr().out


def test_main_flags_a_declaration_that_names_no_tracked_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """A declaration that widens nothing is a defect, never a silent no-op:
    sparse-checkout serves tracked files alone, so a typo here would leave the
    job dying on the runner with the check still green."""
    repo = _needs_repo(
        tmp_path, "# sparse-checkout-needs: config/pinz.toml\n", ".github/scripts"
    )
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "names no tracked file" in out
    assert "file=.github/scripts/_root.py,line=1" in out


def test_main_reports_one_broken_declaration_once_per_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """Two jobs reach the same module, and its broken declaration is one
    defect, so the run says it once."""
    repo = _needs_repo(
        tmp_path, "# sparse-checkout-needs: config/pinz.toml\n", ".github/scripts"
    )
    workflow = repo / ".github/workflows/w.yaml"
    text = workflow.read_text(encoding="utf-8")
    workflow.write_text(
        text + text.split("jobs:\n", 1)[1].replace("  build:", "  again:", 1),
        encoding="utf-8",
    )
    commit_all(repo)
    assert len(mod.checkouts(workflow.read_text(encoding="utf-8"), workflow)) == 2
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert capsys.readouterr().out.count("names no tracked file") == 1


def test_main_flags_a_declaration_that_names_no_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """A marker with nothing after it states nothing, and it reads like a
    working declaration. Dropping it silently is the false green this check
    exists to catch."""
    repo = _needs_repo(tmp_path, "# sparse-checkout-needs:\n", ".github/scripts")
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "names no path" in capsys.readouterr().out


def test_main_walks_declarations_to_a_fixed_point(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """A declared file declares in turn: `_root.py` names the plugin it loads,
    and the plugin names the config it reads. One pass would serve the plugin
    and leave the runner without the config."""
    repo = _needs_repo(
        tmp_path,
        "# sparse-checkout-needs: .github/scripts/plugin.sh\n",
        ".github/scripts",
    )
    _write(
        repo,
        ".github/scripts/plugin.sh",
        "# sparse-checkout-needs: config/pins.toml\nload() { :; }\n",
    )
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    # The plugin itself is covered by the listed `.github/scripts`; the file
    # the SECOND round found is the hole.
    assert "misses `config/pins.toml`" in out
    assert "plugin.sh" not in out


def test_main_reads_a_one_line_javascript_block_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """`_cts_comments` hands back the comment node, delimiters and all, so the
    `*/` of a one-line block must not read as a second path. The route to a
    `.js` file is a composite action: the step runs the whole directory, and
    `_scan_targets` expands it to every tracked file inside."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts\n            .github/actions",
            "echo hi",
            "      - uses: ./.github/actions/x\n",
        ),
    )
    _write(repo, ".github/actions/x/action.yml", "runs:\n  using: node20\n")
    _write(
        repo,
        ".github/actions/x/index.js",
        "/* sparse-checkout-needs: config/pins.toml */\nconsole.log(1);\n",
    )
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "misses `config/pins.toml`" in out
    assert "names no tracked file" not in out


def test_main_reads_a_declaration_inside_a_composite_action(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """`uses: ./.github/actions/x` names the action's DIRECTORY. Handing that
    directory to a file reader finds nothing, so the declaration its
    `action.yml` carries would bind no job."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(
            ".github/scripts\n            .github/actions",
            "echo hi",
            "      - uses: ./.github/actions/x\n",
        ),
    )
    _write(
        repo,
        ".github/actions/x/action.yml",
        "# sparse-checkout-needs: config/pins.toml\nruns:\n  using: composite\n",
    )
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "misses `config/pins.toml`" in capsys.readouterr().out


def test_main_judges_a_declared_directory_by_the_files_inside_it(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """A cone list covers a root-level FILE for free, so judging a declared
    root-level DIRECTORY as one path passes a job whose tree holds none of its
    contents."""
    repo = _needs_repo(tmp_path, "# sparse-checkout-needs: config\n", ".github/scripts")
    _write(repo, "config/rules.toml", "x = 1\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "misses `config/pins.toml`" in out
    assert "misses `config/rules.toml`" in out


def test_main_reads_a_declaration_in_a_variable_invoked_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The entry point term of the scan set, on its own. `python3 "$DIR/x.py"`
    puts no readable path on the command, so the script reaches the scan only
    as the sparse-checkout list's own entry — nothing else names it."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts/render.py", 'python3 "$DIR/render.py"'),
    )
    _write(
        repo,
        ".github/scripts/render.py",
        "# sparse-checkout-needs: config/pins.toml\nprint(1)\n",
    )
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "misses `config/pins.toml`" in capsys.readouterr().out


def test_main_reads_a_declaration_in_a_shell_entry_point(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The shell half of the same term, and the load-bearing one: `_sourced`
    skips the entry points themselves, so a declaration in the script the job
    runs reaches the scan only through that term."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts/approve.sh", 'bash "$DIR/approve.sh"'),
    )
    _write(
        repo,
        ".github/scripts/approve.sh",
        "# sparse-checkout-needs: config/pins.toml\necho hi\n",
    )
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 1
    assert "misses `config/pins.toml`" in capsys.readouterr().out


def test_main_ignores_a_declaration_in_a_file_the_job_only_names(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """`ruff check x.py` reads that file and runs nothing in it, so what it
    opens at RUN TIME is no dependency of this job. The module docstring holds
    the same rule for the import walk, and a declaration must not be the one
    reference that escapes it."""
    repo = _repo(tmp_path)
    _write(
        repo,
        ".github/workflows/w.yaml",
        _workflow_text(".github/scripts", "ruff check .github/scripts/_root.py"),
    )
    _write(
        repo,
        ".github/scripts/_root.py",
        "# sparse-checkout-needs: config/pins.toml\nx = 1\n",
    )
    _write(repo, "config/pins.toml", "ruff = '0.1.0'\n")
    commit_all(repo)
    assert mod.main(["--repo-root", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_main_reads_prose_after_the_paths_as_a_path(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """The documented grammar: everything after the colon is a path. A reason
    borrowed from the sibling opt-out's habit is not silently ignored — it
    fails, and the message says which word named no tracked file."""
    repo = _needs_repo(
        tmp_path,
        "# sparse-checkout-needs: config/pins.toml (read by pins)\n",
        ".github/scripts\n            config/pins.toml",
    )
    assert mod.main(["--repo-root", str(repo)]) == 1
    out = capsys.readouterr().out
    assert "`sparse-checkout-needs: (read` names no tracked file" in out

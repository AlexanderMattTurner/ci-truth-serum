"""Tests for hooks/check_externalized_markers.py — the pre-commit lint that flags
a workflow job where a policy marker (default: git history-rewrite commands) is
reachable only through `.github/scripts/*.sh` or `./.github/actions/*`
indirection, never in an inline `run:`. That delta is precisely the blind spot of
a guard that scans only inline `run:` text: it would pass vacuously once the
marked command is externalized."""

from pathlib import Path

from tests._helpers import REPO_ROOT, load_hook

em = load_hook("check_externalized_markers.py", "check_externalized_markers")

MARKERS = [(m, em._marker_regex(m)) for m in em.DEFAULT_MARKERS]


def _reader(files: dict[str, str]):
    """A reader over an in-memory {repo_rel_path: text} map; "" for anything else."""
    return lambda rel: files.get(rel, "")


# ── _marker_regex / markers_present ───────────────────────────────────────────


def test_marker_regex_is_whitespace_insensitive():
    pat = em._marker_regex("git commit --amend")
    assert pat.search("run: git   commit    --amend HEAD")
    assert not pat.search("git commit -m amend")


def test_markers_present_returns_matched_subset():
    text = "git rebase --onto main\nsome other line"
    assert em.markers_present(text, MARKERS) == {"git rebase"}


def test_markers_present_empty_when_none_match():
    assert em.markers_present("echo hello", MARKERS) == set()


def test_marker_boundary_distinguishes_force_variants():
    """`git push --force` must not match inside `git push --force-with-lease`, so
    the two markers stay distinct rather than both firing on one command."""
    assert em.markers_present("git push --force-with-lease", MARKERS) == {
        "git push --force-with-lease"
    }
    assert em.markers_present("git push --force origin", MARKERS) == {
        "git push --force"
    }


# ── referenced_scripts ────────────────────────────────────────────────────────


def test_referenced_scripts_finds_all_invocation_forms():
    text = (
        "bash .github/scripts/a.sh\n"
        "sh .github/scripts/b.bash arg\n"
        "./.github/scripts/c.sh\n"
        ". .github/scripts/d.sh\n"
    )
    assert em.referenced_scripts(text) == [
        ".github/scripts/a.sh",
        ".github/scripts/b.bash",
        ".github/scripts/c.sh",
        ".github/scripts/d.sh",
    ]


def test_referenced_scripts_dedupes_in_first_seen_order():
    text = "bash .github/scripts/a.sh\nbash .github/scripts/a.sh\n"
    assert em.referenced_scripts(text) == [".github/scripts/a.sh"]


def test_referenced_scripts_ignores_non_scripts_dir():
    assert em.referenced_scripts("bash scripts/other.sh") == []


# ── _composite_dir ────────────────────────────────────────────────────────────


def test_composite_dir_strips_leading_dot_slash():
    assert em._composite_dir("./.github/actions/foo") == ".github/actions/foo"


def test_composite_dir_none_for_remote_action():
    assert em._composite_dir("actions/checkout@v4") is None
    assert em._composite_dir(None) is None


# ── analyze: the core blind-spot delta ────────────────────────────────────────


def test_marker_only_in_referenced_script_is_flagged():
    """The motivating bug: `git rebase` moved into a script the job invokes. An
    inline-only guard sees no marker; the with-scripts scan does — that delta is
    the blind spot."""
    doc = {
        "jobs": {
            "autofix": {"steps": [{"run": "bash .github/scripts/precommit-autofix.sh"}]}
        }
    }
    reader = _reader({".github/scripts/precommit-autofix.sh": "git rebase -i HEAD~2"})
    out = em.analyze(doc, reader, MARKERS)
    assert len(out) == 1
    _line, msg = out[0]
    assert "job autofix" in msg
    assert "git rebase" in msg
    assert ".github/scripts/precommit-autofix.sh" in msg


def test_marker_present_inline_is_not_flagged():
    """If the same marker is already visible inline in the job, an inline-only
    guard is NOT blind — no delta, no finding (even though a script also has it)."""
    doc = {
        "jobs": {
            "autofix": {
                "steps": [
                    {"run": "git rebase --continue"},
                    {"run": "bash .github/scripts/x.sh"},
                ]
            }
        }
    }
    reader = _reader({".github/scripts/x.sh": "git rebase -i"})
    assert em.analyze(doc, reader, MARKERS) == []


def test_partial_delta_flags_only_the_externalized_marker():
    """One marker inline, a DIFFERENT marker external: only the external one is a
    blind spot (the inline one a guard already sees). Exercises the set-difference
    `external - inline` at the heart of the check."""
    doc = {
        "jobs": {
            "j": {
                "steps": [
                    {"run": "git rebase --continue"},
                    {"run": "bash .github/scripts/x.sh"},
                ]
            }
        }
    }
    reader = _reader({".github/scripts/x.sh": "git filter-branch --all"})
    out = em.analyze(doc, reader, MARKERS)
    assert len(out) == 1
    assert "git filter-branch" in out[0][1]
    assert "git rebase" not in out[0][1]


def test_opt_out_on_run_step_suppresses_finding():
    doc = {
        "jobs": {
            "j": {
                "steps": [
                    {
                        "run": "bash .github/scripts/x.sh  # allow-externalized-marker: reviewed"
                    }
                ]
            }
        }
    }
    reader = _reader({".github/scripts/x.sh": "git rebase -i"})
    assert em.analyze(doc, reader, MARKERS) == []


def test_opt_out_inside_referenced_script_suppresses_finding():
    doc = {"jobs": {"j": {"steps": [{"run": "bash .github/scripts/x.sh"}]}}}
    reader = _reader(
        {
            ".github/scripts/x.sh": "# allow-externalized-marker: intentional\ngit rebase -i"
        }
    )
    assert em.analyze(doc, reader, MARKERS) == []


def test_nested_composite_is_not_followed_one_hop_only():
    """Documented limit: a marker inside a composite that itself `uses:` a further
    nested composite is NOT resolved. Asserts the current (miss) behavior so a
    future change to two-hop resolution is deliberate, not accidental."""
    doc = {"jobs": {"j": {"steps": [{"uses": "./.github/actions/outer"}]}}}
    reader = _reader(
        {
            ".github/actions/outer/action.yml": (
                "runs:\n  steps:\n    - uses: ./.github/actions/inner\n"
            ),
            ".github/actions/inner/action.yml": (
                "runs:\n  steps:\n    - run: git rebase -i\n"
            ),
        }
    )
    assert em.analyze(doc, reader, MARKERS) == []


def test_no_marker_anywhere_is_clean():
    doc = {"jobs": {"build": {"steps": [{"run": "bash .github/scripts/x.sh"}]}}}
    reader = _reader({".github/scripts/x.sh": "echo hello && make build"})
    assert em.analyze(doc, reader, MARKERS) == []


def test_marker_reached_through_composite_action_is_flagged():
    doc = {"jobs": {"j": {"steps": [{"uses": "./.github/actions/fixup"}]}}}
    reader = _reader(
        {
            ".github/actions/fixup/action.yml": "runs:\n  steps:\n    - run: git commit --amend\n"
        }
    )
    out = em.analyze(doc, reader, MARKERS)
    assert len(out) == 1
    assert "git commit --amend" in out[0][1]
    assert ".github/actions/fixup/action.yml" in out[0][1]


def test_marker_reached_through_composite_then_script_is_flagged():
    """One hop past the composite: the action invokes a script that carries the marker."""
    doc = {"jobs": {"j": {"steps": [{"uses": "./.github/actions/fixup"}]}}}
    reader = _reader(
        {
            ".github/actions/fixup/action.yaml": (
                "runs:\n  steps:\n    - run: bash .github/scripts/deep.sh\n"
            ),
            ".github/scripts/deep.sh": "git push --force origin HEAD",
        }
    )
    out = em.analyze(doc, reader, MARKERS)
    assert len(out) == 1
    assert "git push --force" in out[0][1]


def test_missing_referenced_script_is_clean_not_a_crash():
    """A referenced-but-unreadable script contributes empty text — nothing to flag."""
    doc = {"jobs": {"j": {"steps": [{"run": "bash .github/scripts/gone.sh"}]}}}
    assert em.analyze(doc, _reader({}), MARKERS) == []


def test_composite_action_document_itself_is_scanned():
    """A composite `action.yml`'s own steps are scanned: an internal step
    externalizes the marker into a script."""
    doc = {"runs": {"steps": [{"run": "bash .github/scripts/x.sh"}]}}
    reader = _reader({".github/scripts/x.sh": "git filter-branch --force"})
    out = em.analyze(doc, reader, MARKERS)
    assert len(out) == 1
    assert "composite action" in out[0][1]


def test_non_dict_and_malformed_jobs_are_clean():
    assert em.analyze(["not", "a", "doc"], _reader({}), MARKERS) == []
    assert em.analyze({"jobs": {"bad": "x", "empty": {}}}, _reader({}), MARKERS) == []


# ── check_file ────────────────────────────────────────────────────────────────


def test_check_file_resolves_indirection_from_disk(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(em, "REPO_ROOT", tmp_path)
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "autofix.sh").write_text("git commit --amend --no-edit\n")
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        "jobs:\n  autofix:\n    steps:\n      - run: bash .github/scripts/autofix.sh\n"
    )
    out = em.check_file(wf)
    assert len(out) == 1
    line, msg = out[0]
    assert line == 4  # the `- run:` step line
    assert "git commit --amend" in msg


def test_check_file_reports_unparseable_yaml(tmp_path: Path):
    # Unparseable input can't be verified, so it must not silently read as
    # "no violations" — that would be the exact false-green this tool exists to catch.
    bad = tmp_path / "bad.yaml"
    bad.write_text("jobs: [unbalanced\n")
    out = em.check_file(bad)
    assert len(out) == 1
    line, message = out[0]
    assert line is None
    assert "could not parse as YAML" in message


def test_custom_marker_set_is_honored():
    """A repo-specific marker (not in the git-history defaults) is flagged."""
    extra = [("terraform apply", em._marker_regex("terraform apply"))]
    doc = {"jobs": {"j": {"steps": [{"run": "bash .github/scripts/x.sh"}]}}}
    reader = _reader({".github/scripts/x.sh": "terraform apply -auto-approve"})
    assert len(em.analyze(doc, reader, extra)) == 1


# ── main ──────────────────────────────────────────────────────────────────────


def test_main_clean_returns_zero(monkeypatch, capsys):
    monkeypatch.setattr(em, "workflow_files", lambda: [])
    assert em.main() == 0
    assert capsys.readouterr().out == ""


def test_main_reports_and_fails_on_violation(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(em, "REPO_ROOT", tmp_path)
    scripts = tmp_path / ".github" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "autofix.sh").write_text("git rebase --onto main\n")
    wf = tmp_path / "wf.yaml"
    wf.write_text(
        "jobs:\n  autofix:\n    steps:\n      - run: bash .github/scripts/autofix.sh\n"
    )
    monkeypatch.setattr(em, "workflow_files", lambda: [wf])
    assert em.main() == 1
    out = capsys.readouterr().out
    assert "::error file=wf.yaml,line=4::" in out
    assert "externalized-marker blind spot(s)" in out


def test_parse_markers_extracts_repeatable_flag():
    assert em._parse_markers(["--marker", "a", "--marker", "b"]) == ["a", "b"]


def test_parse_markers_missing_argument_exits(monkeypatch):
    import pytest

    with pytest.raises(SystemExit) as exc:
        em._parse_markers(["--marker"])
    assert exc.value.code == 2


# ── repo-wide: the shipped workflows must pass ────────────────────────────────


def test_own_ci_workflow_passes_the_check():
    """ci-truth-serum's own CI workflow must satisfy the check, so the repo
    dogfoods its own lint."""
    ci = REPO_ROOT / ".github" / "workflows" / "ci.yaml"
    assert em.check_file(ci) == [], em.check_file(ci)

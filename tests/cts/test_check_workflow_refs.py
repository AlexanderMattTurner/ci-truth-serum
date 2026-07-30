"""Tests for ci_truth_serum/check_workflow_refs.py — the lint that bans a doc or
comment citing a GitHub Actions workflow file that no longer exists.

Drives `violations()` directly (each narrowing gate member-by-member, the
false-positive carve-outs, fences, the escape hatch) plus `main()`'s CLI contract
against a REAL git repo (the resolution set comes from `git ls-files`), and the
script end-to-end.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from tests._helpers import (
    HOOKS_DIR,
    REPO_ROOT,
    commit_all,
    dogfood_extras_exclude,
    init_test_repo,
    load_hook,
)

_SRC = HOOKS_DIR / "check_workflow_refs.py"
mod = load_hook("check_workflow_refs.py", "check_workflow_refs")

# The resolution sets a repo with one workflow and a few other tracked files has.
WORKFLOWS = {"evals.yaml", "deps-release.yaml"}
TRACKED = WORKFLOWS | {"package.json", "codeql-config.yml", "seed.sh"}


def _hits(line: str, *, prose: bool = False, dot_github: bool = False):
    return mod.violations(line, prose, WORKFLOWS, TRACKED, dot_github)


# -- non-vacuity: red while the workflow is missing, green once it exists -------


def test_red_then_green_when_the_workflow_resolves() -> None:
    line = "# dispatched by release-prep.yaml after the bump lands\n"
    assert _hits(line) == [(1, "release-prep.yaml")]
    resolved = WORKFLOWS | {"release-prep.yaml"}
    assert mod.violations(line, False, resolved, TRACKED, False) == []


# -- gate 1: citations only — a comment/prose line, never a constructed value ---


def test_string_literal_in_code_is_not_a_citation() -> None:
    """A synthetic fixture name a test builds is a value, not a claim about the
    tree — the class that made a blanket rule unusable."""
    body = '    (wf / "ci-workflow.yaml").write_text(SPEC)\n'
    assert _hits(body) == []


CITATION_FORMS = {
    "full_line_hash": ("# the ci-workflow.yaml job posts the status\n", 1),
    "trailing_hash": ("run: ./x.sh  # replaced by ci-workflow.yaml\n", 1),
    "double_slash": ("// dispatched from ci-workflow.yaml\n", 1),
    "block_star": (" * the ci-workflow.yaml job posts the status\n", 1),
}


@pytest.mark.parametrize("body, lineno", CITATION_FORMS.values(), ids=CITATION_FORMS)
def test_each_comment_form_is_scanned(body: str, lineno: int) -> None:
    assert _hits(body) == [(lineno, "ci-workflow.yaml")]


def test_prose_mode_scans_every_line() -> None:
    body = "The nightly CI run lives in ci-workflow.yaml today.\n"
    assert _hits(body, prose=True) == [(1, "ci-workflow.yaml")]


# -- gate 2: a basename that resolves elsewhere in the tree is not dangling -----


def test_tracked_basename_elsewhere_is_not_flagged() -> None:
    line = "# the CodeQL job reads codeql-config.yml before analysing\n"
    assert _hits(line, dot_github=True) == []


# -- gate 3: a bare basename needs a workflow-ish setting -----------------------


def test_bare_basename_without_context_outside_dot_github_is_ignored() -> None:
    line = "# parse the env's compose-spec.yaml into a ComposeSpec\n"
    assert _hits(line) == []


def test_same_line_fires_once_context_appears() -> None:
    line = "# the nightly job parses compose-spec.yaml into a ComposeSpec\n"
    assert _hits(line) == [(1, "compose-spec.yaml")]


def test_dot_github_setting_alone_is_enough() -> None:
    line = "# Invoked by cancel-on-pr-close.yaml with REPO, HEAD_REF, HEAD_SHA\n"
    assert _hits(line) == []
    assert _hits(line, dot_github=True) == [(1, "cancel-on-pr-close.yaml")]


CONTEXT_WORDS = ("workflow", "job", "CI", "Actions", "dispatch", "runner", "cron")


@pytest.mark.parametrize("word", CONTEXT_WORDS)
def test_each_context_word_admits_a_bare_basename(word: str) -> None:
    assert _hits(f"# the {word} lives in ci-workflow.yaml\n") == [
        (1, "ci-workflow.yaml")
    ]


# -- gate 4: paths and reserved ecosystem basenames -----------------------------


def test_workflows_path_needs_no_context_word() -> None:
    line = "# see .github/workflows/breakout-ctf.yaml for the live-fire matrix\n"
    assert _hits(line) == [(1, ".github/workflows/breakout-ctf.yaml")]


@pytest.mark.parametrize(
    "prefix, cited",
    [("", ""), ("./", "./"), ("/srv/repo/", "srv/repo/")],
    ids=["bare", "dot_slash", "absolute"],
)
def test_workflows_path_is_found_however_it_is_anchored(prefix, cited) -> None:
    """A leading `/` is not part of the reference, so an absolute path is
    reported from its first path segment on."""
    line = f"# see {prefix}.github/workflows/breakout-ctf.yaml for the matrix\n"
    assert _hits(line) == [(1, f"{cited}.github/workflows/breakout-ctf.yaml")]


def test_reference_inside_a_url_is_ignored() -> None:
    """A URL names another host's file, which this tree can never resolve."""
    line = "# see https://github.com/o/r/blob/main/.github/workflows/ci-x.yaml\n"
    assert _hits(line, dot_github=True) == []


def test_slashed_path_outside_workflows_dir_is_ignored() -> None:
    line = "# the CI job reads packaging/nfpm/nfpm.yaml for the version\n"
    assert _hits(line, dot_github=True) == []


RESERVED = ("pnpm-lock.yaml", "compose.yml", "docker-compose.yml", "action.yaml")


@pytest.mark.parametrize("name", RESERVED)
def test_reserved_non_workflow_basename_is_never_a_workflow_claim(name: str) -> None:
    """These names are fixed by their tool, so an untracked mention of one is a
    discussion of that tool, not a claim about a missing workflow."""
    assert _hits(f"# this repo commits no {name}, so the CI job skips it\n") == []


# -- fenced code blocks (prose only) --------------------------------------------


def test_fenced_block_is_skipped_in_prose() -> None:
    body = "intro\n\n```\nthe CI job runs ci-workflow.yaml\n```\n\ndone\n"
    assert _hits(body, prose=True) == []


def test_citation_after_a_closed_fence_still_fires() -> None:
    body = "```\nthe CI job runs other.yaml\n```\nthe CI job runs ci-workflow.yaml\n"
    assert _hits(body, prose=True) == [(4, "ci-workflow.yaml")]


# -- escape hatch ----------------------------------------------------------------


def test_allow_marker_same_line_suppresses() -> None:
    line = "# the CI job runs ci-workflow.yaml  # allow-workflow-ref: template repo\n"
    assert _hits(line) == []


def test_allow_marker_line_above_suppresses() -> None:
    body = (
        "<!-- allow-workflow-ref: the template repo's own CI -->\n"
        "The CI job runs ci-workflow.yaml upstream.\n"
    )
    assert _hits(body, prose=True) == []


def test_allow_marker_requires_a_reason() -> None:
    line = "# the CI job runs ci-workflow.yaml  # allow-workflow-ref:\n"
    assert _hits(line) == [(1, "ci-workflow.yaml")]


def test_allow_marker_two_lines_above_does_not_suppress() -> None:
    body = (
        "<!-- allow-workflow-ref: too far away -->\nfiller\n"
        "The CI job runs ci-workflow.yaml.\n"
    )
    assert _hits(body, prose=True) == [(3, "ci-workflow.yaml")]


# -- main(): CLI contract against a real git repo ---------------------------------


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A committed git repo with FILES (repo-relative path → content)."""
    init_test_repo(tmp_path)
    for rel, text in files.items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    commit_all(tmp_path)
    mod.REPO_ROOT = tmp_path
    return tmp_path


WORKFLOW_YAML = "name: Evals\non: push\njobs: {}\n"


def test_main_reports_path_line_name_and_remedy(tmp_path: Path, capsys) -> None:
    repo = _repo(
        tmp_path,
        {
            ".github/workflows/evals.yaml": WORKFLOW_YAML,
            "docs/ci.md": "one\nThe CI job lives in ct-inspect-e2e.yaml today.\n",
        },
    )
    doc = repo / "docs" / "ci.md"
    assert mod.main([str(doc)]) == 1
    err = capsys.readouterr().err
    assert f"{doc}:2: `ct-inspect-e2e.yaml`" in err
    assert "does not exist under .github/workflows/" in err


@pytest.mark.parametrize(
    "name, source, flagged",
    [
        # A block comment after code on the same line: no ` # `/` // ` delimiter,
        # so the delimiter scan read the whole line as code and never saw it.
        ("trailing block comment", "run(); /* dispatched by gone.yaml */", True),
        # `//` inside a string opens nothing, so the citation is a value the
        # program builds — the delimiter scan read it as a comment and flagged it.
        ("string containing a //", 'const m = "see // gone.yaml job";', False),
        # Positive control, so the "no" row cannot pass by the check going inert.
        ("line comment", "// dispatched by gone.yaml", True),
    ],
)
def test_js_comments_come_from_the_grammar(
    tmp_path: Path, name: str, source: str, flagged: bool
) -> None:
    repo = _repo(
        tmp_path,
        {
            ".github/workflows/evals.yaml": WORKFLOW_YAML,
            "a.mjs": source + "\n",
        },
    )
    assert mod.main([str(repo / "a.mjs")]) == int(flagged)


def test_main_returns_zero_once_the_workflow_exists(tmp_path: Path, capsys) -> None:
    repo = _repo(
        tmp_path,
        {
            ".github/workflows/evals.yaml": WORKFLOW_YAML,
            ".github/workflows/ct-inspect-e2e.yaml": WORKFLOW_YAML,
            "docs/ci.md": "The CI job lives in ct-inspect-e2e.yaml today.\n",
        },
    )
    assert mod.main([str(repo / "docs" / "ci.md")]) == 0
    assert capsys.readouterr().err == ""


def test_main_is_a_noop_in_a_repo_with_no_workflows(tmp_path: Path, capsys) -> None:
    """Nothing to resolve against, so the repo is out of scope rather than wholly
    in violation."""
    repo = _repo(tmp_path, {"docs/ci.md": "The CI job lives in evals.yaml today.\n"})
    assert mod.main([str(repo / "docs" / "ci.md")]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "rel", ["CHANGELOG.md", "docs/CHANGELOG.md", "changelog.d/42.added.md"]
)
def test_main_skips_the_changelog_surfaces(tmp_path: Path, rel: str) -> None:
    """A released entry — and a pending fragment assembled into one verbatim — is
    an audit record: the workflow it names really did exist when it was written."""
    repo = _repo(
        tmp_path,
        {
            ".github/workflows/evals.yaml": WORKFLOW_YAML,
            rel: "The CI job moved out of ct-inspect-e2e.yaml.\n",
        },
    )
    assert mod.main([str(repo / rel)]) == 0


def test_main_skips_an_unreadable_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path, {".github/workflows/evals.yaml": WORKFLOW_YAML})
    assert mod.main([str(repo / "absent.md")]) == 0


def test_main_treats_a_dot_github_file_as_workflow_context(tmp_path: Path) -> None:
    """The same sentence is a citation inside .github/ and ambiguous outside it."""
    body = "# superseded by cancel-on-pr-close.yaml\n"
    repo = _repo(
        tmp_path,
        {
            ".github/workflows/evals.yaml": WORKFLOW_YAML,
            ".github/scripts/x.sh": body,
            "bin/x.sh": body,
        },
    )
    assert mod.main([str(repo / ".github" / "scripts" / "x.sh")]) == 1
    assert mod.main([str(repo / "bin" / "x.sh")]) == 0


# -- end-to-end: the real CLI entrypoint -------------------------------------------


def test_cli_invocation_flags_and_exits_nonzero(tmp_path: Path) -> None:
    init_test_repo(tmp_path)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "evals.yaml").write_text(WORKFLOW_YAML)
    (tmp_path / "docs.md").write_text("The CI job runs ct-inspect-e2e.yaml.\n")
    commit_all(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(_SRC), "docs.md"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "docs.md:1: `ct-inspect-e2e.yaml`" in proc.stderr


def test_enforced_scope_is_clean() -> None:
    """Every tracked file this hook's types_or covers (minus the dogfood excludes,
    the one authoritative skip list) passes today. Non-vacuous: the gate cases
    above show `violations` fires, and the scope selection is asserted non-empty."""
    exclude = dogfood_extras_exclude()
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    suffixes = {
        ".md",
        ".rst",
        ".py",
        ".sh",
        ".bash",
        ".mjs",
        ".js",
        ".ts",
        ".yaml",
        ".yml",
    }
    scanned = [
        str(REPO_ROOT / rel)
        for rel in tracked
        if rel and Path(rel).suffix in suffixes and not exclude.match(rel)
    ]
    assert scanned, "scope selection found nothing — the assertion would be vacuous"
    mod.REPO_ROOT = REPO_ROOT
    assert mod.main(scanned) == 0

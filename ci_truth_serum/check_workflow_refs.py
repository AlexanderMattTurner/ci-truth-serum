#!/usr/bin/env python3
"""Fail when a doc or comment cites a GitHub Actions workflow file that is gone.

Consolidating workflows is routine — four `ct-*.yaml` files fold into one
`evals.yaml` driven by a `suite:` input, a `phone-home.yaml` becomes a job inside
`deps-release.yaml` — and every sentence that named the old file silently becomes
a lie. Nothing catches it, because a workflow is almost always cited by BARE
BASENAME (`release-prep.yaml`) rather than by a path a link checker could
resolve. The damage is worst in contributor-facing prose: a `.github/CLAUDE.md`
telling the next author to "copy the shape from `devcontainer-checks.yaml`'s
reporters" sends them looking for a file that does not exist.

Resolution target: the reference must name a git-tracked file under
`.github/workflows/`. A repo with no tracked workflow at all is skipped whole —
there is nothing to resolve against, so every hit would be noise.

NARROWING — why this is not "flag every bare `*.yaml` token". Dogfooding the
blanket form against a large consumer produced 25 distinct tokens of which
roughly half were legitimate non-workflow names (`compose.yml`, `spec.yaml`,
`pnpm-lock.yaml`, `action.yaml`, `secrets.yaml`) or synthetic fixture names
invented inside a test (`wf.yaml`, `four-jobs.yaml`, `.github/workflows/x.yaml`).
A hook that noisy gets disabled, and ci-truth-serum hooks are stateless (no
baseline file exists to grandfather the noise away), so the rule has to be
precise by construction. Four gates, each removing one of those classes:

  1. **Citations only, never constructed values.** In a code file only COMMENT
     bodies are scanned; in Markdown/rst every non-fenced line is. A workflow
     name written as a string literal is a value the code builds (a tmp-dir
     fixture, a path being assembled), not a claim about this repo's tree —
     which is exactly what every synthetic `wf.yaml` / `x.yaml` fixture is.
  2. **A basename that resolves elsewhere in the tree is not dangling.** If some
     tracked file anywhere carries that basename, the sentence points at a real
     file and no reader is misled — that is `compose.yml`, `spec.yaml`,
     `action.yaml`, `pnpm-lock.yaml`, `codeql-config.yml`.
  3. **A bare basename needs a workflow-ish setting** — either the citing file
     lives under `.github/` (where an unresolvable `*.ya?ml` basename is
     overwhelmingly a sibling workflow), or the line itself carries CI
     vocabulary ("workflow", "job", "CI", "Actions", "dispatch", "runner",
     "cron", …). Either alone is enough; neither means the mention is a
     config-file name this check does not adjudicate (`compose.yml` in an
     eval-harness docstring, `secrets.yaml` in an illustrative sentence). A
     `.github/workflows/<name>.ya?ml` path needs no such evidence: naming that
     directory IS the workflow claim.
  4. **A slashed path that is not under `.github/workflows/` is ignored**, and a
     basename an ecosystem reserves for something that is definitionally NOT a
     workflow (`pnpm-lock.yaml`, `compose.yml`, a composite action's
     `action.yaml`) is never a workflow claim even inside `.github/` — those
     names are fixed by their tool, so the list can't rot into a per-repo
     baseline. A reference inside an `http(s)://` URL is skipped for the same
     reason: it names another host's file, which this tree can never resolve.

Gate 3's vocabulary is matched against the whole line, so a name that itself
reads as CI (`release-prep.yaml`, `deploy-gate.yaml`) supplies its own evidence.
That is deliberate: a basename spelled like a workflow, absent from
`.github/workflows/` AND absent from the entire tracked tree, is precisely what
this check exists to find.

A CHANGELOG.md is always skipped, wherever it lives, and so is anything under a
`changelog.d/` fragment directory: released entries are an immutable audit record
of what a past change touched, and a pending fragment is assembled into that
record verbatim. The workflow an entry names really did exist when it was
written, so rewording it would falsify the record — and annotating a fragment
would leak the annotation into the shipped changelog.

Escape hatch: an inline `# allow-workflow-ref: <reason>` (or the Markdown
`<!-- allow-workflow-ref: <reason> -->` form) on the offending line or the one
directly above, with a REQUIRED reason. Reach for it when the name genuinely
belongs to another repo — a template's workflow, an upstream project's — which
this repo's tree can never resolve.

Invoked by pre-commit with the staged prose/commented-code files as arguments.
"""

import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _comments import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    comment_lines,
    text_comments,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
)

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override this.
REPO_ROOT = Path.cwd()
WORKFLOWS_PREFIX = ".github/workflows/"

# A file reference ending in .yaml/.yml, with any leading path kept so the
# `.github/workflows/` form can be told from a bare basename. The left boundary
# stops a longer path/name being sliced mid-token (`…/my-compose.yml`).
_REF_RE = re.compile(
    r"(?<![\w.-])(?P<path>(?:[\w.-]+/)*(?P<base>[\w][\w.-]*\.ya?ml))\b"
)
# A reference inside an http(s) URL names another host's file, which this repo's
# tree can never resolve — the same carve-out check_doc_line_refs makes.
_URL_RE = re.compile(r"https?://\S+")
# Evidence the sentence is talking about CI rather than some config file. Only
# consulted for a BARE basename in a file outside `.github/`; a
# `.github/workflows/…` path is self-evident, and inside `.github/` the setting
# already supplies the evidence.
_CONTEXT_RE = re.compile(
    r"\b(?:workflow|workflows|job|jobs|CI|Actions|dispatch|dispatches|dispatched"
    r"|runner|runners|cron|scheduled|reusable|pipeline|rerun|reruns|re-run"
    r"|required|check|checks|gate|gates|reporter|reporters|autofix|release)\b"
)
_DOT_GITHUB = re.compile(r"(?:^|/)\.github/")
# Basenames an ecosystem reserves for a file that is definitionally not a
# GitHub Actions workflow. Gate 2 already spares any of these that the repo
# tracks; this covers the case where the name is DISCUSSED but not present — a
# comment explaining that this repo commits no `pnpm-lock.yaml` must not read as
# a claim about a missing workflow.
_NON_WORKFLOW_BASENAMES = frozenset(
    {
        "action.yaml",
        "action.yml",
        "compose.yaml",
        "compose.yml",
        "dependabot.yaml",
        "dependabot.yml",
        "docker-compose.yaml",
        "docker-compose.yml",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
    }
)
# The changelog surfaces whose text is an audit record, skipped whole.
_CHANGELOG_RE = re.compile(r"(?:^|/)(?:CHANGELOG\.md|changelog\.d/)")
_ALLOW = "allow-workflow-ref"
_PROSE_SUFFIXES = frozenset({".md", ".markdown", ".rst"})

MESSAGE = (
    "cites a workflow that does not exist under .github/workflows/ — the file "
    "was renamed, consolidated, or deleted; name the workflow that runs it now "
    "(suppress a reference to another repo's workflow with "
    "`allow-workflow-ref: <reason>`)."
)


def workflow_basenames() -> set[str]:
    """Basenames of the git-tracked files under `.github/workflows/` — the set a
    reference must resolve into."""
    return {Path(p).name for p in _tracked(WORKFLOWS_PREFIX)}


def tracked_basenames() -> set[str]:
    """Basenames of every git-tracked file. A reference matching one of these
    points at a real file somewhere, so it is not a dangling claim."""
    return {Path(p).name for p in _tracked()}


def _tracked(*pathspec: str) -> list[str]:
    """Tracked paths in the repo being linted, optionally under PATHSPEC."""
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspec],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in proc.stdout.split("\0") if p]


def _dangling(
    line: str, workflows: set[str], tracked: set[str], in_dot_github: bool
) -> str | None:
    """The first reference in LINE naming a workflow absent from WORKFLOWS, or
    None. See the module docstring for the four narrowing gates."""
    url_spans = [m.span() for m in _URL_RE.finditer(line)]
    for match in _REF_RE.finditer(line):
        base, path = match.group("base"), match.group("path")
        if base in workflows or base in _NON_WORKFLOW_BASENAMES:
            continue
        if any(start <= match.start() < end for start, end in url_spans):
            continue
        if path.endswith(WORKFLOWS_PREFIX + base):
            return path
        # Any other slashed path asserts a location this check does not
        # adjudicate; a bare basename is a workflow claim only in a CI setting.
        workflowish = in_dot_github or bool(_CONTEXT_RE.search(line))
        if "/" in path or base in tracked or not workflowish:
            continue
        return base
    return None


def violations(
    text: str,
    prose: bool,
    workflows: set[str],
    tracked: set[str],
    in_dot_github: bool,
    comments: dict[int, str] | None = None,
) -> list[tuple[int, str]]:
    """(1-based line, cited name) for every un-suppressed dangling workflow
    reference. In PROSE mode every line outside a fenced code block is scanned;
    in code mode only comment bodies are. IN_DOT_GITHUB says the scanned file
    lives under `.github/`, which by itself makes a bare basename a workflow
    claim (see gate 3).

    COMMENTS maps 1-based line -> comment body, from ``comment_lines`` — omitting
    it applies the text delimiter scan to the whole file, which only the caller's
    path can improve on. It is unused in PROSE mode, where every line counts."""
    lines = text.split("\n")
    if comments is None:
        comments = text_comments(text)
    hits: list[tuple[int, str]] = []
    in_fence = False
    for lineno, raw in enumerate(lines, 1):
        if prose and raw.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        target = raw if prose else comments.get(lineno)
        if target is None or (prose and in_fence):
            continue
        cited = _dangling(target, workflows, tracked, in_dot_github)
        if cited is None:
            continue
        if annotated(raw, _ALLOW) or (
            lineno >= 2 and annotated(lines[lineno - 2], _ALLOW)
        ):
            continue
        hits.append((lineno, cited))
    return hits


def main(argv: list[str]) -> int:
    workflows = workflow_basenames()
    if not workflows:
        # No tracked workflow to resolve against — every reference would be
        # reported, so the repo is out of scope rather than wholly in violation.
        return 0
    tracked = tracked_basenames()
    status = 0
    for path in argv:
        if _CHANGELOG_RE.search(path.replace("\\", "/")):
            continue
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        prose = Path(path).suffix.lower() in _PROSE_SUFFIXES
        in_dot_github = bool(_DOT_GITHUB.search(path.replace("\\", "/")))
        for lineno, cited in violations(
            text, prose, workflows, tracked, in_dot_github, comment_lines(text, path)
        ):
            print(f"{path}:{lineno}: `{cited}` — {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

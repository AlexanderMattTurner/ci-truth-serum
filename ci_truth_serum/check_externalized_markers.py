#!/usr/bin/env python3
"""Catch workflow-introspecting guards that go blind when logic moves into scripts.

A CI guard often enforces a safety invariant by scanning a job's inline `run:`
text for a marker command and, when it finds one, asserting some property of that
job. The canonical case: a job that rewrites git history (`git commit --amend`,
`git rebase`) MUST check out with `fetch-depth: 0`, or an amend/rebase on the
depth-1 graft orphans the commit and a force-push severs the PR from its base
(GitHub then auto-closes it).

Such a guard has a silent blind spot. The moment the marker command is extracted
out of the inline `run:` body into `.github/scripts/<name>.sh` (invoked as
`run: bash .github/scripts/<name>.sh`) or into a local composite action
(`uses: ./.github/actions/<name>`), the marker no longer appears in the `run:`
text — so an inline-only guard stops seeing that job and passes VACUOUSLY. This is
especially dangerous in repos with a policy of externalizing inline shell (this
pack's own `check-inline-run-length` pushes exactly that refactor): the safe move
is the thing that blinds the guard.

This lint is the positive form of that check: for a policy marker set (default:
the git history-rewrite commands), it scans BOTH the inline `run:` text of each
job AND every referenced `.github/scripts/*.sh` script and `./.github/actions/*`
composite. It flags any job where the two scans DISAGREE — i.e. a marker lives
only in externalized code. That delta is exactly the blind spot: proof that an
inline-only guard would miss this job. Fix the guard to resolve the indirection,
keep the marker inline, or annotate the invoking step (or the referenced script)
with `# allow-externalized-marker: <reason>` to opt out.

Scope: indirection is followed ONE hop past a composite — a marker inside a
composite that itself `uses:` a further nested composite is not resolved (a
deliberate limit; the common shape is job → script or job → composite → script).
Add markers with `--marker '<cmd>'` (repeatable; each is matched
whitespace-insensitively but bounded so `--force` does not match inside
`--force-with-lease`). Globs every workflow + composite action like the other
workflow lints; the passed file list is ignored.
"""

import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import LineLoader as _LineLoader  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import annotation_re  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

# Opt out of a legitimately-externalized marker with this comment on the invoking
# `run:` step or inside the referenced script.
OPT_OUT = "allow-externalized-marker"
_OPTOUT_RE = annotation_re(OPT_OUT)

# The default policy marker set: git commands that rewrite history. A job running
# any of these must check out with `fetch-depth: 0`; a guard enforcing that by
# scanning inline `run:` text is the exact guard this lint protects from going
# blind. Extend with `--marker` for repo-specific invariants.
DEFAULT_MARKERS = (
    "git commit --amend",
    "git rebase",
    "git filter-branch",
    "git push --force",
    "git push -f",
    "git push --force-with-lease",
)

# A reference to a repo-local shell script under .github/scripts, however it is
# invoked (`bash …`, `sh …`, `. …`, `./…`, or embedded in a longer path). The
# path token is captured wherever it appears in the run body.
_SCRIPT_REF = re.compile(r"(?:\./)?(?P<path>\.github/scripts/[\w./-]+?\.(?:sh|bash))\b")

# A reader over repo-relative paths — returns file text, or "" when unreadable.
Reader = Callable[[str], str]


def _marker_regex(marker: str) -> re.Pattern[str]:
    """Compile a marker string into a whitespace-insensitive matcher bounded so it
    matches a whole command token: `git   commit  --amend` matches
    `git commit --amend`, but `git push --force` does NOT match inside
    `git push --force-with-lease` (the trailing `-` breaks the boundary)."""
    body = r"\s+".join(re.escape(tok) for tok in marker.split())
    return re.compile(rf"(?<![\w-]){body}(?![\w-])")


def markers_present(text: str, markers: list[tuple[str, re.Pattern[str]]]) -> set[str]:
    """The subset of MARKERS whose command appears in TEXT."""
    return {name for name, pat in markers if pat.search(text)}


def referenced_scripts(text: str) -> list[str]:
    """Every `.github/scripts/*.sh|bash` path referenced in TEXT, de-duplicated in
    first-seen order (leading `./` normalized away)."""
    seen: dict[str, None] = {}
    for match in _SCRIPT_REF.finditer(text):
        seen.setdefault(match.group("path"), None)
    return list(seen)


def _composite_dir(uses: object) -> str | None:
    """The repo-relative directory of a LOCAL composite action `uses:` value
    (`./.github/actions/foo` → `.github/actions/foo`), or None for a remote/no ref.
    A local action reference starts with `./` and carries no `@ref`."""
    text = str(uses).strip()
    if not text.startswith("./"):
        return None
    return text[2:].rstrip("/")


def _read_action(dir_rel: str, reader: Reader) -> list[tuple[str, str]]:
    """A local composite action resolved to (label, text) sources: its
    action.yml/action.yaml (whose inline `run:` bodies live there verbatim) plus
    every script that action references — one hop past the composite. Empty when
    no definition is readable. Each source is kept separate so a marker can never
    be matched across a source boundary."""
    for name in ("action.yml", "action.yaml"):
        rel = f"{dir_rel}/{name}"
        content = reader(rel)
        if not content:
            continue
        return [(rel, content)] + [(s, reader(s)) for s in referenced_scripts(content)]
    return []


def _step_external(run: str, uses: object, reader: Reader) -> list[tuple[str, str]]:
    """The (label, text) sources reachable from one step by resolving
    `bash .github/scripts/*.sh` and `uses: ./.github/actions/*` indirection —
    exactly what an inline-only guard would fail to see. Inline `run:` text is NOT
    included."""
    sources = [(rel, reader(rel)) for rel in referenced_scripts(run)]
    composite = _composite_dir(uses)
    if composite is not None:
        sources += _read_action(composite, reader)
    return sources


def _iter_steps(steps: object) -> list[tuple[int | None, str, object]]:
    """Every (line, run text, uses value) in STEPS. LINE is the step's 1-based
    source line (None when parsed without line tags, e.g. a unit-test dict)."""
    if not isinstance(steps, list):
        return []
    out = []
    for step in steps:
        if not isinstance(step, dict):
            continue
        run = step.get("run") if isinstance(step.get("run"), str) else ""
        out.append((step.get("__line__"), run, step.get("uses")))
    return out


def _analyze_unit(
    unit_id: str,
    line: int | None,
    steps: object,
    markers: list[tuple[str, re.Pattern[str]]],
    reader: Reader,
) -> list[tuple[int | None, str]]:
    """Flag a job / composite (a UNIT of steps) for every policy marker that lives
    only in externalized code the unit invokes, never in its inline `run:` text."""
    parsed = _iter_steps(steps)
    inline_markers = markers_present(
        "\n".join(run for _line, run, _uses in parsed), markers
    )
    found: list[tuple[int | None, str]] = []
    for step_line, run, uses in parsed:
        sources = _step_external(run, uses, reader)
        if _OPTOUT_RE.search(run) or any(
            _OPTOUT_RE.search(text) for _label, text in sources
        ):
            continue
        blind: set[str] = set()
        blind_labels: list[str] = []
        for label, text in sources:
            hit = markers_present(text, markers) - inline_markers
            if hit:
                blind |= hit
                blind_labels.append(label)
        if not blind:
            continue
        found.append(
            (
                step_line if step_line is not None else line,
                f"{unit_id}: policy marker(s) {sorted(blind)} appear only in "
                f"externalized code ({', '.join(blind_labels)}), not in any inline "
                "run: — an inline-only workflow guard is BLIND to this job and "
                "passes vacuously. Resolve script/composite indirection in the "
                f"guard, keep the marker inline, or annotate with `# {OPT_OUT}: "
                "<reason>`.",
            )
        )
    return found


def analyze(
    doc: object,
    reader: Reader,
    markers: list[tuple[str, re.Pattern[str]]],
) -> list[tuple[int | None, str]]:
    """Every blind-spot violation as (line, message). READER(rel_path) returns the
    text of a repo-relative file (or "" if unreadable)."""
    if not isinstance(doc, dict):
        return []
    found: list[tuple[int | None, str]] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            found += _analyze_unit(
                f"job {job_id}", job.get("__line__"), job.get("steps"), markers, reader
            )
    runs = doc.get("runs")
    if isinstance(runs, dict):
        found += _analyze_unit(
            "composite action", runs.get("__line__"), runs.get("steps"), markers, reader
        )
    return found


def _repo_reader(rel: str) -> str:
    """Read a repo-relative file under REPO_ROOT, "" if unreadable — a missing
    referenced script or action is simply nothing to scan, not a crash."""
    try:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def check_file(
    path: Path, markers: list[tuple[str, re.Pattern[str]]] | None = None
) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH. A file this lint cannot parse
    as YAML is itself reported as a violation (line ``None``) rather than
    silently passed as clean: "no violations" on unparseable input would be a
    silent false-green, not a real result. (YAML *syntax* is actionlint's job —
    this only fires when PyYAML can't build a document to analyze at all.)"""
    if markers is None:
        markers = [(m, _marker_regex(m)) for m in DEFAULT_MARKERS]
    try:
        doc = yaml.load(path.read_text(), Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify externalized-"
                "marker reachability — fix the syntax (or run actionlint) and re-check.",
            )
        ]
    return analyze(doc, _repo_reader, markers)


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def _parse_markers(argv: list[str]) -> list[str]:
    """Extra `--marker '<cmd>'` values (repeatable) to add to the defaults."""
    extra: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] == "--marker":
            if i + 1 >= len(argv):
                print("error: --marker requires an argument", file=sys.stderr)
                sys.exit(2)
            extra.append(argv[i + 1])
            i += 2
        else:
            i += 1
    return extra


def main() -> int:
    markers = [
        (m, _marker_regex(m)) for m in (*DEFAULT_MARKERS, *_parse_markers(sys.argv[1:]))
    ]
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path, markers):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} externalized-marker blind spot(s) found.")
        print(
            "A policy marker was reachable only through script/composite "
            "indirection, so an inline-only workflow guard would miss it."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

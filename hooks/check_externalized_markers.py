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
especially dangerous in repos with a policy of externalizing inline shell (the
safe refactor is the thing that blinds the guard).

This lint is the positive form of that check: for a policy marker set (default:
the git history-rewrite commands), it scans BOTH the inline `run:` text of each
job AND every referenced `.github/scripts/*.sh` script and `./.github/actions/*`
composite. It flags any job where the two scans DISAGREE — i.e. a marker lives
only in externalized code. That delta is exactly the blind spot: proof that an
inline-only guard would miss this job. Fix the guard to resolve the indirection
(or keep the check inline).

Add markers with `--marker '<cmd>'` (repeatable; each is matched
whitespace-insensitively). Globs every workflow + composite action like the other
workflow lints; the passed file list is ignored.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position


class _LineLoader(yaml.SafeLoader):
    """SafeLoader that tags every mapping with `__line__` (the 1-based source line
    of its first key) so a flagged step can be reported with a navigable
    file/line annotation instead of a bare, unclickable `::error::`."""


def _mapping_with_line(loader: _LineLoader, node: yaml.MappingNode) -> dict:
    mapping = loader.construct_mapping(node, deep=True)
    mapping["__line__"] = node.start_mark.line + 1
    return mapping


_LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_with_line
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

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


def _marker_regex(marker: str) -> re.Pattern[str]:
    """Compile a marker string into a whitespace-insensitive matcher, so
    `git   commit  --amend` in a script matches `git commit --amend`."""
    return re.compile(r"\s+".join(re.escape(tok) for tok in marker.split()))


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


def _read_action(dir_rel: str, reader) -> tuple[str, str] | None:
    """Resolve a local composite action's definition to (label, scanned text).

    Returns the raw action.yml/action.yaml text (its inline `run:` bodies live
    there verbatim) concatenated with every script that action references — one
    hop of indirection past the composite. None if no definition is readable.
    """
    for name in ("action.yml", "action.yaml"):
        rel = f"{dir_rel}/{name}"
        content = reader(rel)
        if not content:
            continue
        extra = "\n".join(reader(s) for s in referenced_scripts(content))
        return rel, f"{content}\n{extra}"
    return None


def _step_external(run: str, uses: object, reader) -> tuple[list[str], str]:
    """The (source labels, concatenated text) reachable from one step by resolving
    `bash .github/scripts/*.sh` and `uses: ./.github/actions/*` indirection.
    Inline `run:` text is NOT included — this is exactly what an inline-only guard
    would fail to see."""
    labels: list[str] = []
    texts: list[str] = []
    for rel in referenced_scripts(run):
        labels.append(rel)
        texts.append(reader(rel))
    composite = _composite_dir(uses)
    if composite is not None:
        resolved = _read_action(composite, reader)
        if resolved is not None:
            labels.append(resolved[0])
            texts.append(resolved[1])
    return labels, "\n".join(texts)


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
    reader,
) -> list[tuple[int | None, str]]:
    """Flag a job / composite (a UNIT of steps) for every policy marker that lives
    only in externalized code the unit invokes, never in its inline `run:` text."""
    parsed = _iter_steps(steps)
    inline_markers = markers_present(
        "\n".join(run for _line, run, _uses in parsed), markers
    )
    found: list[tuple[int | None, str]] = []
    for step_line, run, uses in parsed:
        labels, external = _step_external(run, uses, reader)
        blind = markers_present(external, markers) - inline_markers
        if not blind:
            continue
        found.append(
            (
                step_line if step_line is not None else line,
                f"{unit_id}: policy marker(s) {sorted(blind)} live only in "
                f"externalized code ({', '.join(labels)}) — no inline run: in "
                "this job contains them. A CI guard that scans only inline run: "
                "text is BLIND here and passes vacuously. Make the guard resolve "
                "`bash .github/scripts/*.sh` and `uses: ./.github/actions/*` "
                "indirection, or keep the marker inline.",
            )
        )
    return found


def analyze(
    doc: object,
    reader,
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
    """(line, message) for every violation in PATH; [] on unparseable YAML."""
    if markers is None:
        markers = [(m, _marker_regex(m)) for m in DEFAULT_MARKERS]
    try:
        doc = yaml.load(path.read_text(), Loader=_LineLoader)
    except (yaml.YAMLError, OSError, UnicodeDecodeError):
        return []
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

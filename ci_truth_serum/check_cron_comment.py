#!/usr/bin/env python3
"""Fail a workflow whose human-readable schedule comment contradicts its cron.

The comment above a `cron:` line is what reviewers read; the expression is
what runs. Nothing ties them together, so they drift: in two sibling repos a
header said "daily" while the cron was weekly, and the job silently ran 1/7th
as often as everyone believed.

Scope (precision over recall — anything unparseable or ambiguous passes):

  * a comment on the `cron:` line, or within the 3 lines above it, claiming
    `hourly` / `daily` / `weekly` / `monthly` / `every N minutes|hours|days`;
  * a 5-field cron whose shape clearly maps to one of those cadences
    (fixed-vs-`*`/`*/N` fields; lists, ranges, and exotic forms are treated
    as unclassifiable and pass).

Opt out with `# cron-comment-ok` on the `cron:` line. Globs every workflow
like the other workflow lints; the passed file list is ignored.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import annotated  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import workflow_files as _workflow_files  # noqa: E402,I001  # pylint: disable=wrong-import-position

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

OPT_OUT = "cron-comment-ok"
COMMENT_WINDOW = 3  # lines above the cron line that a claim may live on

_CRON_LINE = re.compile(r"^\s*-?\s*cron\s*:\s*[\"']?(?P<expr>[^\"'#]+)")
_CLAIM = re.compile(
    r"\b(?:(?P<word>hourly|daily|weekly|monthly)|every\s+(?P<n>\d+)\s+"
    r"(?P<unit>minute|hour|day)s?)\b",
    re.IGNORECASE,
)

_FIXED = re.compile(r"^\d+$")
_STEP = re.compile(r"^\*/(?P<n>\d+)$")


def classify(expr: str) -> str | None:
    """The cadence a 5-field cron expression clearly encodes, or None when the
    shape is ambiguous (lists, ranges, names, step days, …) — unclassifiable
    expressions never produce findings."""
    fields = expr.split()
    if len(fields) != 5:
        return None
    minute, hour, dom, mon, dow = fields
    if mon != "*":
        return None  # fixed/exotic month — yearly-ish, out of scope
    simple = lambda f: f == "*" or _FIXED.match(f) or _STEP.match(f)  # noqa: E731
    if not all(simple(f) for f in (minute, hour, dom, dow)):
        return None
    if _STEP.match(dom) or _STEP.match(dow):
        return None
    if _FIXED.match(dow):
        # A fixed weekday is weekly — but only when the time of day is fixed too.
        return "weekly" if _FIXED.match(minute) and _FIXED.match(hour) else None
    if _FIXED.match(dom):
        return "monthly" if _FIXED.match(minute) and _FIXED.match(hour) else None
    # dom == dow == "*": cadence is set by the time fields.
    if hour == "*":
        if _FIXED.match(minute):
            return "hourly"
        step = _STEP.match(minute)
        return f"every {step.group('n')} minutes" if step else None
    step = _STEP.match(hour)
    if step and _FIXED.match(minute):
        return f"every {step.group('n')} hours"
    if _FIXED.match(hour) and _FIXED.match(minute):
        return "daily"
    return None


def _claimed(comment_text: str) -> str | None:
    """The cadence a comment claims, normalized to `classify`'s vocabulary, or
    None when it claims nothing recognizable."""
    m = _CLAIM.search(comment_text)
    if not m:
        return None
    if m.group("word"):
        return m.group("word").lower()
    n, unit = int(m.group("n")), m.group("unit").lower()
    if n == 1:
        return {"minute": None, "hour": "hourly", "day": "daily"}[unit]
    return f"every {n} {unit}s"


def _matches(claimed: str, actual: str) -> bool:
    """True when the claimed cadence is consistent with the actual one. `every
    N days` is deliberately generous: cron cannot express it cleanly, so any
    day-based cadence passes."""
    if claimed == actual:
        return True
    equivalent = {
        ("hourly", "every 1 hours"),
        ("daily", "every 24 hours"),
    }
    if (claimed, actual) in equivalent:
        return True
    if claimed.startswith("every") and claimed.endswith("days"):
        return actual in ("daily", "weekly", "monthly")
    return False


def violations(text: str) -> list[tuple[int, str]]:
    """(1-based line, message) for every cron line whose nearby comment claims
    a cadence the expression contradicts."""
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        m = _CRON_LINE.match(line)
        if not m or line.lstrip().startswith("#"):
            continue
        # The claim window: this line plus up to COMMENT_WINDOW lines above,
        # stopping at a previous `cron:` line so a sibling schedule's comment
        # is never attributed to this one.
        window = [line]
        for above in reversed(lines[max(0, idx - COMMENT_WINDOW) : idx]):
            if _CRON_LINE.match(above):
                break
            window.append(above)
        if any(annotated(w, OPT_OUT, require_reason=False) for w in window):
            continue
        comments = " ".join(w.split("#", 1)[1] for w in window if "#" in w)
        claimed = _claimed(comments)
        if claimed is None:
            continue
        actual = classify(m.group("expr").strip())
        if actual is None or _matches(claimed, actual):
            continue
        found.append(
            (
                idx + 1,
                f'schedule comment says "{claimed}" but the cron expression '
                f"`{m.group('expr').strip()}` runs {actual} — the comment is what "
                "reviewers trust. Fix whichever is wrong, or annotate "
                f"`# {OPT_OUT}`.",
            )
        )
    return found


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in violations(path.read_text(encoding="utf-8")):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} cron-comment violation(s) found.")
        print("A schedule comment contradicting its cron misleads every reviewer.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Require an explicit `timeout-minutes` on every GitHub Actions job.

A job with no `timeout-minutes` inherits GitHub's default of 360 minutes — six
hours. A single wedged step (a network fetch with no deadline, a hung test, a
deadlocked container) then pins a runner slot for six hours before GitHub kills
it, and on a small shared/self-hosted pool one such job starves every other
workflow behind it. The default is almost never what anyone wants, and it is
invisible in the YAML: nothing in the file says "this job may run for six hours."

This lint rejects any `jobs.<id>` that omits `timeout-minutes`. Setting it to any
integer is the fix — pick a ceiling comfortably above the job's real runtime.

A reusable-workflow call (`jobs.<id>.uses: …`) is exempt: `timeout-minutes` is not
valid on a `uses:` job (the called workflow's own jobs carry their own timeouts),
so requiring it there would be un-satisfiable.

A job that must genuinely run unbounded (a deliberately long-lived watcher) opts
out with a `# allow-no-timeout: <reason>` comment on the job's key line or one of
its body lines — the reason is REQUIRED; a bare annotation does not suppress.

This lint is opinionated (Tier 2): it prescribes that every job declare its own
ceiling rather than inherit the six-hour default.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _linecheck import _job_blocks  # noqa: E402,I001  # pylint: disable=wrong-import-position

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# The annotation only suppresses when it carries a non-empty reason after the colon.
_ALLOW_WITH_REASON = re.compile(r"#\s*allow-no-timeout:\s*\S")

_MESSAGE = (
    "job '{name}' has no timeout-minutes — it inherits GitHub's 360-minute (6h) "
    "default, so a single wedged step pins a runner slot for six hours and "
    "starves a shared pool. Set timeout-minutes to an explicit ceiling above the "
    "job's real runtime, or add '# allow-no-timeout: <reason>' if it must run "
    "unbounded."
)


def _block_opted_out(block_text: str) -> bool:
    """True if a reason-bearing `# allow-no-timeout:` appears in a `#` comment
    inside the job's source block (key line or body) — never in a string value."""
    return any(
        _ALLOW_WITH_REASON.search("#" + line.split("#", 1)[1])
        for line in block_text.splitlines()
        if "#" in line
    )


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """Return (line, message) for every job missing timeout-minutes.

    A reusable-workflow call (`uses:`) job is exempt. A file that cannot be parsed
    as YAML is itself reported as a violation (line ``None``) rather than silently
    passed as clean — matching the sibling workflow lints (check_concurrency &c.)."""
    text = path.read_text()
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify "
                "timeout-minutes on jobs — fix the syntax (or run actionlint) and "
                "re-check.",
            )
        ]
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    violations: list[tuple[int | None, str]] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        if "uses" in cfg:  # reusable-workflow call cannot carry timeout-minutes
            continue
        if "timeout-minutes" in cfg:
            continue
        block = blocks.get(str(name))
        if block and _block_opted_out(block[1]):
            continue
        line = block[0] if block else 1
        violations.append((line, _MESSAGE.format(name=name)))
    return violations


def main() -> int:
    files = sorted(WORKFLOWS_DIR.glob("*.yaml")) + sorted(WORKFLOWS_DIR.glob("*.yml"))
    total = 0
    for path in files:
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

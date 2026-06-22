"""Shared machinery for the line-oriented pre-commit lints under this directory.

The four ``check_{exit_suppression,stderr_suppression,pinned_downloads,
pinned_base_images}`` scripts each scan a list of paths given on argv, read
each file as UTF-8 (skipping anything unreadable), run a per-script detector over
the text, and print ``<path>:<lineno>: <message>`` to stderr for every hit —
returning 1 if any fired. Only the detector and the message differ; the read
loop, the skip-on-OSError/UnicodeDecodeError, the print loop, and the exit code
are identical, and live here.

The workflow lints (``check_pr_paths``, ``check_workflow_pipefail``,
``check_inline_run_length``, ``check_always_reporter``) share a byte-identical
``workflow_files()`` discovery glob; it lives here too.

Imported as a sibling: the scripts run as ``python3 hooks/check_*.py`` (or
``python -m hooks.check_*``), so each script prepends its own dir to ``sys.path``
before importing this module; the tests load each script by path.
"""

import re
import sys
from collections.abc import Callable
from pathlib import Path

# Lines whose first word only prints text — a command quoted inside them is an
# example or hint, not executed code. Shared by the stderr- and download-pinning
# checks; check_exit_suppression extends it (it also excuses status helpers).
MESSAGE_PREFIX = re.compile(r"^(?:echo|printf|warn|status|die|log|:)\b")


def run_line_checks(
    argv: list[str],
    find_violations: Callable[[str], list[int]],
    message: str,
) -> int:
    """Drive a line-oriented lint over ARGV.

    For each readable path, FIND_VIOLATIONS(text) returns the 1-based line numbers
    that violate. Each hit prints ``<path>:<lineno>: <message>`` to stderr; an
    unreadable path (OSError / UnicodeDecodeError) is skipped. Returns 1 if any
    path produced a hit, else 0.
    """
    status = 0
    for path in argv:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno in find_violations(text):
            print(f"{path}:{lineno}: {message}", file=sys.stderr)
            status = 1
    return status


def workflow_files(workflows_dir: Path, actions_dir: Path) -> list[Path]:
    """Every workflow file plus every composite-action definition, path-sorted.

    The dirs are passed in (not read from this module) so a consumer's tests can
    monkeypatch its own ``WORKFLOWS_DIR`` / ``ACTIONS_DIR`` constants and still
    redirect discovery.
    """
    files = list(workflows_dir.glob("*.yaml")) + list(workflows_dir.glob("*.yml"))
    if actions_dir.exists():
        files += actions_dir.rglob("action.yaml")
        files += actions_dir.rglob("action.yml")
    return sorted(files)

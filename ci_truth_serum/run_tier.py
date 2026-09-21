#!/usr/bin/env python3
"""
Run every check in a ci-truth-serum tier under a single hook id.

Consumers enable one aggregate — ``check-tier1`` / ``check-tier2`` /
``check-extras`` — instead of listing each lint, so a check added to that tier
later is picked up with no change to the consumer's ``.pre-commit-config.yaml``.

Each member runs exactly as its standalone hook would: the workflow lints
self-discover ``.github/{workflows,actions}`` (the passed file list is ignored),
and the content lints receive only the committed files of their kind (shell /
python / Dockerfile), classified with ``identify`` — the same library pre-commit
uses for its own ``types:`` filtering.

A content lint with no file of its kind cannot run, and the run says so on
stderr, naming each one. Pre-commit always passes the changed files, so this
reports on a commit that touches only one language. It also catches the hand
run: ``run_tier 1`` with no arguments still runs every workflow lint and exits
0, which without the note reads as a clean tier rather than a partial one.

A member that takes flags gets them from ``--check-arg <check>=<flag>``, so a
parameterised check stays inside its tier instead of being skipped and re-listed
as its own hook. Repeat the flag to pass several; ``--wrapper=retry_cmd`` is one
token, so a flag and its value need one ``--check-arg``.

Three hooks are intentionally NOT aggregated, each enabled on its own:
``check-absolute-symlinks`` is a ``language: script`` shell hook, not a Python
module, so it cannot run inside this Python aggregate; ``check-lockstep-pins``
and ``check-env-symmetry`` scan the whole tree and take no file list, and every
value they compare is per-repo, so a tier that carried them would carry one
repo's configuration for every consumer. The contract test in
``tests/cts/test_run_tier.py`` asserts the registry stays in sync with
``.pre-commit-hooks.yaml`` so a newly added hook can't silently escape its tier.

The registry itself is ``ci_truth_serum/_cts_registry.py``, which also carries each
check's tags. ``run_selection`` runs a selection over those tags.
"""

import importlib
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path

from identify import identify

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_registry import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    COMMENTED_CODE,
    DOCKERFILE,
    DRIFT,
    JS,
    JS_OR_PYTHON,
    MARKDOWN,
    PROSE_OR_COMMENTED_CODE,
    PYTHON,
    REFERENCING_TEXT,
    SHELL,
    SHELL_OR_DOCKERFILE,
    SHELL_OR_WORKFLOW_YAML,
    PER_FILE,
    SHELL_PYTHON_OR_WORKFLOW_YAML,
    TIERS,
    WORKFLOW,
)

# The file classes whose `#`/`//` comments the comment lints can read, and the
# prose classes scanned line-by-line.
_COMMENT_TAGS = frozenset({"shell", "python", "javascript", "ts"})
_PROSE_TAGS = frozenset({"markdown", "rst"})

# The workflow/composite-action files a SHELL_OR_WORKFLOW_YAML lint scans for
# inline `run:` blocks (matching the standalone hook's own path routing).
_WORKFLOW_YAML = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")
# check_conclusion_coverage's repository override. It is not a consumer, but a
# commit that widens the declared set changes no consumer at all, so the check
# has to SEE the file to know it must re-verify the tree.
_CONCLUSION_CONFIG = re.compile(r"(?:^|/)\.github/conclusion-coverage\.ya?ml$")


def matches(path: str, kind: str) -> bool:
    """True if PATH is a file of the class a KIND-selector content lint wants."""
    tags = identify.tags_from_path(path)
    if kind == SHELL:
        return "shell" in tags
    if kind == PYTHON:
        return "python" in tags
    if kind == DOCKERFILE:
        return "dockerfile" in tags
    if kind == SHELL_OR_DOCKERFILE:
        return "shell" in tags or "dockerfile" in tags
    if kind == SHELL_OR_WORKFLOW_YAML:
        return "shell" in tags or bool(
            "yaml" in tags and _WORKFLOW_YAML.search(path.replace("\\", "/"))
        )
    if kind == MARKDOWN:
        return "markdown" in tags
    if kind == COMMENTED_CODE:
        return bool(tags & _COMMENT_TAGS)
    if kind == PROSE_OR_COMMENTED_CODE:
        return bool(tags & (_COMMENT_TAGS | _PROSE_TAGS))
    if kind == REFERENCING_TEXT:
        return bool(tags & (_COMMENT_TAGS | _PROSE_TAGS | {"yaml"}))
    if kind == SHELL_PYTHON_OR_WORKFLOW_YAML:
        normalized = path.replace("\\", "/")
        return bool(tags & {"shell", "python"}) or bool(
            "yaml" in tags
            and (
                _WORKFLOW_YAML.search(normalized)
                or _CONCLUSION_CONFIG.search(normalized)
            )
        )
    if kind == JS_OR_PYTHON:
        return bool(tags & {"python", "javascript", "jsx", "ts", "tsx"})
    if kind == JS:
        return bool(tags & {"javascript", "jsx", "ts", "tsx"})
    if kind == DRIFT:
        return bool(tags & {"python", "javascript", "ts", "shell"})
    return False


def selected_files(kind: str, files: list[str]) -> list[str] | None:
    """The file arguments a KIND member receives, or None when it cannot run.

    A workflow lint self-discovers `.github/*` and ignores FILES, so it always
    runs and takes no arguments. A content lint reads only what it is given, so
    with no committed file of its kind it has nothing to scan. The caller must be
    able to tell that case from a pass — they are the same exit code, and a run
    that reports one as the other is the false green this pack exists to refuse.
    """
    if kind == WORKFLOW:
        return []
    return [f for f in files if matches(f, kind)] or None


def run_check(module: str, argv: list[str]) -> int:
    """Run one member check as its own subprocess; return its exit code."""
    return subprocess.run(
        [sys.executable, "-m", f"ci_truth_serum.{module}", *argv], check=False
    ).returncode


def run_in_process(module: str, argv: list[str]) -> int:
    """Run one member inside THIS interpreter; return its exit code.

    This is what makes a shared parse possible. `run_check` gives each member
    its own interpreter, so nothing a member parsed can reach the next one. The
    per-file pass below calls every member of a file in one process, where
    `_cts_py_ast.parse_whole` and `_cts_bash_ast.parse` hand the second member
    the tree the first one paid for.

    The subprocess also gave failure isolation for free: a member that raised
    printed its traceback and left the others to run. Keeping that is the one
    recovery this except clause performs, and it re-raises nothing only because
    the caller turns the status into a failed run. The traceback still prints,
    so a raising member is as loud as it was.
    """
    check = importlib.import_module(f"ci_truth_serum.{module}")
    try:
        return check.main(argv)
    except SystemExit as stop:
        return 0 if stop.code in (0, None) else 1
    except Exception:  # pylint: disable=broad-except
        traceback.print_exc()
        return 1


def run_per_file(
    members: list[tuple[str, str]],
    files: list[str],
    extra: dict[str, list[str]],
    seconds: dict[str, float],
) -> int:
    """Run each per-file MEMBER over FILES, one FILE at a time; return the status.

    The loop is file-outer on purpose. Member-outer, a member finishes the whole
    tree before the next one starts, so a cache would have to hold every file's
    tree at once — 1292 MB of them on a consumer with 2516 Python files. File-
    outer, every member sees one file while it is the current one, so a cache of
    a single entry is enough and the memory stays flat.

    Only a member the registry marks `per_file` may come through here. See
    `Check.per_file`.
    """
    rc = 0
    for path in files:
        for module, kind in members:
            if not matches(path, kind):
                continue
            started = time.monotonic()
            failed = run_in_process(module, [*extra.get(module, []), path])
            seconds[module] = seconds.get(module, 0.0) + time.monotonic() - started
            if failed:
                rc = 1
    return rc


def run_members(
    members: list[tuple[str, str]],
    files: list[str],
    extra: dict[str, list[str]] | None = None,
) -> tuple[int, list[str], dict[str, float]]:
    """Run each (module, kind) member over FILES; return the exit code, the
    members that had no file of their kind to scan, and each member's seconds.

    EXTRA maps a module to the flags `--check-arg` gave it. They precede the
    file arguments, which is where argparse wants them.

    The second value is what separates a member that passed from one that never
    ran: both leave exit code 0, and a caller that reports one as the other is
    the false green this pack exists to refuse.

    The third value is what names the member to cut. This runner is the whole
    cost of the job that runs it, and that job is capped, so a reader who cannot
    attribute the seconds re-measures them by hand against a consumer tree.
    `report_seconds` prints them.
    """
    extra = extra or {}
    rc = 0
    unscanned: list[str] = []
    seconds: dict[str, float] = {}
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"

    # A member with no file of its kind is unscanned whichever pass would have
    # run it, so the two passes are split only after that question is answered.
    scannable: list[tuple[str, str]] = []
    for module, kind in members:
        if selected_files(kind, files) is None:
            unscanned.append(module)
        else:
            scannable.append((module, kind))

    per_file = [(m, k) for m, k in scannable if m in PER_FILE]
    whole_list = [(m, k) for m, k in scannable if m not in PER_FILE]

    for module, kind in whole_list:
        # Actions cancels the job at its cap mid-member, and a cancelled run
        # reaches no line below this loop. This marker is what names the member
        # that was running when it died.
        if in_actions:
            print(f"--- run_tier: starting {module}", file=sys.stderr, flush=True)
        started = time.monotonic()
        failed = run_check(
            module, [*extra.get(module, []), *selected_files(kind, files)]
        )
        seconds[module] = time.monotonic() - started
        if failed:
            rc = 1

    if per_file:
        if in_actions:
            names = ", ".join(module for module, _ in per_file)
            print(
                f"--- run_tier: starting per-file pass ({names})",
                file=sys.stderr,
                flush=True,
            )
        if run_per_file(per_file, files, extra, seconds):
            rc = 1
    return rc, unscanned, seconds


def report_seconds(seconds: dict[str, float], subject: str) -> None:
    """Name each member's wall clock, slowest first, on stderr.

    Only under Actions, where the job log is where this is read. A hand run
    reads its own wall clock.
    """
    if not seconds or os.environ.get("GITHUB_ACTIONS") != "true":
        return
    print(f"--- {subject} seconds, slowest first", file=sys.stderr)
    for module, took in sorted(seconds.items(), key=lambda row: -row[1]):
        print(f"{took:8.1f}  {module}", file=sys.stderr)


def report_unscanned(
    unscanned: list[str], files: list[str], subject: str, rerun: str
) -> None:
    """Name the members that had no file of their kind, on stderr.

    A member that never ran and a member that passed leave the same exit code,
    so a run with no file arguments reads as clean while every content lint sat
    out. SUBJECT names the set ("tier 1 checks"), RERUN is the command that
    scans the whole tree — the remedy only an empty file list has, because a
    caller who DID pass files has already scanned what they hold.
    """
    if not unscanned:
        return
    print(
        f"note: these {subject} did not run, because no file of their kind was "
        f"passed: {', '.join(unscanned)}",
        file=sys.stderr,
    )
    if not files:
        print(
            f"  to scan the whole tree: git ls-files -z | xargs -0 {rerun}",
            file=sys.stderr,
        )


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] not in TIERS:
        print(
            f"usage: run_tier <{'|'.join(TIERS)}> [--skip <check>]... "
            "[--check-arg <check>=<flag>]... [files...]",
            file=sys.stderr,
        )
        raise SystemExit(2)
    tier, rest = argv[0], argv[1:]

    skips: set[str] = set()
    extra: dict[str, list[str]] = {}
    files: list[str] = []
    i = 0
    while i < len(rest):
        if rest[i] == "--skip":
            if i + 1 >= len(rest):
                print("error: --skip requires an argument", file=sys.stderr)
                raise SystemExit(2)
            skips.add(rest[i + 1])
            i += 2
        elif rest[i] == "--check-arg":
            if i + 1 >= len(rest):
                print("error: --check-arg requires an argument", file=sys.stderr)
                raise SystemExit(2)
            module, sep, flag = rest[i + 1].partition("=")
            if not sep or not module or not flag:
                print(
                    f"error: --check-arg takes <check>=<flag>, got {rest[i + 1]!r}",
                    file=sys.stderr,
                )
                raise SystemExit(2)
            extra.setdefault(module, []).append(flag)
            i += 2
        elif rest[i].startswith("--"):
            # A misspelled flag must not become a filename. `--skp <name>` would
            # otherwise pass two paths no member matches, and the tier would run
            # with the check the caller meant to drop still in it.
            print(f"error: unknown option {rest[i]!r}", file=sys.stderr)
            raise SystemExit(2)
        else:
            files.append(rest[i])
            i += 1

    members_of_tier = {mod for mod, _ in TIERS[tier]}
    unknown = (skips | set(extra)) - members_of_tier
    if unknown:
        print(
            f"error: unknown check(s) for tier {tier!r}: {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )
        print(
            f"  valid: {', '.join(mod for mod, _ in TIERS[tier])}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    # A check both skipped and configured is a contradiction: the flags would
    # silently do nothing, and the caller believes they configured the check.
    contradictory = skips & set(extra)
    if contradictory:
        print(
            "error: --check-arg names a check that --skip removes: "
            f"{', '.join(sorted(contradictory))}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    members = [(m, k) for m, k in TIERS[tier] if m not in skips]
    rc, unscanned, seconds = run_members(members, files, extra)
    report_seconds(seconds, f"tier {tier} member")
    report_unscanned(
        unscanned,
        files,
        f"tier {tier} checks",
        f"python -m ci_truth_serum.run_tier {tier}",
    )
    if rc != 0:
        raise SystemExit(rc)


if __name__ == "__main__":
    main()

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
``workflow_files()`` discovery glob; it lives here too. The two
required-check-shape probes (``has_decide_gate``, ``has_always_reporter``) are
shared by ``check_always_reporter`` and ``check_concurrency`` and live here too.

Imported as a sibling: the scripts run as ``python3 hooks/check_*.py`` (or
``python -m hooks.check_*``), so each script prepends its own dir to ``sys.path``
before importing this module; the tests load each script by path.
"""

import itertools
import re
import sys
from collections.abc import Callable
from pathlib import Path

import yaml

# Lines whose first word only prints text — a command quoted inside them is an
# example or hint, not executed code. Shared by the stderr- and download-pinning
# checks; check_exit_suppression extends it (it also excuses status helpers).
MESSAGE_PREFIX = re.compile(r"^(?:echo|printf|warn|status|die|log|:)\b")

# The two extensions a GitHub workflow file may carry. One SSOT so the reporter
# lint's discovery (`workflow_files`) and the apply step's (`desired_contexts`)
# can never diverge on which files they read.
WORKFLOW_GLOBS = ("*.yaml", "*.yml")

# `# required-check: true` on a job key line or one of its direct-child lines —
# the SSOT marker both check_required_reporter (which *requires* every always()
# reporter to be classified) and the sync_required_checks apply step (which reads
# the marker from ANY job) consume from the same scoped lines.
REQUIRED_MARKER = re.compile(r"#\s*required-check\s*:\s*true\b")
# A `${{ matrix.KEY }}` reference inside a job `name:`.
MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.(?P<key>[A-Za-z_][\w-]*)\s*\}\}")


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
    files = [p for glob in WORKFLOW_GLOBS for p in workflows_dir.glob(glob)]
    if actions_dir.exists():
        files += actions_dir.rglob("action.yaml")
        files += actions_dir.rglob("action.yml")
    return sorted(files)


def has_decide_gate(jobs: dict) -> bool:
    """True if any job uses decide-reusable.yaml or conditions on needs.decide.outputs.*"""
    for job_cfg in jobs.values():
        if not isinstance(job_cfg, dict):
            continue
        if "decide-reusable.yaml" in str(job_cfg.get("uses", "")):
            return True
        if "needs.decide.outputs" in str(job_cfg.get("if", "")):
            return True
    return False


def has_always_reporter(jobs: dict) -> bool:
    """True if any job has `if: always()` — the required-check reporter shape."""
    return any(
        isinstance(job_cfg, dict) and str(job_cfg.get("if", "")) == "always()"
        for job_cfg in jobs.values()
    )


def _job_blocks(text: str) -> dict[str, tuple[int, str]]:
    """Map each top-level job name to (1-based key line, its source block).

    A block is the job's key line plus every following body line indented deeper
    than the key — it stops at the next line dedented to the job-key indent or
    shallower (a sibling job, an inter-job comment, or the end of `jobs:`). Blank
    lines never terminate a block. Comments thus count as classification only
    when trailing the key line or living inside the indented body.

    Shared by the required-check lint and the apply step so both read the marker
    from byte-identical scoping; the comment-scope semantics are why a bespoke
    line scanner is used over a YAML parser (PyYAML discards comments).
    """
    lines = text.splitlines()
    jobs_idx = next(
        (i for i, line in enumerate(lines) if re.match(r"^jobs\s*:", line)), None
    )
    if jobs_idx is None:
        return {}

    job_indent = next(
        (
            len(line) - len(line.lstrip())
            for line in lines[jobs_idx + 1 :]
            if line.strip() and not line.lstrip().startswith("#")
        ),
        None,
    )
    if job_indent is None:
        return {}

    blocks: dict[str, tuple[int, str]] = {}
    key = re.compile(rf"^\s{{{job_indent}}}([^\s:#][^:]*?)\s*:")
    i = jobs_idx + 1
    while i < len(lines):
        stripped = lines[i].strip()
        indent = len(lines[i]) - len(lines[i].lstrip())
        if stripped and not stripped.startswith("#") and indent < job_indent:
            break
        match = key.match(lines[i])
        if not (match and indent == job_indent and not stripped.startswith("#")):
            i += 1
            continue
        end = i + 1
        while end < len(lines):
            body = lines[end]
            if body.strip() and len(body) - len(body.lstrip()) <= job_indent:
                break
            end += 1
        name = match.group(1).strip("'\"")  # align with PyYAML's unquoted key
        blocks[name] = (i + 1, "\n".join(lines[i:end]))
        i = end
    return blocks


def _classification_text(block: str) -> str:
    """The lines of a job block where a classification comment may live: the key
    line plus the job's direct-child lines (a trailing comment on a child, or a
    standalone comment at the child indent). Deeper step/run content is excluded
    so a `# required-check:` string buried in a step can't pass as a classification.
    """
    lines = block.splitlines()
    if not lines:
        return ""
    child_indent = next(
        (len(ln) - len(ln.lstrip()) for ln in lines[1:] if ln.strip()), None
    )
    eligible = [lines[0]]
    if child_indent is not None:
        eligible += [
            ln
            for ln in lines[1:]
            if ln.strip() and len(ln) - len(ln.lstrip()) == child_indent
        ]
    return "\n".join(eligible)


def matrix_combinations(matrix: dict) -> list[dict]:
    """Expand a job's `strategy.matrix` into the list of variable combinations
    GitHub schedules — the Cartesian product of the axis lists, then `exclude`
    removed and `include` entries extended-or-appended."""
    axes = {
        k: v
        for k, v in matrix.items()
        if k not in ("include", "exclude") and isinstance(v, list)
    }
    if axes:
        names = list(axes)
        combos = [
            dict(zip(names, vals, strict=True))
            for vals in itertools.product(*axes.values())
        ]
    else:
        combos = [{}]

    for ex in matrix.get("exclude", []) or []:
        combos = [c for c in combos if not all(c.get(k) == v for k, v in ex.items())]

    includes = matrix.get("include", []) or []
    if not axes:
        # No base matrix: each include entry is its own job (a bare matrix with
        # only `include` schedules exactly those entries).
        return [dict(inc) for inc in includes] if includes else combos

    for inc in includes:
        extendable = [
            c for c in combos if all(c.get(k) == v for k, v in inc.items() if k in axes)
        ]
        if extendable:
            for c in extendable:
                c.update(inc)
        else:
            combos.append(dict(inc))
    return combos


def expand_name(name: str, matrix: dict) -> list[str]:
    """Resolve a job's `name:` into every concrete check context it produces,
    substituting `${{ matrix.X }}` across the job's matrix."""
    refs = set(MATRIX_REF.findall(name))
    if not refs:
        return [name]

    resolved = []
    for combo in matrix_combinations(matrix):
        if not refs <= combo.keys():
            continue
        resolved.append(MATRIX_REF.sub(lambda m, c=combo: str(c[m.group("key")]), name))
    return sorted(set(resolved))


def required_check_contexts(text: str) -> list[str]:
    """Every required-check context declared by one workflow's source.

    Scans EVERY job (not only `always()` reporters) for a `# required-check: true`
    marker on its key/direct-child line, then expands each such job's `name:`
    across its own `strategy.matrix` into concrete check contexts. This is the set
    a branch-protection ruleset must require; the reporter lint enforces the
    stricter obligation that reporters be classified, a superset of what is read
    here (a cheap always-run linter carries the marker but is no reporter).
    """
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs", {})
    if not isinstance(jobs, dict):
        return []

    blocks = _job_blocks(text)
    contexts: list[str] = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict):
            continue
        block = blocks.get(name, (0, ""))[1]
        if not REQUIRED_MARKER.search(_classification_text(block)):
            continue
        matrix = (cfg.get("strategy") or {}).get("matrix") or {}
        contexts += expand_name(str(cfg.get("name", name)), matrix)
    return contexts

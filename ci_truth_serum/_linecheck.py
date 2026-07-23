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

Imported as a sibling: the scripts run as ``python3 ci_truth_serum/check_*.py`` (or
``python -m ci_truth_serum.check_*``), so each script prepends its own dir to ``sys.path``
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


class LineLoader(yaml.SafeLoader):
    """SafeLoader that tags every mapping with `__line__` (the 1-based source line
    of its first key) so a flagged step can be reported with a navigable
    file/line annotation instead of a bare, unclickable `::error::`. Shared by the
    workflow lints that want line-anchored findings (check_inline_run_length,
    check_externalized_markers)."""


def _mapping_with_line(loader: LineLoader, node: yaml.MappingNode) -> dict:
    mapping = loader.construct_mapping(node, deep=True)
    mapping["__line__"] = node.start_mark.line + 1
    return mapping


LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_with_line
)

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

# A whole-value `${{ … }}` expression wrapper around a job `if:`. GitHub evaluates
# `if: always()` and `if: ${{ always() }}` identically, so the reporter probe must
# see through the wrapper. Only a wrapper spanning the ENTIRE value is stripped — a
# compound like `always() && cond` (wrapped or not) is left intact so it stays a
# non-reporter (it does not unconditionally run).
_IF_WRAPPER = re.compile(r"^\$\{\{\s*(?P<inner>.*?)\s*\}\}$")


# A comment introducer: `#` (shell/YAML/Python), `<!--` (Markdown/HTML), or `//`
# (JS/TS). An annotation token counts only AFTER one of these on its line, so a
# token smuggled into live data (a `group: "<token>"` string value, a printed
# message, a URL fragment) can never silently disable a lint — that would be a
# fail-open. One SSOT for every annotation-matching hook in this package.
_COMMENT_INTRO = r"(?:#|<!--|//)"


def annotation_re(token: str, require_reason: bool = True) -> "re.Pattern[str]":
    """The compiled matcher for an opt-out/annotation TOKEN on one line.

    Comment-scoped: the token must follow a comment introducer. With
    REQUIRE_REASON (the default), the token must also carry `: <non-empty
    reason>` — a bare marker states nothing and does not suppress. Every hook
    that recognizes a per-line annotation builds its matcher here; the
    meta-test in tests/cts/test_annotation_predicates.py bans the bare
    `token in line` substring predicate this replaces."""
    tail = r":\s*\S" if require_reason else r"\b"
    return re.compile(rf"{_COMMENT_INTRO}[^\n]*\b{re.escape(token)}{tail}")


def annotated(line: str, token: str, require_reason: bool = True) -> bool:
    """True when LINE carries the comment-scoped annotation TOKEN (see
    ``annotation_re``)."""
    return bool(annotation_re(token, require_reason).search(line))


# A line that ends in a backslash, a pipe, or a boolean operator is continued on
# the next line by the shell — join them so a command (and its `$(…)` / redirects)
# spanning lines is analyzed whole, not mis-split mid-capture.
_CONTINUES = re.compile(r"(?:\\|\||&&)\s*$")

# The only tokens that affect substitution nesting: an escaped char (`\x`, inert —
# so `\`` is a literal backtick and `\$` never opens `$(`), an opening `$(` / `<(`,
# a closing `)`, or a bare backtick. Walking these instead of indexing characters
# keeps `inside_substitution` a plain fold with no manual offset bookkeeping.
_SUBST_TOKEN = re.compile(r"\\.|\$\(|<\(|`|\)")


def inside_substitution(prefix: str) -> bool:
    """True if PREFIX has an unclosed ``$(`` / ``<(`` / backtick — i.e. text after
    it is still inside a command substitution (so the line continues, or a
    ``|| true`` after it is a value capture)."""
    depth = 0
    backtick = False
    for token in _SUBST_TOKEN.finditer(prefix):
        tok = token.group()
        if tok[0] == "\\":
            continue  # escaped character — inert
        if tok in ("$(", "<("):
            depth += 1
        elif tok == ")" and depth:
            depth -= 1
        elif tok == "`":
            backtick = not backtick
    return depth > 0 or backtick


def logical_lines(text: str) -> list[tuple[int, str]]:
    """Join continued lines into one logical line, tagged with the 1-based
    physical line number where it STARTS.

    A line continues when it ends in ``\\`` / ``|`` / ``&&`` (shell line
    continuation) OR when a command substitution it opened (``$(`` / ``<(`` /
    backtick) is still unclosed. This is the ONE joiner every line-oriented shell
    lint in this package scans through, so a construct wrapped across physical
    lines cannot evade any of them; the meta-test in
    tests/cts/test_shell_hook_traversal.py holds each shell lint to it (or to the
    full ``_bash_ast`` grammar)."""
    out: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for lineno, raw in enumerate(text.splitlines(), 1):
        if not pending:
            start = lineno
        joined = raw[:-1] if raw.endswith("\\") else raw
        if _CONTINUES.search(raw) or inside_substitution(pending + joined):
            pending += joined + " "
            continue
        out.append((start, pending + raw))
        pending = ""
    if pending:
        out.append((start, pending))
    return out


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

    This skip is a deliberate, narrow recovery action, not a silent-pass-on-bad-
    input escape hatch: ARGV here is pre-commit's own file list, already filtered
    to committed files of the right type (shell/python/Dockerfile) via ``identify``
    before this ever runs, so a read failure means the path vanished (a rename/
    delete race) or was mis-tagged as text (stray binary bytes) — not that this
    lint is blessing bad shell/Python/Dockerfile content as clean. That's the
    opposite of the YAML workflow lints (``check_workflow_pipefail`` &c.), whose
    one argument *is* the exact artifact under test: an unparseable workflow
    there is reported as a violation, since "no findings" would be a false-green
    on the very file being verified.
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


def is_always_reporter(if_value: object) -> bool:
    """True if a job `if:` value is an unconditional always() reporter.

    Accepts bare `always()` and the semantically identical `${{ always() }}`
    wrapper (any inner spacing). A compound condition such as `always() && cond`
    is intentionally rejected: it does not always run, so it is no reporter.
    """
    text = str(if_value).strip()
    wrapped = _IF_WRAPPER.match(text)
    if wrapped:
        text = wrapped.group("inner").strip()
    return text == "always()"


def has_always_reporter(jobs: dict) -> bool:
    """True if any job has an always() reporter `if:` — the required-check shape."""
    return any(
        isinstance(job_cfg, dict) and is_always_reporter(job_cfg.get("if", ""))
        for job_cfg in jobs.values()
    )


# A concurrency group keyed by any of these is per-ref / per-PR / per-run, so a
# run is only ever superseded by a *newer run of the same ref* — whose own
# reporter then posts the check. Without one of these the group is static and a
# sibling ref's run can cancel this one with no replacement report. Shared by
# the concurrency lints (check_static_concurrency, which flags a static group on
# the decide+always() shape, and check_cancellable_required_check, which flags a
# static *cancellable* group on any required-check-marked workflow) so the
# per-ref definition is one SSOT, not two copies that could drift.
PER_REF_CONCURRENCY_KEYS = (
    "github.ref",
    "github.ref_name",
    "github.head_ref",
    "github.run_id",
    "github.run_number",
    "pull_request.number",
    "github.event.number",
)

# A `${{ … }}` expression span. Non-greedy: each span ends at its own `}}`.
_EXPR_SPAN = re.compile(r"\$\{\{(?P<expr>.*?)\}\}", re.DOTALL)


def group_is_per_ref(group: str) -> bool:
    """True if a concurrency `group:` expression carries a per-ref/per-PR/per-run
    key INSIDE a `${{ … }}` expression span — meaning a superseding run is always
    the same ref's newer run, which re-reports, so the group cannot strand a
    required check. Outside a span the key is a literal: a group named
    `"github.ref-shared"` is one static string for every ref, so a bare
    substring match would fail open exactly on the workflows this guard exists
    to flag."""
    return any(
        key in span.group("expr")
        for span in _EXPR_SPAN.finditer(group)
        for key in PER_REF_CONCURRENCY_KEYS
    )


def opted_out(text: str, token: str) -> bool:
    """True only when the opt-out TOKEN appears inside an actual `#` comment, not
    anywhere in the byte stream — a `group: "<token>"` string value must not
    silently disable a lint (that would be a fail-open). Shared by the
    concurrency lints, each of which passes its own token."""
    return any(
        token in line.split("#", 1)[1] for line in text.splitlines() if "#" in line
    )


def concurrency_line(text: str) -> int:
    """Return the 1-based line number of the top-level `concurrency:` key, or 1
    when the text has none (the fallback anchor). Shared by the concurrency
    lints so their `::error line=` annotations agree byte-for-byte."""
    for num, line in enumerate(text.splitlines(), 1):
        if re.match(r"^concurrency\s*:", line):
            return num
    return 1


def job_concurrency_line(block: tuple[int, str] | None, fallback: int) -> int:
    """The 1-based line of a job's `concurrency:` key within its source BLOCK
    (from `_job_blocks`), else FALLBACK. Scoping the scan to the job's own block
    anchors the annotation on the offending job, not a sibling's block."""
    if block is None:
        return fallback
    start, body = block
    for offset, line in enumerate(body.splitlines()):
        if re.match(r"^\s+concurrency\s*:", line):
            return start + offset
    return fallback


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

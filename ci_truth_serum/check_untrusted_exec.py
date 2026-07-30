#!/usr/bin/env python3
"""
Flag a job that EXECUTES code resolved from an untrusted checkout while secrets
are live in that job.

``check_trusted_base`` reports the *shape* — privilege sitting in a job that
checked out the PR head. This lint reports the *evidence*: the specific step
whose executed bytes come out of that checkout. The two cover different halves,
so a job this lint would report is skipped when ``check_trusted_base`` already
reports it (no duplicate finding), and reported when it does not — including
when the file carries a ``# trusted-base-ok`` opt-out, because the reason that
opt-out is usually given ("it only runs the base branch's trusted copy") is
exactly the claim this lint exists to test.

THE THREE EXECUTION FORMS, each resolved from ``$GITHUB_WORKSPACE`` — i.e. from
the checked-out pull-request head, which the PR author rewrites at will:

  1. ``uses: ./path`` — GitHub reads a LOCAL composite action's manifest out of
     the workspace. The PR rewrites ``action.yml`` and gets arbitrary steps.
  2. a ``run:`` invoking a workspace-relative package-manager script —
     ``pnpm <script>`` / ``npm run <script>`` / ``yarn <script>`` /
     ``npx <bin>`` / ``make <target>``. The body comes from the checked-out
     ``package.json`` / ``Makefile``.
  3. a ``run:`` executing a workspace-relative path — ``bash ./scripts/x.sh``,
     ``node scripts/x.mjs``, ``./bin/x``, ``python scripts/x.py``.

THE UNIT OF ANALYSIS IS THE JOB, NOT THE STEP. One attacker-controlled step
compromises every later step in the same job: it can append to ``$GITHUB_ENV``
(``BASH_ENV``, ``NODE_OPTIONS``) and ``$GITHUB_PATH``, both of which GitHub
applies to all subsequent steps. So "the untrusted execution happens before the
secret-bearing step" is not a defence, and neither is staging a trusted copy of
a script into ``$RUNNER_TEMP`` and running that — same uid, same PATH, so an
earlier attacker step owns both. A ``$RUNNER_TEMP``-anchored ``run:`` is
therefore not *itself* counted as an execution form, but it is not a rescue
either: the job is still reported for whichever of the three forms it does have.

The only real fix is to move the credential out of the job that touches the
untrusted tree (a two-job split, as in a repair/land pair), or to stop executing
workspace-resolved code there at all.

An opt-out is a ``# untrusted-exec-ok: <reason>`` comment inside the offending
JOB's block; the reason is REQUIRED (a bare marker states nothing and does not
suppress). ``# trusted-base-ok`` deliberately does NOT suppress this lint.
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_trusted_base as _trusted_base  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    iter_nodes,
    parse as _parse_bash,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader as _LineLoader,
    annotation_re,
    _job_blocks,
    workflow_files as _workflow_files,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"

ALLOW = "untrusted-exec-ok"
_ALLOW_RE = annotation_re(ALLOW)

# A checkout `ref:` that resolves to the pull-request head. `matrix.*head*` covers
# a fan-out whose matrix entries were built from PR heads (`matrix.pr.head_ref`) —
# deliberately keyed on the word "head" so an ordinary axis (`matrix.os`) never
# matches.
PR_HEAD_REF = re.compile(
    r"github\.event\.pull_request\.head\.(?:sha|ref)"
    r"|github\.head_ref"
    r"|matrix\.[\w.]*head[\w.]*"
)
# The same head reached from a privileged follow-up workflow. Conditionally
# untrusted: see _TRIGGER_PINNED.
WORKFLOW_RUN_REF = re.compile(r"github\.event\.workflow_run\.head_(?:sha|branch)")
# A job `if:` that pins WHICH upstream run may reach this job — to a push, or to a
# branch literal. Such a run's head is a merged commit on a branch the repo
# controls, not a pull-request head, so the checkout is trusted after all. Only
# the workflow_run form can be rescued this way; a `pull_request.head` ref is
# untrusted no matter what gates the job.
_TRIGGER_PINNED = re.compile(r"workflow_run\.(?:head_branch|event)\s*==\s*['\"]")
_SECRET_REF = re.compile(r"secrets\.(?P<name>\w+)")
# The default GITHUB_TOKEN is only worth stealing when the job can write with it;
# a read-scoped one in a job env is noise, not a finding.
_DEFAULT_TOKEN = "GITHUB_TOKEN"

_CHECKOUT_ACTION = re.compile(r"^actions/checkout(?:[@/]|$)")

# Package managers whose subcommand runs a script body out of the workspace
# manifest. `npx`/`pnpx`/`bunx` take a bin name directly (resolved from the
# workspace's node_modules), so they have no subcommand layer.
_SCRIPT_RUNNERS = frozenset({"npm", "pnpm", "yarn", "bun"})
_BIN_RUNNERS = frozenset({"npx", "pnpx", "bunx"})
# Subcommands that fetch/inspect dependencies rather than run a manifest script.
# `install` DOES run the manifest's lifecycle scripts, but it is the single most
# common step in any CI job — flagging it would report the idiom, not the defect
# (see the module docstring's precision note); it is left uncovered on purpose.
_NON_SCRIPT_SUBCOMMANDS = frozenset(
    {
        "install",
        "i",
        "ci",
        "add",
        "remove",
        "rm",
        "uninstall",
        "update",
        "up",
        "upgrade",
        "audit",
        "list",
        "ls",
        "why",
        "outdated",
        "config",
        "store",
        "cache",
        "dlx",
        "create",
        "init",
        "publish",
        "version",
        "link",
        "unlink",
        "set",
        "get",
        "view",
        "info",
        "search",
        "pack",
        "prune",
        "dedupe",
        "fund",
        "docs",
        "repo",
        "whoami",
        "ping",
        "doctor",
        "explain",
        "help",
        "bin",
        "root",
        "prefix",
        "env",
    }
)
_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "ksh",
        "dash",
        "node",
        "deno",
        "bun",
        "python",
        "python3",
        "ruby",
        "perl",
        "tsx",
    }
)
# A filename that is executable content even without a directory component, so
# `bash build.sh` counts as a workspace path the same way `bash ci/build.sh` does.
_SCRIPT_SUFFIX = re.compile(r"\.(?:sh|bash|zsh|mjs|cjs|js|ts|py|rb|pl)$")


def _words(command) -> list[str]:
    """The literal source words of a tree-sitter ``command`` node, in order.

    Redirections and variable assignments are dropped, so ``FOO=1 bash x.sh``
    yields ``["bash", "x.sh"]`` — the caller always sees the real argv head."""
    out = []
    for child in command.children:
        if child.type in (
            "variable_assignment",
            "file_redirect",
            "herestring_redirect",
        ):
            continue
        out.append(child.text.decode("utf-8", "replace"))
    return out


def _is_workspace_path(word: str) -> bool:
    """True when WORD is a path GitHub resolves inside ``$GITHUB_WORKSPACE``.

    Anything anchored elsewhere is excluded: an absolute path, ``~``, and — the
    case that matters here — any word opening with an expansion (``$RUNNER_TEMP``,
    ``${{ steps.x.outputs.dir }}``), whose target this lint cannot know and must
    not guess at. Such a step is not counted as an execution form; it is also not
    a rescue, because a job reported for one of the other forms already owns it.
    """
    bare = unquote(word)
    if not bare or bare.startswith(("-", "/", "~", "$")):
        return False
    return "/" in bare or bool(_SCRIPT_SUFFIX.search(bare))


def _first_operand(words: list[str]) -> str:
    """The first non-flag word after the command name, or ``""``."""
    return next((w for w in words[1:] if not w.startswith("-")), "")


def _script_execution(words: list[str]) -> str:
    """The execution form WORDS is, as a short human label, or ``""`` for none.

    Covers forms 2 and 3 of the module docstring; form 1 (`uses: ./…`) is a YAML
    key, not a shell command, and is detected by the caller.
    """
    if not words:
        return ""
    name = unquote(words[0])
    if name == "make":
        target = _first_operand(words)
        return f"`make {target}`" if target else "`make`"
    if name in _BIN_RUNNERS:
        binary = _first_operand(words)
        return f"`{name} {binary}`" if binary else ""
    if name in _SCRIPT_RUNNERS:
        verb = _first_operand(words)
        operand = verb
        # `npm run build` and `pnpm build` are the same execution; keep the
        # source spelling in the label so the report points at a real line.
        if verb in ("run", "run-script", "exec"):
            operand = next(
                (w for w in words[words.index(verb) + 1 :] if not w.startswith("-")),
                "",
            )
        if not operand or operand in _NON_SCRIPT_SUBCOMMANDS:
            return ""
        spelled = f"{verb} {operand}" if verb != operand else operand
        return f"`{name} {spelled}`"
    if name in _INTERPRETERS:
        operand = _first_operand(words)
        return f"`{name} {operand}`" if _is_workspace_path(operand) else ""
    return f"`{name}`" if _is_workspace_path(name) else ""


def run_executions(script: str) -> list[str]:
    """Every attacker-controlled execution in one ``run:`` body, as labels.

    tree-sitter never raises on malformed shell (errors become ERROR nodes), so a
    ``run:`` this cannot parse simply yields nothing. The one loud case is
    ``PathologicalInputError``, which is re-raised rather than swallowed."""
    if not isinstance(script, str) or not script.strip():
        return []
    found = []
    for command in iter_nodes(_parse_bash(script), "command"):
        label = _script_execution(_words(command))
        if label and label not in found:
            found.append(label)
    return found


def step_executions(step: dict) -> list[str]:
    """Every attacker-controlled execution form in one step, as labels."""
    uses = str(step.get("uses", "")).strip()
    # GitHub resolves a `uses:` starting with `.` against $GITHUB_WORKSPACE — the
    # checked-out tree — so the PR supplies the action's manifest and its steps.
    if uses.startswith("./") or uses == ".":
        return [f"local composite action `uses: {uses}`"]
    return run_executions(step.get("run"))


def _steps(cfg: dict) -> list[dict]:
    steps = cfg.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def job_checks_out_untrusted(cfg: dict) -> bool:
    """True when a step in the job checks out attacker-controlled content — an
    ``actions/checkout`` whose ``ref`` interpolates a pull-request head, or an
    unpinned workflow_run head (see ``_TRIGGER_PINNED``).

    Only a named head CONTEXT counts. An opaque ref (``steps.x.outputs.head_sha``,
    ``needs.y.outputs.head``) is deliberately not guessed at: this lint trades
    that recall for the precision that keeps it default-on."""
    pinned = bool(_TRIGGER_PINNED.search(str(cfg.get("if", ""))))
    for step in _steps(cfg):
        uses = step.get("uses")
        with_block = step.get("with")
        if not isinstance(uses, str) or not _CHECKOUT_ACTION.match(uses.strip()):
            continue
        if not isinstance(with_block, dict):
            continue
        ref = str(with_block.get("ref", ""))
        if PR_HEAD_REF.search(ref) or (WORKFLOW_RUN_REF.search(ref) and not pinned):
            return True
    return False


def _secret_names(value: object) -> set[str]:
    """Every ``secrets.NAME`` referenced anywhere inside VALUE (recursively).

    Keys are scanned as well as values, so a mapping whose KEY carries the
    expression is not missed. ``__line__`` is the LineLoader's own annotation,
    not workflow content."""
    if isinstance(value, dict):
        parts = [
            _secret_names(part)
            for key, val in value.items()
            if key != "__line__"
            for part in (key, val)
        ]
    elif isinstance(value, list):
        parts = [_secret_names(item) for item in value]
    else:
        return {m.group("name") for m in _SECRET_REF.finditer(str(value))}
    return set().union(set(), *parts)


def live_secrets(doc: dict, cfg: dict) -> set[str]:
    """The secrets readable by code running in this job, by name.

    Workflow-level ``env`` is inherited; the job's own ``env`` and every step's
    ``env``/``with`` are in reach; ``secrets: inherit`` on a called workflow hands
    over the lot. The default ``GITHUB_TOKEN`` counts only when the job can write
    with it — a read-scoped one is not a credential worth stealing, and counting
    it would report every ordinary CI job.
    """
    names = _secret_names(doc.get("env"))
    names |= _secret_names(cfg.get("env"))
    for step in _steps(cfg):
        names |= _secret_names(step.get("env"))
        names |= _secret_names(step.get("with"))
    if str(cfg.get("secrets")) == "inherit":
        names.add("inherit (all repository secrets)")
    names |= _secret_names(cfg.get("secrets"))
    writes = _trusted_base._grants_write(doc.get("permissions")) or (
        _trusted_base._grants_write(cfg.get("permissions"))
    )
    if not writes:
        names.discard(_DEFAULT_TOKEN)
    return names


def _opted_out(block: str) -> bool:
    """True when the job's own source block carries a reason-bearing opt-out.

    Job-scoped, so an annotation on a sibling job can never suppress this one."""
    return bool(_ALLOW_RE.search(block))


def analyze(doc: object, already_reported: frozenset[str] = frozenset()) -> list[tuple]:
    """Every violating job as ``(job_name, first_step_line, forms, secrets)``.

    ALREADY_REPORTED names the jobs ``check_trusted_base`` reports for this file;
    they are skipped so one job never yields two findings for one hole."""
    if not isinstance(doc, dict):
        return []
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        return []
    violations = []
    for name, cfg in jobs.items():
        if not isinstance(cfg, dict) or str(name) in already_reported:
            continue
        if not job_checks_out_untrusted(cfg):
            continue
        secrets = live_secrets(doc, cfg)
        if not secrets:
            continue
        forms: list[str] = []
        line = None
        for step in _steps(cfg):
            labels = [lb for lb in step_executions(step) if lb not in forms]
            if labels and line is None:
                line = step.get("__line__")
            forms += labels
        if forms:
            violations.append((str(name), line, forms, sorted(secrets)))
    return violations


def _message(name: str, forms: list[str], secrets: list[str]) -> str:
    return (
        f"job '{name}' checks out untrusted (pull-request head) content AND "
        f"executes code resolved from that checkout — {', '.join(forms)} — while "
        f"these secrets are live in the job: {', '.join(secrets)}. The PR author "
        "rewrites those bytes, so they run with the credentials above; and because "
        "an attacker step can write $GITHUB_ENV/$GITHUB_PATH, every LATER step in "
        "the job is compromised too (staging a trusted copy under $RUNNER_TEMP "
        "does not rescue it — same uid, same PATH). Move the credential into a "
        "separate job that never touches the untrusted tree, or stop executing "
        f"workspace-resolved code here. Opt out with '# {ALLOW}: <reason>' in the "
        "job block."
    )


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH.

    An unparseable workflow is reported as a violation rather than passed clean:
    this file IS the artifact under test, so "no findings" on it would be a
    false green."""
    text = path.read_text()
    try:
        doc = yaml.load(text, Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify that "
                "untrusted checkouts are not executed with secrets in the job — "
                "fix the syntax (or run actionlint) and re-check.",
            )
        ]
    already = frozenset(_trusted_base.reported_job_names(doc, text))
    blocks = _job_blocks(text)
    out: list[tuple[int | None, str]] = []
    for name, line, forms, secrets in analyze(doc, already):
        block = blocks.get(name)
        if block and _opted_out(block[1]):
            continue
        out.append((line or (block[0] if block else 1), _message(name, forms, secrets)))
    return out


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

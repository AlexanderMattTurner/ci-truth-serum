#!/usr/bin/env python3
"""Ban exit-code-masking pipes in GitHub Actions steps whose shell lacks pipefail.

A pipeline's exit status is its LAST command's. `cmd | tee log` therefore exits
with tee's status (0 almost always), so a failing `cmd` becomes a silent success
and a required check reports green while broken. `set -o pipefail` makes the
pipeline exit non-zero if ANY stage fails, surfacing the failure.

GitHub already runs the DEFAULT `run:` shell as `bash --noprofile --norc -eo
pipefail {0}`, so an ordinary `run:` pipe is safe and is NOT flagged. The gap this
guards is the contexts that bypass that wrapper:

  * `runCmd:` (devcontainers/ci and friends) — executed inside the container, not
    through GitHub's pipefail-enabled shell, so a piped runCmd masks failures.
  * a `run:` step under an explicit non-pipefail shell — `shell: sh` (`sh -e {0}`,
    no pipefail) or a hand-rolled `shell: bash -e {0}` that drops `-o pipefail`,
    whether set on the step, the job's `defaults.run`, or the workflow's.

A script is SAFE when its effective shell already enables pipefail OR its executable
code runs `set -o pipefail` (an actual command — a mention in a comment or heredoc
body does not count). Quoted spans, comments, and heredoc bodies are ignored when
scanning for pipes: a `|` there is data, not a pipeline. Non-shell steps
(`shell: python`/`pwsh`/`node`/…) are skipped — their `|` is not a pipeline. Opt out
a deliberate step with a `# allow-no-pipefail: <reason>` comment in the script body.

Globs every workflow + composite action like check_pr_paths; argv is ignored.
"""

import os
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

# The workflow lints anchor discovery at the repo being scanned. pre-commit runs
# the hook from the consumer repo root, so cwd is that root; tests override these.
REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ACTIONS_DIR = REPO_ROOT / ".github" / "actions"
ALLOW = "allow-no-pipefail"

# A POSIX-shell pipe: a single `|` that is not part of `||` (logical or) and not a
# `>|` clobber redirect. `|&` (pipe + stderr) and an FD-glued `2>&1| tee` still
# count — the char before the `|` may be a digit (the FD), so it is NOT excluded.
_PIPE = re.compile(r"(?<![|>&])\|(?!\|)")
# An actual `set -o pipefail` command (any flag bundle that includes `o`, e.g.
# `-eo`/`-euo`), NOT a free "pipefail" substring in a comment and NOT `set +o`
# (which DISABLES it). Anchored so a comment mention can't whitelist the script.
_SET_PIPEFAIL = re.compile(r"\bset\s+-\w*o\w*\s+pipefail\b")
# A heredoc redirect; its body is DATA, not commands, so a `|` there is not a pipe.
# `_code_only` normalizes the delimiter, stripping its quotes (`<<'EOF'` -> `<<EOF`)
# so a quoted heredoc's body is dropped exactly like an unquoted one's.
_HEREDOC = re.compile(r"<<-?\s*(?P<term>\w+)")
# The heredoc INTRODUCER as written in source, recognized inside `_code_only` before
# quotes are stripped: `<<` / `<<-`, optional space, and a delimiter that may be
# bare or quoted (`<<'EOF'`, `<<"EOF"`). `(?P=q)` pins the close quote to the open.
_HEREDOC_INTRO = re.compile(r"<<(?P<dash>-?)\s*(?P<q>['\"]?)(?P<term>\w+)(?P=q)")
_SHELL_BASENAMES = {"bash", "sh", "dash", "zsh", "ksh"}


def _is_posix_shell(shell: str | None) -> bool:
    """True when the step's shell runs POSIX pipelines (so a `|` is a pipe). The
    GitHub default (shell unset) is bash; an explicit python/pwsh/node is not."""
    if shell is None:
        return True
    tok = shell.strip().split()
    if not tok:
        return True
    return os.path.basename(tok[0]) in _SHELL_BASENAMES


def _shell_has_pipefail(shell: str | None) -> bool:
    """True when the effective shell enables pipefail without the body asking. The
    GitHub default and a bare `shell: bash` both expand to `… -eo pipefail {0}`; any
    other invocation (e.g. `sh`, `bash -e {0}`) only counts if it spells pipefail."""
    if shell is None:
        return True
    s = shell.strip()
    return s == "bash" or "pipefail" in s


def _code_only(script: str) -> str:
    """SCRIPT with quoted spans and `#` comments removed, newlines preserved. Quote
    state is tracked ACROSS lines, so a `|` inside a multi-line "…" or '…' string is
    not mistaken for a pipe; a `#` comment runs only to its own end of line.

    A heredoc introducer (`<<'EOF'`, `<<"EOF"`, `<<-EOF`) is recognized when NOT
    inside a string/comment and emitted with its delimiter quotes stripped
    (`<<EOF`) — so the quotes never open a spurious string span that would swallow
    the delimiter, and `_executable_lines` can still find the terminator and drop
    the (quoted-heredoc) body."""
    out: list[str] = []
    i, n = 0, len(script)
    in_s = in_d = in_comment = False
    while i < n:
        ch = script[i]
        if ch == "\n":
            in_comment = False
            out.append(ch)
            i += 1
            continue
        if in_comment:
            i += 1
            continue
        if in_s:
            in_s = ch != "'"
            i += 1
            continue
        if in_d:
            in_d = ch != '"'
            i += 1
            continue
        intro = _HEREDOC_INTRO.match(script, i)
        if intro:
            out.append("<<" + intro.group("dash") + intro.group("term"))
            i = intro.end()
            continue
        if ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "#":
            in_comment = True
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _line_comment(line: str) -> str:
    """The `#`-comment text of LINE (everything after the first unquoted `#`), or
    "" if the line has none. Quote-aware so a `#` inside a string is not a comment."""
    in_s = in_d = False
    for idx, ch in enumerate(line):
        if in_s:
            in_s = ch != "'"
        elif in_d:
            in_d = ch != '"'
        elif ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "#":
            return line[idx + 1 :]
    return ""


def _allow_optout(script: str) -> bool:
    """True if the `# allow-no-pipefail` marker appears in a real `#` comment — not
    buried in a string, a piped-command's data, or a heredoc body (where a `#` is
    literal text, not a comment). Scans each source line's comment, skipping heredoc
    bodies via the same terminator tracking `_executable_lines` uses."""
    raw_lines = script.splitlines()
    code_lines = _code_only(script).splitlines()  # newline-preserving: indices align
    terminator = None
    for idx, code in enumerate(code_lines):
        if terminator is not None:
            if code.strip() == terminator:
                terminator = None
            continue
        if idx < len(raw_lines) and ALLOW in _line_comment(raw_lines[idx]):
            return True
        match = _HEREDOC.search(code)
        if match:
            terminator = match.group("term")
    return False


def _executable_lines(script: str) -> list[str]:
    """The lines of SCRIPT that are executed shell code: quoted spans and comments
    removed (`_code_only`), and heredoc BODIES dropped — their content is data, so a
    `|` (or a `set -o pipefail`) inside a `<<EOF … EOF` block is not shell code. The
    command line introducing the heredoc is kept (it may itself pipe)."""
    lines = []
    terminator = None
    for line in _code_only(script).splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        lines.append(line)
        match = _HEREDOC.search(line)
        if match:
            terminator = match.group("term")
    return lines


def _default_shell(*scopes: object) -> str | None:
    """First `defaults.run.shell` found walking the given scopes (job, then
    workflow); None if none set it. Tolerant of a null/non-mapping `defaults:`."""
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        run = scope.get("defaults")
        run = run.get("run") if isinstance(run, dict) else None
        shell = run.get("shell") if isinstance(run, dict) else None
        if isinstance(shell, str):
            return shell
    return None


def _check_script(script: str, shell: str | None, location: str) -> list[str]:
    """Return a one-element message list when SCRIPT pipes under a shell that lacks
    pipefail and neither opts out nor sets pipefail itself; else empty."""
    if not isinstance(script, str) or not _is_posix_shell(shell):
        return []
    if _shell_has_pipefail(shell) or _allow_optout(script):
        return []
    code = _executable_lines(script)
    if _SET_PIPEFAIL.search("\n".join(code)):
        return []
    pipes = [line for line in code if _PIPE.search(line)]
    if not pipes:
        return []
    shown = pipes[0].strip()
    return [
        f"{location}: pipes (`{shown}`) under a shell without pipefail, so a failure "
        "on the left of the pipe is masked by the last stage's exit status. Add "
        f"`set -o pipefail` to the script, use the default `run:` shell, or annotate "
        f"`# {ALLOW}: <reason>`."
    ]


def _iter_steps(
    steps: object, workflow: dict, job: object
) -> list[tuple[int | None, str, str | None, str]]:
    """Yield (line, script, effective_shell, kind) for every run/runCmd step in
    STEPS. LINE is the step's 1-based source line (None if the doc was parsed
    without line tags, e.g. a hand-built dict in a unit test)."""
    out: list[tuple[int | None, str, str | None, str]] = []
    if not isinstance(steps, list):
        return out
    for step in steps:
        if not isinstance(step, dict):
            continue
        line = step.get("__line__")
        with_ = step.get("with")
        if isinstance(with_, dict) and isinstance(with_.get("runCmd"), str):
            # runCmd bypasses GitHub's pipefail-enabled shell entirely.
            out.append((line, with_["runCmd"], "sh", "runCmd"))
        if isinstance(step.get("run"), str):
            shell = step.get("shell")
            if shell is None:
                shell = _default_shell(job, workflow)
            out.append((line, step["run"], shell, "run"))
    return out


def analyze(doc: object) -> list[tuple[int | None, str]]:
    """Every pipefail violation in a parsed workflow / composite-action document,
    as (line, message). LINE is the offending step's source line, or None."""
    if not isinstance(doc, dict):
        return []
    found: list[tuple[int | None, str]] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            for line, script, shell, kind in _iter_steps(job.get("steps"), doc, job):
                found += [
                    (line, msg)
                    for msg in _check_script(script, shell, f"job {job_id} ({kind})")
                ]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        for line, script, shell, kind in _iter_steps(runs.get("steps"), doc, runs):
            found += [
                (line, msg)
                for msg in _check_script(script, shell, f"composite action ({kind})")
            ]
    return found


def check_file(path: Path) -> list[tuple[int | None, str]]:
    """(line, message) for every violation in PATH. A file this lint cannot parse
    as YAML is itself reported as a violation (line ``None``) rather than
    silently passed as clean: this lint's job is asserting pipefail safety, and
    "no violations" on unparseable input would be exactly the silent lie the
    tool exists to catch. (YAML *syntax* is actionlint's job — this only fires
    when PyYAML can't build a document to analyze at all.)"""
    try:
        doc = yaml.load(path.read_text(), Loader=_LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                None,
                f"could not parse as YAML ({first_line}); cannot verify pipefail "
                "safety — fix the syntax (or run actionlint) and re-check.",
            )
        ]
    return analyze(doc)


def workflow_files() -> list[Path]:
    return _workflow_files(WORKFLOWS_DIR, ACTIONS_DIR)


def main() -> int:
    total = 0
    for path in workflow_files():
        rel = path.relative_to(REPO_ROOT)
        for line, message in check_file(path):
            loc = f"file={rel},line={line}" if line else f"file={rel}"
            print(f"::error {loc}::{message}")
            total += 1
    if total:
        print(f"\nERROR: {total} pipefail violation(s) found.")
        print(
            "A pipe in a non-pipefail shell masks a failing left-hand command. Add "
            "`set -o pipefail` to the script body (or use the default `run:` shell)."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Demand a version on every install command that would otherwise take "latest".

`pip install ruff`, `apt-get install -y docker-sbx`, `pipx install pre-commit`,
`npm install -g prettier`: each resolves against a registry index at run time, so
the bytes CI installs today are not the bytes it installed yesterday. That is the
same identity lie as an un-pinned base image, minus the FROM line — and it hides
worse, because the version that changed is nowhere in the diff. Real incident: a
runtime installed as `apt-get install docker-sbx` served v0.37.1 while the repo's
own `sbx-version.json` pinned v0.35.0; the pin existed and nothing at install time
read it, and the mismatch cost days of red end-to-end runs plus a wasted
revalidation cycle.

Flagged: an install invocation with at least one positional package spec that
carries no version, for four families —

  * pip (`pip install`, `pip3 install`, `python -m pip install`, `uv pip install`)
    — pinned by `pkg==1.2.3`;
  * tool installers (`pipx install`, `uv tool install`) — `pkg==1.2.3` or
    `pkg@1.2.3`;
  * Debian (`apt-get install`, `apt install`, `aptitude install`) — `pkg=1.2.3-1`;
  * global Node (`npm install -g`, `npm i --global`, `pnpm add -g`,
    `yarn global add`) — `pkg@1.2.3`.

Not flagged, because the version lives somewhere this lint can see is pinned or
somewhere it cannot judge: a requirements/constraints file (`-r`, `-c`) or a
pipx `--spec`, a local
path or archive (`.`, `./pkg`, `/tmp/x.whl`, `pkg.deb`), a URL or VCS spec
(`git+https://…@rev` carries its own ref), a spec built from a variable
(`"$PKG"`, `"ruff==${RUFF_VERSION}"` — the first is unknowable, the second is
already pinned), and a command inside a message string
(`echo "run pip install ruff"`). A **local** `npm install pkg` is also out of
scope: it writes the range into `package.json`, which is where that pin belongs
and what a lockfile check reads — only the global form, which pins nowhere, is
flagged.

An install that genuinely cannot be pinned opts out with a same-line or
preceding-line `# pin-exempt: <reason>` (the same annotation
check_pinned_downloads accepts). The recurring legitimate case is a distro
package tracking a moving index: `apt-get install -y curl` against Ubuntu's
archive fails outright once the indexed version rolls, so those sites annotate
rather than pin.

Dockerfiles are deliberately out of scope: hadolint's DL3008/DL3013/DL3018
already demand pinned `apt-get`/`pip`/`apk` installs there. This lint covers the
two places hadolint never looks — shell scripts and inline workflow `run:` blocks.

Invoked by pre-commit with the staged shell paths as arguments; a
`.github/{workflows,actions}` YAML path among them has each inline `run:` block
scanned instead (reported at the step's line).
"""

import re
import shlex
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import strip_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    MESSAGE_PREFIX,
    annotated,
    logical_lines,
)

OPT_OUT = "pin-exempt"

MESSAGE = (
    "install command names no version, so a later run installs different bytes "
    "than the ones you reviewed — pin it (`pkg==1.2.3` / `pkg=1.2.3-1` / "
    f"`pkg@1.2.3`), or annotate `# {OPT_OUT}: <reason>`"
)

_WORKFLOW_PATH = re.compile(r"(?:^|/)\.github/(?:workflows|actions)/.*\.ya?ml$")

# Version syntax differs per installer family, so each package spec is judged by
# the family that will resolve it.
PIP = "pip"  # `pkg==1.2.3`
TOOL = "tool"  # pipx / uv tool: `pkg==1.2.3` or `pkg@1.2.3`
APT = "apt"  # `pkg=1.2.3-1`
NODE = "node"  # `pkg@1.2.3` (global installs only)

# An optional `sudo` (with its flags and `VAR=value` prefixes) between the command
# boundary and the installer name.
_SUDO = r"(?:sudo\s+(?:-\S+\s+|\w+=\S+\s+)*)?"
# Start of a command: line start, or after whitespace, a shell separator, or a
# quote (`bash -c "pip install x"` runs the install). Keeps `nopip install` out.
_LEAD = r"""(?:^|[\s;&|(`"'])"""

_INSTALLERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        PIP,
        re.compile(
            rf"{_LEAD}{_SUDO}(?:python[\d.]*\s+-m\s+pip|pip[\d.]*|uv\s+pip)\s+install\b"
        ),
    ),
    (TOOL, re.compile(rf"{_LEAD}{_SUDO}(?:pipx\s+install|uv\s+tool\s+install)\b")),
    (
        APT,
        re.compile(
            rf"{_LEAD}{_SUDO}(?:apt-get|apt|aptitude)\s+(?:-{{1,2}}[\w-]+\s+)*install\b"
        ),
    ),
    (
        NODE,
        re.compile(
            rf"{_LEAD}{_SUDO}(?:npm\s+(?:install|i|add)|pnpm\s+(?:add|install)"
            rf"|yarn\s+global\s+add)\b"
        ),
    ),
)

# Where one command ends inside a joined logical line: a separator, a redirect, or
# the close of the substitution/subshell the command sits in. Everything after it
# belongs to a different command, not to this install's argument list. A redirect
# must be whitespace-surrounded so the `>` of a version floor (`pkg>=1.2`, quoted
# in real shell precisely because a bare one WOULD redirect) does not cut the
# segment mid-spec.
_SEGMENT_END = re.compile(r"&&|\|\||[;|)]|\s>>?\s|\s<\s")

# Flags that consume the next token, per family. Without these an `-o Foo=bar` or
# `-r req.txt` value would be read as a package spec — and `Foo=bar` carries an
# `=`, so an apt install would look pinned when its real package is not.
_VALUE_FLAGS = {
    PIP: {
        "-r",
        "--requirement",
        "-c",
        "--constraint",
        "-t",
        "--target",
        "-i",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "-e",
        "--editable",
        "--python",
        "--prefix",
        "--root",
        "--upgrade-strategy",
        "--no-binary",
        "--only-binary",
        "--platform",
        "--python-version",
        "--implementation",
        "--abi",
        "--cache-dir",
        "--config-settings",
        "--report",
    },
    TOOL: {
        "--with-requirements",
        "--python",
        "-p",
        "--index-url",
        "--extra-index-url",
        "-f",
        "--find-links",
        "--pip-args",
    },
    APT: {"-o", "--option", "-t", "--target-release", "-c", "--config-file"},
    NODE: {"--prefix", "--registry", "--workspace"},
}

# Flags whose value pins the whole command, so its positionals need no version of
# their own: a pip/uv constraints file fixes every spec resolved alongside it, and
# pipx's `--spec` IS the requirement spec the named tool installs from. (Whether the
# constraints file's own contents are pinned is that file's problem.) Per-family
# because apt's `-c` is `--config-file` and constrains nothing.
#
# `--with pkg` is deliberately absent: it installs another package, so it is judged
# as a spec like any positional.
_PINS_THE_COMMAND = {
    PIP: {"-c", "--constraint"},
    TOOL: {"-c", "--constraint", "--spec"},
    APT: set(),
    NODE: set(),
}

# A positional that is not a registry package name: a local path or archive, or a
# URL / VCS spec (which carries its own ref).
_NOT_A_REGISTRY_SPEC = re.compile(
    r"^[./~]"  # ./pkg, ../pkg, /tmp/x.whl, ~/pkg
    r"|^[a-z][a-z0-9+.-]*://"  # https://…, file://…
    r"|^(?:git|hg|bzr|svn)\+"  # git+https://…@rev
    r"|\.(?:whl|deb|tar\.gz|tgz|tar\.bz2|zip)$"
)

# Shell plumbing that shares the argument list but names no package: a redirection
# (`>&2`, `2>&1`, `>log`) or a control operator the segment scan did not cut. Read as
# a spec, `>&2` would make every `apt-get install pkg=1.2 >&2` look unpinned.
_SHELL_PLUMBING = re.compile(r"^[<>&]|^\d+[<>]")

# A spec whose version is decided at run time by the shell: `"$PKG"`,
# `"ruff==${RUFF_VERSION}"`, `` `cat spec` ``. Reading it as unpinned would flag a
# line whose pin this lint cannot see, so it is left alone.
_DYNAMIC = re.compile(r"[$`]")

_GLOBAL_FLAG = re.compile(r"(?:^|\s)(?:-\w*g\w*|--global)\b")

# What has to appear in front of a QUOTED install for it to run: the command being
# invoked must itself execute the string. `bash -c "pip install x"` and
# `ssh host "apt-get install x"` install; `gb_error "install it: apt install
# coreutils"` and `require_command jq "e.g. apt-get install jq"` are text written
# for a human, and a repo's own logger/help-text helpers are unenumerable — so the
# rule keys on the executor rather than on knowing every printing command's name.
#
# ANCHORED at the command word, because an interpreter NAME can appear inside the
# very hint text being excused: `missing_gate "install it with 'bash setup.bash'"`
# is not a `bash` invocation, and an unanchored search reads it as one.
_INTERPRETER = re.compile(
    r"^(?:sudo\s+(?:-\S+\s+)*)?(?:env\s+)?(?:\w+=\S+\s+)*(?:\S*/)?"
    r"(?:sh|bash|dash|zsh|ksh|ash|eval|ssh|xargs)\b"
)


def _tokens(segment: str) -> list[str]:
    """Shell-split SEGMENT, falling back to whitespace splitting.

    ``shlex.split`` is what knows that `"ruff==1.2"` is one token and that a quote
    does not belong to the package name. It raises on an unbalanced quote, which a
    joined logical line legitimately has (the closing quote lives in a command this
    segment cut away), so that one case degrades to whitespace splitting rather
    than crashing a commit."""
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _is_pinned(spec: str, family: str) -> bool:
    """True when SPEC names the version FAMILY's installer would otherwise pick."""
    if family in (PIP, TOOL) and "==" in spec:
        return True
    if family == APT:
        return "=" in spec
    if family in (TOOL, NODE):
        # `pkg@1.2.3`; the leading `@` of a scoped npm name (`@scope/pkg`) is not a
        # version separator, so look past it.
        return "@" in spec[1:]
    return False


def _unpinned_specs(segment: str, family: str) -> list[str]:
    """Positional package specs in SEGMENT that name no version.

    Flags and their values are consumed, non-registry specs (paths, URLs, VCS refs)
    and shell-dynamic specs are skipped, and a constraints/`--spec` flag pins the
    whole command."""
    unpinned: list[str] = []
    skip_next = False
    for token in _tokens(segment):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            flag = token.split("=", 1)[0]
            if flag in _PINS_THE_COMMAND[family]:
                return []
            skip_next = flag in _VALUE_FLAGS[family] and "=" not in token
            continue
        if (
            _DYNAMIC.search(token)
            or _NOT_A_REGISTRY_SPEC.search(token)
            or _SHELL_PLUMBING.match(token)
        ):
            continue
        if not _is_pinned(token, family):
            unpinned.append(token)
    return unpinned


def _segment_after(line: str, end: int) -> str:
    """LINE's argument text from offset END up to the end of that command."""
    tail = line[end:]
    cut = _SEGMENT_END.search(tail)
    return tail[: cut.start()] if cut else tail


def _command_prefix(line: str, start: int) -> tuple[str, bool]:
    """LINE's text from the previous command separator up to offset START, plus
    whether START sits inside a quoted string.

    Scoping the message-command test (`echo "run pip install ruff"`) to this
    prefix rather than to the whole line is what keeps an install joined onto a
    message — `echo installing && pip install ruff` — in view; skipping the whole
    logical line would let the leading `echo` hide it.

    Separators inside a quoted string do not start a new command, or a hint that
    happens to contain one (`gb_error "install it: apt install coreutils"`) would
    read as a command of its own with `install` as its name — which is also why
    the quote state travels back with the prefix."""
    prefix = line[:start]
    cut = 0
    quote = ""
    i = 0
    while i < len(prefix):
        char = prefix[i]
        if char == "\\":
            i += 2
            continue
        if quote:
            quote = "" if char == quote else quote
            i += 1
            continue
        if char in "\"'":
            quote = char
            i += 1
            continue
        separator = _SEGMENT_END.match(prefix, i)
        if separator:
            cut = i = separator.end()
            continue
        i += 1
    return prefix[cut:].lstrip(), bool(quote)


def violations(text: str) -> list[int]:
    """1-based line numbers of install commands that name no version.

    Detection runs over LOGICAL lines (continuations joined) of a
    COMMENT-STRIPPED view, so a `\\`-wrapped install is analyzed as one command and
    an install quoted in a comment is not a command at all. The raw physical lines
    are kept for the `# pin-exempt:` opt-out, which by definition lives in a
    comment (accepted on any physical line of the flagged command, or the line
    directly above it)."""
    raw = text.splitlines()
    logicals = logical_lines(strip_comments(text))
    starts = [start for start, _ in logicals]
    hits: list[int] = []
    for index, (start, line) in enumerate(logicals):
        if not _has_unpinned_install(line):
            continue
        span_end = starts[index + 1] - 1 if index + 1 < len(starts) else len(raw)
        span = raw[start - 1 : span_end]
        if any(annotated(physical, OPT_OUT) for physical in span) or (
            start >= 2 and annotated(raw[start - 2], OPT_OUT)
        ):
            continue
        hits.append(start)
    return hits


def _has_unpinned_install(line: str) -> bool:
    """True when LINE runs an install command with at least one unpinned spec."""
    for family, pattern in _INSTALLERS:
        for match in pattern.finditer(line):
            prefix, in_quotes = _command_prefix(line, match.start())
            if MESSAGE_PREFIX.match(prefix):
                continue  # an argument to a command that only prints
            # A match whose own leading character is the quote opens the string it
            # sits in, so it is quoted too — `echo "pip install x"` must not read
            # as an unquoted install just because the quote came first.
            if (in_quotes or match.group()[:1] in "\"'") and not _INTERPRETER.match(
                prefix
            ):
                continue  # text inside a string nothing executes
            segment = _segment_after(line, match.end())
            # Only a GLOBAL Node install pins nowhere else; a local one records its
            # range in package.json. `yarn global add` says so in the command
            # itself, npm/pnpm say it with a `-g`/`--global` flag.
            if (
                family == NODE
                and "global" not in match.group()
                and not _GLOBAL_FLAG.search(segment)
            ):
                continue
            if _unpinned_specs(segment, family):
                return True
    return False


def _run_scripts(path: Path) -> list[tuple[int, str]]:
    """(step line, script) for every inline `run:` block in a workflow or
    composite-action file. An unparseable file yields no scripts — YAML syntax is
    actionlint's job, and the shell files this lint owns are its argv."""
    try:
        doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    containers = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        containers += [job for job in jobs.values() if isinstance(job, dict)]
    runs = doc.get("runs")
    if isinstance(runs, dict):
        containers.append(runs)
    scripts: list[tuple[int, str]] = []
    for container in containers:
        steps = container.get("steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                scripts.append((step.get("__line__", 1), step["run"]))
    return scripts


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    status = 0
    for arg in argv:
        path = Path(arg)
        try:
            if _WORKFLOW_PATH.search(arg.replace("\\", "/")):
                hits = [
                    step_line
                    for step_line, script in _run_scripts(path)
                    if violations(script)
                ]
            elif arg.endswith((".yaml", ".yml")):
                continue  # a non-workflow YAML file is not shell
            else:
                hits = violations(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue  # a deleted/renamed path pre-commit may still list
        for lineno in hits:
            print(f"{arg}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())

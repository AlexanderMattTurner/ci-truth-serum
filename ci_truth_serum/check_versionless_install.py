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

The scan runs on the REAL bash grammar (``_bash_ast``), walking ``command`` nodes,
so the questions that sink a text scan are answered by structure instead of
guessed at: a redirection is a ``file_redirect`` sibling and never an argument; a
quoted message is a ``string`` argument of the command that prints it, holding no
commands at all; a heredoc body is a ``heredoc_body``; ``&&``/``;``/``|`` split a
``list``/``pipeline`` into separate commands; and a ``$VAR``/``$(…)`` spec is an
``expansion`` child rather than a ``$`` somewhere in the line.

Not flagged, because the version lives somewhere this lint can see is pinned or
somewhere it cannot judge: a requirements/constraints file (``-r``, ``-c``) or a
pipx ``--spec``, a local path or archive (``.``, ``./pkg``, ``/tmp/x.whl``,
``pkg.deb``), a URL or VCS spec (``git+https://…@rev`` carries its own ref), a
spec whose value comes from the shell (``"$PKG"``, ``"ruff==${RUFF_VERSION}"`` —
the first is unknowable, the second already pinned), and an install named by a
command that only prints (``echo``, ``printf``, ``warn``, …). A **local** ``npm
install pkg`` is also out of scope: it writes the range into ``package.json``,
which is where that pin belongs and what a lockfile check reads — only the global
form, which pins nowhere, is flagged.

An install inside a string is text, not a command — EXCEPT where something
executes that string: ``bash -c "pip install x"``, ``eval "…"``, ``ssh host "…"``,
and an interpreter reached through a wrapper (``xargs … sh -c '…'``). Those bodies
are parsed as scripts in their own right, so the install inside them is judged
like any other.

An install that genuinely cannot be pinned opts out with a same-line or
preceding-line ``# pin-exempt: <reason>`` (the same annotation
check_pinned_downloads accepts). The recurring legitimate case is a distro
package tracking a moving index: ``apt-get install -y curl`` against Ubuntu's
archive fails outright once the indexed version rolls, so those sites annotate
rather than pin.

Dockerfiles are deliberately out of scope: hadolint's DL3008/DL3013/DL3018
already demand pinned ``apt-get``/``pip``/``apk`` installs there. This lint covers
the two places hadolint never looks — shell scripts and inline workflow ``run:``
blocks.

Invoked by pre-commit with the staged shell paths as arguments; a
``.github/{workflows,actions}`` YAML path among them has each inline ``run:``
block scanned instead (reported at the step's line).
"""

import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    parse,
    unquote,
)
from _linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    MESSAGE_PREFIX,
    annotated_near,
    run_file_cli,
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

# The command words that open an install, matched against a command's argument
# tokens. A leading wrapper needs no enumeration — `retry sudo apt-get install …`,
# `time pip install …` — because the sequence is searched at any token position,
# and a message's contents are a `string` node rather than tokens.
_INSTALL_COMMANDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (PIP, ("pip", "install")),
    (PIP, ("uv", "pip", "install")),
    (TOOL, ("pipx", "install")),
    (TOOL, ("uv", "tool", "install")),
    (APT, ("apt-get", "install")),
    (APT, ("apt", "install")),
    (APT, ("aptitude", "install")),
    (NODE, ("npm", "install")),
    (NODE, ("npm", "i")),
    (NODE, ("npm", "add")),
    (NODE, ("pnpm", "add")),
    (NODE, ("pnpm", "install")),
    (NODE, ("yarn", "global", "add")),
)
# `pip3`/`pip3.11` are the same installer; `python -m pip` reaches it the long way.
_PIP_ALIAS = re.compile(r"^pip[\d.]*$")
_PYTHON = re.compile(r"^python[\d.]*$")

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
    APT: frozenset(),
    NODE: frozenset(),
}

# A positional that is not a registry package name: a local path or archive, or a
# URL / VCS spec (which carries its own ref).
_NOT_A_REGISTRY_SPEC = re.compile(
    r"^[./~]"  # ./pkg, ../pkg, /tmp/x.whl, ~/pkg
    r"|^[a-z][a-z0-9+.-]*://"  # https://…, file://…
    r"|^(?:git|hg|bzr|svn)\+"  # git+https://…@rev
    r"|\.(?:whl|deb|tar\.gz|tgz|tar\.bz2|zip)$"
)

_GLOBAL_FLAG = re.compile(r"^(?:-\w*g\w*|--global)$")

# The shells whose `-c` argument is a script, and the commands whose arguments are
# code by definition. A string reached this way is parsed as its own script — the
# one place where quoted text is treated as commands.
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "busybox"})
_CODE_COMMANDS = frozenset({"eval", "ssh"})

# A value decided at run time by the shell, as NODE TYPES rather than a `$` in the
# text: `"ruff==${V}"` is a string with an `expansion` child, `"$PKG"` a string
# whose whole content is one.
_DYNAMIC_TYPES = frozenset(
    {"expansion", "simple_expansion", "command_substitution", "arithmetic_expansion"}
)


def _is_dynamic(node) -> bool:
    """True when NODE's value depends on a shell expansion (`iter_nodes` is
    inclusive, so a bare `$PKG` argument counts as much as one nested in a
    string)."""
    return next(iter_nodes(node, *_DYNAMIC_TYPES), None) is not None


def _find_install(tokens: list[str]) -> tuple[str, int] | None:
    """(family, index of the first argument) for the install invocation in TOKENS,
    or None. `python -m pip install` and `pip3`/`pip3.11` resolve to pip."""
    for start, token in enumerate(tokens):
        if _PYTHON.match(token) and tokens[start + 1 : start + 4] == [
            "-m",
            "pip",
            "install",
        ]:
            return PIP, start + 4
        for family, words in _INSTALL_COMMANDS:
            end = start + len(words)
            candidate = tokens[start:end]
            if len(candidate) < len(words):
                continue
            head_matches = candidate[0] == words[0] or (
                words[0] == "pip" and bool(_PIP_ALIAS.match(candidate[0]))
            )
            if head_matches and candidate[1:] == list(words[1:]):
                return family, end
    return None


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


def _unpinned_specs(args: list, family: str) -> list[str]:
    """Package specs among ARGS that name no version.

    Flags and their values are consumed, non-registry specs (paths, URLs, VCS refs)
    and shell-decided values are skipped, and a constraints/`--spec` flag pins the
    whole command."""
    unpinned: list[str] = []
    skip_next = False
    for node in args:
        raw = unquote(node_text(node))
        if skip_next:
            skip_next = False
            continue
        if raw.startswith("-"):
            flag = raw.split("=", 1)[0]
            if flag in _PINS_THE_COMMAND[family]:
                return []
            skip_next = flag in _VALUE_FLAGS[family] and "=" not in raw
            continue
        if _is_dynamic(node) or _NOT_A_REGISTRY_SPEC.search(raw):
            continue
        if not _is_pinned(raw, family):
            unpinned.append(raw)
    return unpinned


def _executed_strings(tokens: list[str], args: list) -> list:
    """String arguments whose contents something RUNS, so they hold commands.

    A shell's `-c` script, `eval`'s arguments and `ssh`'s remote command are code;
    an interpreter reached through a wrapper (`xargs … sh -c '…'`) is covered
    because the shell is found at any token position. Every other string is text a
    command prints or passes on."""
    quoted = [node for node in args if node.type in ("string", "raw_string")]
    if not (quoted and tokens):
        return []
    if tokens[0] in _CODE_COMMANDS:
        return quoted[-1:] if tokens[0] == "ssh" else quoted
    for index, token in enumerate(tokens):
        if token.split("/")[-1] in _SHELLS and "-c" in tokens[index + 1 :]:
            return quoted[:1]
    return []


def _install_spans(root) -> list[tuple[int, int]]:
    """(first line, last line) of every unpinned install command under ROOT, both
    1-based.

    Recurses into the string bodies something executes, so an install inside
    `bash -c "…"` is judged as the command it becomes; its lines are reported
    relative to the enclosing script."""
    spans: list[tuple[int, int]] = []
    for command in iter_nodes(root, "command"):
        args = command_arguments(command)
        tokens = [unquote(node_text(node)) for node in args]
        for node in _executed_strings(tokens, args):
            offset = node.start_point[0]
            spans += [
                (offset + first, offset + last)
                for first, last in _install_spans(parse(unquote(node_text(node))))
            ]
        if tokens and MESSAGE_PREFIX.match(tokens[0]):
            continue  # a command that only prints; its arguments are text
        found = _find_install(tokens)
        if not found:
            continue
        family, index = found
        rest = args[index:]
        # Only a GLOBAL Node install pins nowhere; a local one records its range in
        # package.json. `yarn global add` says so in the command words themselves.
        if family == NODE and not (
            "global" in tokens[:index]
            or any(_GLOBAL_FLAG.match(token) for token in tokens[index:])
        ):
            continue
        if _unpinned_specs(rest, family):
            spans.append((command.start_point[0] + 1, command.end_point[0] + 1))
    return spans


def violations(text: str) -> list[int]:
    """1-based line numbers of install commands that name no version.

    Detection walks the bash grammar, so a `\\`-continued command is ONE node
    (reported at its first line), an install written in a comment is a `comment`
    node rather than a command, and one written inside a message string is that
    string. The raw physical lines are kept for the `# pin-exempt:` opt-out, which
    by definition lives in a comment (accepted on any physical line of the flagged
    command, or the line directly above it)."""
    raw = text.splitlines()
    # One finding per line, carrying the WIDEST span that starts there: two commands
    # can begin on the same row (`pip install a; pip install b`, or a `bash -c`
    # string whose install sits on the line that opened it), and a line reported
    # twice would be a duplicate finding — plus the wider span gives the
    # `pin-exempt` lookup every physical line the reader would put it on.
    widest: dict[int, int] = {}
    for start, end in _install_spans(parse(text)):
        widest[start] = max(widest.get(start, end), end)
    hits = []
    for start, end in sorted(widest.items()):
        if annotated_near(raw, start, OPT_OUT, span_end=end):
            continue
        hits.append(start)
    return hits


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


def main(argv: list[str]) -> int:
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
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails the check LOUDLY (the
            # same posture as check_untrusted_exec): skipping it would false-green
            # exactly the input an adversary controls.
            print(f"{arg}: {err}", file=sys.stderr)
            status = 1
            continue
        for lineno in hits:
            print(f"{arg}:{lineno}: {MESSAGE}", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

#!/usr/bin/env python3
"""Demand a cache in any job that downloads bytes that cannot change between runs.

A `run:` step that fetches a pinned tool — `pip install ruff==0.14.0`, `npm
install -g pkg@2.1.0`, `curl -O …/releases/download/v2.12.0/tool` — pulls the
same bytes on every run of every push to every open pull request. The bytes
cannot differ, because the version is pinned. The download is therefore pure
waste, and the waste is invisible: the step is green, and its cost shows up as
minutes on a job's clock rather than as a failure anyone reads.

The failure it does produce is a timeout, and a timeout does not say so. GitHub
reports a job that runs past `timeout-minutes` with the conclusion **cancelled**,
and its log holds one line: "The operation was canceled." Worked case from the
repo this lint came out of: a test shard with a 15-minute budget spent 7.9 of
those minutes on one `npm install -g` of a pinned package, because the registry
was slow that hour. Three shards went "cancelled" at once. Nobody reads
"cancelled" as "your uncached download ran long", so the hunt went to the tests.
A cache keyed on the pinned version turns that 7.9 minutes into seconds.

A download is pinned in one of two ways:

  * the URL or the package spec names a fixed release (`/v2.7.0/`, `==0.14.0`,
    or a `*VERSION*` variable);
  * the same shell process checks the download against a digest
    (`sha256sum -c`, `shasum -a 256 -c`), or the same Python function hashes
    the bytes and compares the hash. A job that refuses every other byte has
    immutable input by its own rule.

The lint reads more than the `run:` text. `_cts_step_reach` follows each repo
script the step runs or sources, and each function call into that function's
body. It also records a Python file the step hands an interpreter, which this
module parses with `ast`. A path that the text does not fix ends the walk with
no finding, because a guessed path judges a file the step never runs. Measured
on agent-glovebox: both osv-scanner jobs download a pinned binary on every run
through a two-file chain, and only a walk that follows scripts sees it.

A cache in the job covers a download only when the cache can hold it:

  * its `key:` names the pin — the version, the digest, the version variable
    as `env.NAME`, or a `hashFiles()` of a file that holds the fetch or
    assigns its pin;
  * or its `path:` holds the file the download writes.

A variable counts as a pin only when its name says it holds one (`*VERSION*`,
`*SHA*`, …) or the text fixes a version or digest as its value, so a key that
names `env.ARCH` does not cover a download whose URL also reads `$ARCH`.

A cache whose key or path the lint cannot read statically (a setup action's own
cache, a key from a step output) still covers every download in the job. A miss
there costs less than a false positive. Local composite actions
(`uses: ./.github/actions/foo`) are inlined first.

A fetcher that checks a cache directory before it downloads satisfies the lint
through the `actions/cache` step that restores that directory, when the key of
that step names the pin or hashes the fetcher. The lint cannot read where a
Python fetch writes, so a `path:` alone does not cover one.

Deliberately NOT reported, because the download is the point or has no stable key:

  * an install with any unpinned spec — `check-versionless-install` owns that, and
    a floating install has no cache key that could stay correct;
  * a `docker pull`, and any install inside a job whose purpose is to prove a
    fresh machine can install the thing;
  * a program the job does not run from the repo, such as a pip-installed CLI.

Fix it by adding `actions/cache` keyed on the pinned version, and by making the
install skip its own work when the cache restores. Probe the post-condition (the
digest of the file, the version the binary reports), never the `cache-hit` output
alone: the post-job save runs even after a failed install, so a cache entry can
hold a partial tree.

A job that must download every time opts out with a `# cache-exempt: <reason>`
comment, either in the `run:` body or anywhere in the job's block. The reason is
REQUIRED; a bare annotation does not suppress.

The workflow is the artifact under test, so a workflow or composite action that
does not parse is a finding. A followed Python file that does not parse ends the
walk, like any other path the lint cannot read. A followed file that is not
UTF-8 raises, because the lint cannot judge bytes it cannot decode.

This lint is opinionated (Tier 2): it prescribes a cache for a class of cost that
is real but never fails loudly.
"""

import ast
import functools
import posixpath
import re
import sys
from pathlib import Path
from typing import NamedTuple

import yaml
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    command_arguments,
    iter_nodes,
    node_text,
    program_name,
    redirect_parts,
    unquote,
)
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    annotated,
    LineLoader,
    MESSAGE_PREFIX,
    workflow_files,
    _job_blocks,
    yaml_script_view,
)
from _cts_py_ast import name_of  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_py_imports import local_files, sys_path_roots  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_step_reach import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Namespace,
    Unit,
    body_start,
    callable_words,
    definitions,
    load_shell,
    reach,
    tokens,
)
from check_path_gate_deps import glob_to_regex  # noqa: E402,I001  # pylint: disable=wrong-import-position
from check_versionless_install import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    NODE,
    _find_install,
    spec_scan,
    writes_outside_the_lockfile,
)

REPO_ROOT = Path.cwd()
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
OPT_OUT = "cache-exempt"

MESSAGE = (
    "job '{job}' downloads pinned {what} with {why} — the bytes are identical "
    "on every run, so every push to every pull request pays for the same "
    "fetch, and a slow registry turns that cost into a job timeout that GitHub "
    "reports as 'cancelled'. Add actions/cache keyed on the pin (or on "
    "hashFiles() of the file that sets it), or with a path that holds the "
    "download, and skip the download when the cache restores, or annotate "
    f"'# {OPT_OUT}: <reason>'."
)
NO_CACHE = "no cache step anywhere in the job"
NO_COVER = "no cache in the job keyed on its pin or holding its output path"

# An action that caches. `actions/cache` and its `restore`/`save` sub-actions do
# so by definition; the language setup actions do so only when their cache input
# is set, which is why the input name is part of the table rather than assumed.
_CACHE_ACTIONS = ("actions/cache", "buildjet/cache")
_SETUP_CACHE_INPUTS: dict[str, tuple[str, ...]] = {
    "actions/setup-node": ("cache",),
    "actions/setup-python": ("cache",),
    "actions/setup-go": ("cache",),
    "actions/setup-java": ("cache",),
    "actions/setup-dotnet": ("cache",),
    "astral-sh/setup-uv": ("enable-cache",),
    "ruby/setup-ruby": ("bundler-cache",),
}
# An action whose whole job is to cache, so it carries no enabling input.
_ALWAYS_CACHING_ACTIONS = ("Swatinem/rust-cache",)

# A version that is not a version: the registry still resolves it fresh, so the
# download has no stable cache key and this lint must leave it alone.
_FLOATING = re.compile(r"[=@](?:latest|next|stable|\*|main|master)$", re.IGNORECASE)

_DOWNLOADERS = frozenset({"curl", "wget"})
# A URL whose path carries a concrete release: a `v1.2.3` style segment, or a
# shell variable whose NAME says it holds one. Without that the fetch resolves to
# whatever the host serves today, which no cache key can track.
#
# GITHUB_* and RUNNER_* are excluded because they are the runner's OWN context
# variables, and the ones whose names end in REF or SHA hold a different value on
# every run — $GITHUB_SHA is this commit, $GITHUB_REF_NAME is this branch. A URL
# built from one changes per run, so a cache keyed on it could never restore, and
# flagging it would contradict the "not a version" exclusion above.
_PINNED_URL = re.compile(
    r"^https?://.*(?:/v?\d+\.\d+[\w.+-]*/"
    r"|\$\{?(?!(?:GITHUB|RUNNER)_)\w*(?:VERSION|RELEASE|TAG|REF|SHA)\w*)",
    re.IGNORECASE,
)
_URL = re.compile(r"^https?://")
_VERSION_LITERAL = re.compile(r"(?<![\w.])v?(?P<version>\d+\.\d+(?:\.\d+)*)(?![\w])")
_DIGEST_LITERAL = re.compile(r"\b[0-9a-f]{64}(?:[0-9a-f]{64})?\b")
_RUNNER_VARIABLE = re.compile(r"^(?:GITHUB|RUNNER)_")
# A variable whose name says it holds a pin. REF is left out: it is a substring
# of PREFIX, a word that names a directory far more often than a release.
_PIN_NAME = re.compile(r"VERSION|RELEASE|TAG|SHA|DIGEST|CHECKSUM|HASH", re.IGNORECASE)
_IDENTIFIER = re.compile(r"[A-Za-z_]\w*")
# The suffix a checksum file adds to the name of the file it vouches for.
_CHECKSUM_SUFFIX = re.compile(r"\.(?:sha(?:1|224|256|384|512)(?:sums?)?|b2|asc)$")

# The checksum tools whose `-c` mode refuses any bytes but the pinned ones.
_DIGEST_TOOLS = re.compile(r"sha(?:1|224|256|384|512)sum|shasum|b2sum")
_CHECK_FLAG = re.compile(r"^(?:--check|-[A-Za-z]*c[A-Za-z]*)$")
# The nodes around a checksum command that carry its stdin.
_CHECK_SCOPES = frozenset({"pipeline", "redirected_statement"})

# curl's and wget's flags that name the file the download writes.
# A short flag closes a cluster (`-fsSLo FILE`). The only attached value read
# is `-` (`-qO-`, stdout): in `-Hcontent-type:x` the `o` is part of a value.
_OUTPUT_FLAGS = {
    "curl": (re.compile(r"^-[A-Za-z]*o(?P<value>-?)$"), "--output"),
    "wget": (re.compile(r"^-[A-Za-z]*O(?P<value>-?)$"), "--output-document"),
}
# curl's `-O` writes the URL's last path segment.
_REMOTE_NAME = re.compile(r"^(?:-[A-Za-z]*O[A-Za-z]*|--remote-name)$")
# curl's and wget's flags that name the directory the file lands in.
_DIRECTORY_FLAGS = {
    "curl": (None, "--output-dir"),
    "wget": (re.compile(r"^-[A-Za-z]*P(?P<value>)$"), "--directory-prefix"),
}
# The source that stands for the workflow or action file a `run:` body is
# written in, until `_script_fetches` knows which file that is.
_RUN_BODY = "<run body>"

# A cache key expression this lint can read: the runner, the env, the matrix,
# the event context, and `hashFiles()`. Anything else (a step output, an input)
# is computed at run time, so the cache reads as covering everything.
_READABLE_EXPRESSION = re.compile(
    r"^\s*(?:(?:runner|env|matrix|github)\.[\w.-]+|hashFiles\(.*\))\s*$", re.DOTALL
)
_EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)\}\}", re.DOTALL)
_HASHFILES = re.compile(r"hashFiles\((?P<args>[^)]*)\)")
_QUOTED = re.compile(r"'(?P<single>[^']*)'|\"(?P<double>[^\"]*)\"")

# The Python calls that fetch bytes over the network.
_PY_FETCHERS = frozenset(
    {
        "urllib.request.urlopen",
        "urllib.request.urlretrieve",
        "requests.get",
        "httpx.get",
        "httpx.stream",
    }
)
_PY_HASH_METHODS = frozenset({"hexdigest", "digest"})
_PY_DIGEST_COMPARES = frozenset({"hmac.compare_digest", "secrets.compare_digest"})


class Fetch(NamedTuple):
    """One pinned download a step causes.

    WHAT names it for the message. PINS are the words a cache key may carry to
    name it: variable names, versions, digests. SOURCES are the repo-relative
    files a `hashFiles()` key may hash to name it. OUTPUTS are the paths the
    bytes land at, as far as the text fixes them. OUTPUT_NAMES are the
    relative paths the bytes land at in a directory the text does not fix."""

    what: str
    pins: frozenset[str]
    sources: frozenset[str]
    outputs: frozenset[str]
    output_names: frozenset[str] = frozenset()


def _action_name(step: dict) -> str:
    """The action STEP uses, without its `@ref` (`actions/cache@0057852…`)."""
    uses = step.get("uses")
    return uses.split("@", 1)[0].strip() if isinstance(uses, str) else ""


def _caches(step: dict) -> bool:
    """True when STEP restores or saves a cache."""
    action = _action_name(step)
    if not action:
        return False
    if action.startswith(_CACHE_ACTIONS) or action in _ALWAYS_CACHING_ACTIONS:
        return True
    inputs = _SETUP_CACHE_INPUTS.get(action)
    with_block = step.get("with")
    if not inputs or not isinstance(with_block, dict):
        return False
    return any(
        str(with_block.get(key, "")).strip() not in ("", "false") for key in inputs
    )


# ── shell: what a namespace downloads ───────────────────────────────────────


def _relative(path: Path | None) -> str | None:
    if path is None or not path.is_relative_to(REPO_ROOT):
        return None
    return path.relative_to(REPO_ROOT).as_posix()


def _downloader_functions(namespace: Namespace) -> frozenset[str]:
    """The functions whose bodies run curl or wget, directly or through another
    such function. `pinned_curl() { curl … "$url"; }` is one."""
    callees: dict[str, set[str]] = {}
    found: set[str] = set()
    for name in namespace.functions:
        callees[name] = set()
        for command, _ in namespace.function_commands(name):
            if _runs_downloader(command):
                found.add(name)
            callees[name] |= set(callable_words(command)) & namespace.functions.keys()
    changed = True
    while changed:
        before = len(found)
        found |= {name for name, called in callees.items() if called & found}
        changed = len(found) != before
    return frozenset(found)


@functools.lru_cache(maxsize=65536)
def _runs_downloader(command: Node) -> bool:
    """True when COMMAND holds a curl or wget word at any position, so a
    leading wrapper (`sudo curl …`, `retry wget …`) does not hide it."""
    return any(program_name(word) in _DOWNLOADERS for word in tokens(command))


def _expanded_names(node: Node) -> set[str]:
    """The variables NODE expands, minus the runner's own context variables."""
    names = set()
    for expansion in iter_nodes(node, "simple_expansion", "expansion"):
        for name in iter_nodes(expansion, "variable_name"):
            text = node_text(name)
            if not _RUNNER_VARIABLE.match(text) and not text.isdigit():
                names.add(text)
    return names


def _literals(text: str) -> set[str]:
    """The versions and digests written in TEXT."""
    return set(_VERSION_LITERAL.findall(text)) | set(_DIGEST_LITERAL.findall(text))


def _is_pin_name(name: str, value: str | None) -> bool:
    """True when the variable NAME holds a pin: its name says so, or VALUE, its
    fixed value, carries a version or digest."""
    return bool(_PIN_NAME.search(name)) or bool(value and _literals(value))


def _pins_of(nodes: list[Node], namespace: Namespace, unit: Unit) -> set[str]:
    """The words a cache key may carry to name what NODES pin: each version or
    digest written in them, and each pin variable they expand. A variable
    assigned from another one adds that one too, so `version="${TOOL_VERSION#v}"`
    lets a key name `TOOL_VERSION`."""
    names: set[str] = set()
    pending = [name for node in nodes for name in _expanded_names(node)]
    while pending:
        name = pending.pop()
        if name in names:
            continue
        names.add(name)
        for assignment, _ in namespace.assignments.get(name, []):
            pending += _expanded_names(assignment)
    values = {name: namespace.variable(name, unit) for name in names}
    pins = {name for name, value in values.items() if _is_pin_name(name, value)}
    texts = [node_text(node) for node in nodes]
    texts += [namespace.value(node, unit) or "" for node in nodes]
    texts += [value or "" for value in values.values()]
    for text in texts:
        pins |= _literals(text)
    return pins


def _sources_of(pins: set[str], namespace: Namespace, unit: Unit) -> set[str]:
    """The files a `hashFiles()` key may hash to name a fetch: the file that
    holds it, and each file that assigns one of its pins. `_RUN_BODY` stands
    for the workflow or action file when that file is one of them."""
    paths = {unit.path} if unit.path else set()
    for pin in pins:
        paths |= namespace.assigned_in(pin)
    sources = {rel for path in paths if (rel := _relative(path))}
    in_body = unit.path is None or any(
        owner.path is None
        for pin in pins
        for _, owner in namespace.assignments.get(pin, [])
    )
    return sources | {_RUN_BODY} if in_body else sources


def _digest_checks(namespace: Namespace) -> list[tuple[Node, Unit]]:
    """The `sha256sum -c` style commands the namespace runs. Each comes back as
    the pipeline or redirected statement around it, since the digest and the
    file name usually arrive on stdin."""
    found = []
    for command, unit in namespace.executed:
        words = tokens(command)
        if not words or not _DIGEST_TOOLS.fullmatch(program_name(words[0])):
            continue
        if not any(_CHECK_FLAG.match(word) for word in words[1:]):
            continue
        scope = command
        while scope.parent is not None and scope.parent.type in _CHECK_SCOPES:
            scope = scope.parent
        found.append((scope, unit))
    return found


def _checked_names(checks: list[tuple[Node, Unit]], namespace: Namespace) -> set[str]:
    """The base names of the files the digest checks name, as far as the text
    fixes them, from argument words and here-strings:
    `echo "$SHA  tool" | sha256sum -c -` names `tool`, and
    `sha256sum -c tool.tgz.sha256` names `tool.tgz` through its checksum file."""
    names = set()
    for scope, unit in checks:
        nodes = [
            node
            for command in iter_nodes(scope, "command")
            for node in command_arguments(command)[1:]
        ]
        nodes += [
            child
            for here in iter_nodes(scope, "herestring_redirect")
            for child in here.named_children
        ]
        for node in nodes:
            for word in _spelled(node, namespace, unit).split():
                if word.startswith("-"):
                    continue  # an option, or stdin
                name = posixpath.basename(word)
                names |= {name, _CHECKSUM_SUFFIX.sub("", name)}
    return names


def _spelled(
    node: Node,
    namespace: Namespace,
    unit: Unit,
    following: frozenset[str] = frozenset(),
) -> str:
    """NODE's value when the text fixes it. Otherwise its text after quote
    removal. A variable with one assignment spells that assignment, so
    `D="$HOME/bin"` makes `$D/tool` read `$HOME/bin/tool`. Any other expansion
    is spelled `$NAME`, so one variable reads the same in an output flag and
    in a here-string. FOLLOWING holds the names being spelled, to stop a cycle."""
    value = namespace.value(node, unit)
    if value is not None:
        return value
    if node.type in ("simple_expansion", "expansion"):
        name_node = next(iter_nodes(node, "variable_name"), None)
        if name_node is None:
            return node_text(node)
        name = node_text(name_node)
        assignments = namespace.assignments.get(name, [])
        plain = node_text(node).strip() in (f"${name}", f"${{{name}}}")
        if plain and name not in following and len(assignments) == 1:
            assignment, owner = assignments[0]
            assigned = assignment.child_by_field_name("value")
            if assigned is not None:
                return _spelled(assigned, namespace, owner, following | {name})
        return f"${name}"
    if node.type not in ("string", "concatenation"):
        return unquote(node_text(node))
    raw, base = node.text or b"", node.start_byte
    start = 1 if node.type == "string" else 0
    end = len(raw) - start
    pieces = []
    for child in node.named_children:
        if child.type == "string_content":
            continue
        pieces += [raw[start : body_start(child) - base].decode()]
        pieces += [_spelled(child, namespace, unit, following)]
        start = child.end_byte - base
    pieces.append(raw[start:end].decode())
    return "".join(pieces)


def _flag_values(words: list[str], spelled: list[str], short, long: str) -> list[str]:
    """The values WORDS give the flag spelled SHORT (a pattern with a `value`
    group, or None) or LONG, in order: `-o FILE`, `-o-`, `--output FILE` and
    `--output=FILE`. Each value comes from SPELLED, the words' values."""
    found = []
    for index, (word, value) in enumerate(zip(words, spelled)):
        after = spelled[index + 1] if index + 1 < len(words) else None
        match = short.match(word) if short is not None else None
        if word == long or (match and not match["value"]):
            if after is not None:
                found.append(after)
        elif word.startswith(long + "=") or match:
            prefix = word[: match.start("value")] if match else long + "="
            found.append(value.removeprefix(prefix))
    return found


def _outputs(command: Node, words: list[str], spelled: list[str]) -> set[str]:
    """The files a curl or wget COMMAND writes: from its output flag, from the
    URL's last segment (curl's `-O`, wget with no `-O`), in the directory its
    directory flag names, or from a stdout redirect."""
    program = next((p for w in words if (p := program_name(w)) in _OUTPUT_FLAGS), "")
    if program not in _OUTPUT_FLAGS:
        return set()
    named = set(_flag_values(words, spelled, *_OUTPUT_FLAGS[program]))
    remote = (
        any(_REMOTE_NAME.match(w) for w in words) if program == "curl" else not named
    )
    if remote:
        named |= {posixpath.basename(url) for url in spelled if _URL.match(url)}
    # Both tools honour the last directory flag only.
    directories = _flag_values(words, spelled, *_DIRECTORY_FLAGS[program])
    if directories:
        named = {
            name if name.startswith("/") or name == "-" else f"{directories[-1]}/{name}"
            for name in named
        }
    return named | _stdout_redirects(command)


def _placed(
    outputs: set[str], namespace: Namespace, moved: bool
) -> tuple[frozenset[str], frozenset[str]]:
    """OUTPUTS as paths a cache `path:` can name: absolute, under `~`, or
    relative to the workspace. The second set holds each relative output whose
    directory the text does not fix: the step's directory is an expression, a
    `cd` ran first (MOVED), or the path is a variable only the run knows."""
    placed, names = set(), set()
    moved = moved or namespace.cwd is None
    root = str(namespace.repo_root)
    for output in outputs:
        out = posixpath.normpath(_home(output))
        if out == root or out.startswith(root + "/"):
            placed.add(posixpath.relpath(out, root))
        elif out.startswith(("/", "~")):
            placed.add(out)
        elif moved or out.startswith("$"):
            names.add(out)
        else:
            cwd = posixpath.normpath(str(namespace.cwd))
            if cwd == root or cwd.startswith(root + "/"):
                cwd = posixpath.relpath(cwd, root)
            placed.add(posixpath.normpath(posixpath.join(cwd, out)))
    return frozenset(placed), frozenset(names)


def _stdout_redirects(command: Node) -> set[str]:
    """The files COMMAND's `>`/`>>` redirects write its stdout to."""
    parent = command.parent
    if parent is None or parent.type != "redirected_statement":
        return set()
    found = set()
    for child in parent.children:
        if child.type != "file_redirect":
            continue
        parts = redirect_parts(child)
        if parts.operator in (">", ">>", ">|") and parts.descriptor in (None, "1"):
            found.add(parts.destination)
    return found - {""}


class _Shell(NamedTuple):
    """What a namespace knows that every command in it shares: its downloader
    functions, the pins its digest checks read, and the base names of the
    files those checks name."""

    namespace: Namespace
    downloaders: frozenset[str]
    digest_pins: frozenset[str]
    checked_names: frozenset[str]


def _shell_context(namespace: Namespace) -> _Shell:
    checks = _digest_checks(namespace)
    pins: set[str] = set()
    for node, unit in checks:
        pins |= _pins_of([node], namespace, unit)
    return _Shell(
        namespace,
        _downloader_functions(namespace),
        frozenset(pins),
        frozenset(_checked_names(checks, namespace)),
    )


def _digest_pinned(url: str, outputs: frozenset[str], shell: _Shell) -> bool:
    """True when the namespace checks a digest that can be this download's: the
    URL is an http(s) address, and a check names the file the download writes.
    A download with no output the text can read (`curl … | jq`, or a call to a
    downloader function that writes where its body decides) pins nothing."""
    if not _URL.match(url):
        return False
    names = {posixpath.basename(output) for output in outputs}
    return bool(names & shell.checked_names)


def _command_fetches(command: Node, unit: Unit, shell: _Shell) -> list[Fetch]:
    """The pinned downloads COMMAND makes."""
    namespace = shell.namespace
    args = command_arguments(command)
    words = tokens(command)
    if not words or MESSAGE_PREFIX.match(words[0]):
        return []
    where = f" in {rel}" if (rel := _relative(unit.path)) else ""
    fetches = []
    for spec in _pinned_install(words, args):
        pins = _pins_of(
            [args[words.index(spec)]] if spec in words else [], namespace, unit
        )
        pins |= _literals(spec)
        fetches.append(
            Fetch(
                spec + where,
                frozenset(pins),
                frozenset(_sources_of(pins, namespace, unit)),
                frozenset(),
            )
        )
    if not _runs_downloader(command) and not shell.downloaders & set(words):
        return fetches
    values = [namespace.value(node, unit) for node in args]
    spelled = [_spelled(node, namespace, unit) for node in args]
    outputs, output_names = _placed(
        _outputs(command, words, spelled),
        namespace,
        namespace.moved_before(command, unit),
    )
    for node, token, value in zip(args, words, values):
        if _PINNED_URL.match(token) or (value and _PINNED_URL.match(value)):
            label, extra = token, set()
        elif _digest_pinned(value or token, outputs | output_names, shell):
            label, extra = f"{token} (digest-checked)", set(shell.digest_pins)
        else:
            continue
        pins = _pins_of([node], namespace, unit) | extra
        fetches.append(
            Fetch(
                label + where,
                frozenset(pins),
                frozenset(_sources_of(pins, namespace, unit)),
                outputs,
                output_names,
            )
        )
    return fetches


def _pinned_install(words: list[str], args: list) -> list[str]:
    """The pinned package specs of an install invocation in WORDS."""
    invocation = _find_install(words)
    if not invocation:
        return []
    family, index = invocation
    if family == NODE and not writes_outside_the_lockfile(words, index):
        return []  # a local install is covered by the lockfile and setup-node's cache
    pinned, unpinned = spec_scan(args[index:], family)
    if unpinned:
        return []
    # A spec that opens with an expansion (`"$@"`, `${ARGS[@]}`) names no
    # package the text fixes, so it is no download this lint can name.
    return [
        spec
        for spec in pinned
        if not _FLOATING.search(spec) and not spec.startswith("$")
    ]


# ── Python: what a followed file downloads ──────────────────────────────────


def _aliases(nodes: list[ast.AST]) -> dict[str, str]:
    """Each local name an import among NODES binds, mapped to the dotted name
    it means."""
    found: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    found[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    found[head] = head
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return found


def _qualified(func: ast.expr, aliases: dict[str, str]) -> str | None:
    dotted = name_of(func)
    if dotted is None:
        return None
    head, _, rest = dotted.partition(".")
    base = aliases.get(head, head)
    return f"{base}.{rest}" if rest else base


def _constants(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level `NAME = <expr>` bindings, for a URL held in a constant."""
    found: dict[str, ast.expr] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name):
                found[target.id] = statement.value
    return found


def _py_text(
    node: ast.expr | None, constants: dict[str, ast.expr], depth: int = 0
) -> str:
    """NODE as text a URL pattern can read: a literal as written, an f-string
    with each `{name}` as `${name}`, and a constant through its binding."""
    if node is None or depth > 3:
        return ""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            _py_text(part, constants, depth)
            if isinstance(part, ast.Constant)
            else "${" + (name_of(part.value) or "") + "}"
            for part in node.values
            if isinstance(part, (ast.Constant, ast.FormattedValue))
        )
    if isinstance(node, ast.Name) and node.id in constants:
        return _py_text(constants[node.id], constants, depth + 1)
    return ""


def _is_digest_method(node: ast.AST) -> bool:
    """True when NODE is a `.hexdigest()` or `.digest()` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _PY_HASH_METHODS
    )


def _is_hash_call(node: ast.AST, hashers: set[str]) -> bool:
    if _is_digest_method(node):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in hashers
    )


def _compares_hash(scope: ast.AST, hashers: set[str], aliases: dict[str, str]) -> bool:
    """True when SCOPE compares a hash: an `==`/`!=` with a hash call on one
    side, or a `compare_digest` fed one."""
    for node in ast.walk(scope):
        if isinstance(node, ast.Compare) and all(
            isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops
        ):
            operands = [node.left, *node.comparators]
        elif (
            isinstance(node, ast.Call)
            and _qualified(node.func, aliases) in _PY_DIGEST_COMPARES
        ):
            operands = list(node.args)
        else:
            continue
        if any(
            _is_hash_call(sub, hashers)
            for operand in operands
            for sub in ast.walk(operand)
        ):
            return True
    return False


def _called(scope: ast.AST, functions: dict[str, ast.AST]) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(scope)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in functions
    }


def _checks_digest(
    scope: ast.AST,
    functions: dict[str, ast.AST],
    hashers: set[str],
    aliases: dict[str, str],
) -> bool:
    """True when SCOPE, or a module function it calls at any depth, compares a hash."""
    pending, seen = [scope], set()
    while pending:
        current = pending.pop()
        if _compares_hash(current, hashers, aliases):
            return True
        for name in _called(current, functions) - seen:
            seen.add(name)
            pending.append(functions[name])
    return False


# A cheap shortlist ahead of the parse: a module that spells no fetcher's name
# holds no fetch call. The `ast` walk below is what decides.
_PY_FETCHER_SPELLING = re.compile(r"\b(?:urlopen|urlretrieve|requests|httpx)\b")


@functools.lru_cache(maxsize=1024)
def _source(path: Path) -> str:
    """PATH's text. A followed file that is not UTF-8 raises, as a followed
    shell file does: the lint cannot judge bytes it cannot read."""
    return path.read_text(encoding="utf-8")


@functools.lru_cache(maxsize=512)
def _parsed(path: Path) -> ast.Module | None:
    """PATH's tree, or None when it does not read as Python."""
    try:
        return ast.parse(_source(path))
    except (SyntaxError, ValueError):
        return None


@functools.lru_cache(maxsize=512)
def _python_fetches(path: Path) -> list[Fetch]:
    """The pinned downloads the Python file PATH makes, and those of each
    repo-local module it imports, one level down."""
    tree = _parsed(path)
    if tree is None:
        return []
    fetches = list(_module_fetches(path, None))
    for module in _local_imports(tree, path):
        fetches += _module_fetches(module, path)
    return fetches


@functools.lru_cache(maxsize=1024)
def _module_fetches(path: Path, importer: Path | None) -> list[Fetch]:
    """The pinned downloads written in the Python file PATH itself. IMPORTER is
    the file that imports it, whose hash a cache key may also name."""
    if not _PY_FETCHER_SPELLING.search(_source(path)):
        return []
    tree = _parsed(path)
    if tree is None:
        return []
    nodes = list(ast.walk(tree))
    aliases = _aliases(nodes)
    calls = [
        node
        for node in nodes
        if isinstance(node, ast.Call) and _qualified(node.func, aliases) in _PY_FETCHERS
    ]
    if not calls:
        return []
    constants = _constants(tree)
    functions: dict[str, ast.AST] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    hashers = {
        name
        for name, node in functions.items()
        if any(_is_digest_method(sub) for sub in ast.walk(node))
    }
    digests = {
        node.value
        for node in nodes
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _DIGEST_LITERAL.fullmatch(node.value)
    }
    sources = {rel for p in (path, importer) if (rel := _relative(p))}
    rel = _relative(path) or path.name
    # A fetch's scope is its innermost enclosing function. ast.walk is
    # breadth-first, so an inner function overwrites its outer one here. The
    # module scope holds only top-level statements, so a compare inside some
    # function never vouches for a fetch outside it.
    owner: dict[int, ast.AST] = {}
    for fn in nodes:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner.update((id(sub), fn) for sub in ast.walk(fn))
    top = ast.Module(
        body=[
            stmt
            for stmt in tree.body
            if not isinstance(
                stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            )
        ],
        type_ignores=[],
    )
    fetches = []
    for node in calls:
        scope = owner.get(id(node), top)
        scope_name = getattr(scope, "name", "<module>")
        url_node = (
            node.args[0]
            if node.args
            else next((k.value for k in node.keywords if k.arg == "url"), None)
        )
        url = _py_text(url_node, constants)
        pinned_url = bool(_PINNED_URL.match(url))
        if not pinned_url and not _checks_digest(scope, functions, hashers, aliases):
            continue
        names = set(re.findall(r"\$\{(?P<name>\w+)\}", url))
        values = {n: _py_text(constants[n], constants) for n in names if n in constants}
        pins = {n for n in names if _is_pin_name(n, values.get(n))}
        pins |= set(values.values()) | set(_VERSION_LITERAL.findall(url))
        pins |= set() if pinned_url else digests
        label = f"{url or 'a URL'} (fetched by {rel}:{scope_name}"
        label += ")" if pinned_url else ", digest-checked)"
        fetches.append(
            Fetch(
                label, frozenset(p for p in pins if p), frozenset(sources), frozenset()
            )
        )
    return fetches


def _local_imports(tree: ast.Module, path: Path) -> list[Path]:
    """The repo files PATH imports, found the way `python3 PATH` finds them."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    search = (path.parent, *sys_path_roots(path))
    found = []
    for name in sorted(names):
        for module in local_files(name, search):
            if (
                module.is_relative_to(REPO_ROOT)
                and module != path
                and module not in found
            ):
                found.append(module)
    return found


@functools.lru_cache(maxsize=1024)
def _namespace_fetches(namespace: Namespace) -> list[Fetch]:
    """Every pinned download NAMESPACE makes, computed once per namespace: a
    shared script is reached from many jobs."""
    shell = _shell_context(namespace)
    return [
        fetch
        for command, unit in namespace.executed
        for fetch in _command_fetches(command, unit, shell)
    ]


def _script_fetches(
    script: str,
    holder: str | None,
    action_dir: str | None,
    working_directory: str | None,
) -> list[Fetch]:
    """Every pinned download the `run:` body SCRIPT causes, through every repo
    file it reaches. HOLDER is the repo-relative workflow or action file that
    SCRIPT is written in."""
    reached = reach(script, REPO_ROOT, action_dir, working_directory)
    fetches: list[Fetch] = []
    for namespace in reached.namespaces:
        fetches += _namespace_fetches(namespace)
    for path in reached.python:
        fetches += _python_fetches(path)
    held = frozenset({holder} if holder else ())
    return [
        fetch._replace(sources=fetch.sources - {_RUN_BODY} | held)
        if _RUN_BODY in fetch.sources
        else fetch
        for fetch in fetches
    ]


# ── caches: which download a cache can hold ─────────────────────────────────


class CacheScope(NamedTuple):
    """A cache step's key, and the paths it saves, as written."""

    key: str
    paths: tuple[str, ...]


def _cache_scope(step: dict) -> CacheScope | None:
    """STEP's key and paths, or None when the lint cannot read them statically.
    A None scope covers every download in the job."""
    with_block = step.get("with")
    if not _action_name(step).startswith(_CACHE_ACTIONS) or not isinstance(
        with_block, dict
    ):
        return None
    key, path = with_block.get("key"), with_block.get("path")
    if not isinstance(key, str) or not isinstance(path, str) or "${{" in path:
        return None
    if not all(_READABLE_EXPRESSION.match(e) for e in _EXPRESSION.findall(key)):
        return None
    paths = tuple(
        posixpath.normpath(_home(line.strip()))
        for line in path.splitlines()
        if line.strip() and not line.strip().startswith("!")
    )
    return CacheScope(key, paths)


def _home(path: str) -> str:
    for spelling in ("${HOME}", "$HOME"):
        if path.startswith(spelling):
            return "~" + path[len(spelling) :]
    return path


@functools.lru_cache(maxsize=4096)
def _pin_in_key(pin: str) -> re.Pattern[str]:
    """PIN as it can appear in a cache key. A version or digest is a literal
    token (`tool-2.7.0`). A variable name is a reference to a value the job
    sets: `env.TOOL_VERSION`, `vars.…`, `matrix.…` or `inputs.…`. A bare word
    in a key is text, so `version` in `deps-version-1` names no variable."""
    if _IDENTIFIER.fullmatch(pin) and not _DIGEST_LITERAL.fullmatch(pin):
        return re.compile(rf"\b(?:env|vars|matrix|inputs)\.{re.escape(pin)}(?!\w)")
    return re.compile(rf"(?<![\w.])v?{re.escape(pin)}(?!\w|\.\w)")


@functools.lru_cache(maxsize=4)
def _shell_files(root: Path) -> tuple[str, ...]:
    """The repo-relative path of every `.sh` and `.bash` file under ROOT."""
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for suffix in (".sh", ".bash")
            for path in root.rglob(f"*{suffix}")
            if path.is_file()
        )
    )


@functools.lru_cache(maxsize=1024)
def _hashed_assignments(root: Path, pattern: str) -> frozenset[str]:
    """The variables the shell files a `hashFiles()` PATTERN matches assign. A
    key that hashes the file that sets a pin names that pin, even when the
    walk cannot tell which file a call like `source_pins "$1"` reads."""
    names: set[str] = set()
    if not pattern or pattern.startswith(("/", "!")):
        return frozenset()
    matcher = glob_to_regex(pattern)
    for rel in _shell_files(root):
        if not matcher.match(rel):
            continue
        try:
            tree = load_shell(root / rel)[1]
        except UnicodeDecodeError:
            continue  # bytes no shell step reads as a script set no pin
        names |= {name for name, _ in definitions(tree)[1]}
    return frozenset(names)


def _covers(scope: CacheScope, fetch: Fetch) -> bool:
    """True when SCOPE's key names FETCH's pin or a file that sets it, or its
    path holds FETCH's output."""
    if any(_pin_in_key(pin).search(scope.key) for pin in fetch.pins):
        return True
    for match in _HASHFILES.finditer(scope.key):
        patterns = [a or b for a, b in _QUOTED.findall(match["args"])]
        for pattern in patterns:
            pattern = pattern.removeprefix("./")
            if any(glob_to_regex(pattern).match(src) for src in fetch.sources):
                return True
            if fetch.pins & _hashed_assignments(REPO_ROOT, pattern):
                return True
    for out in fetch.outputs:
        for path in scope.paths:
            if (
                out == path
                or out.startswith(path + "/")
                or glob_to_regex(path).match(out)
            ):
                return True
    # A relative output in a directory the text does not fix: a cache path
    # that ends in it, or in one of its leading directories, can hold it.
    # `bin/tool` after a `cd` is held by `sub/bin`, but not by a bare `bin` or
    # `bin/tool`, which name the directory the shell left.
    return any(
        path.endswith("/" + head)
        for name in fetch.output_names
        for head in (name.rsplit("/", n)[0] for n in range(name.count("/") + 1))
        for path in scope.paths
    )


def _steps(container: object) -> list[dict]:
    steps = container.get("steps") if isinstance(container, dict) else None
    return (
        [step for step in steps if isinstance(step, dict)]
        if isinstance(steps, list)
        else []
    )


def _load(path: Path) -> dict | None:
    """The YAML of PATH with every mapping line-tagged, or None when it is not a
    mapping. A parse failure propagates to check_file, which reports it."""
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=LineLoader)
    return doc if isinstance(doc, dict) else None


def _action_path(uses: str) -> Path | None:
    """The definition file of a local composite action referenced by USES."""
    directory = REPO_ROOT / uses.removeprefix("./").rstrip("/")
    return next(
        (
            p
            for p in (directory / "action.yaml", directory / "action.yml")
            if p.is_file()
        ),
        None,
    )


class FlatStep(NamedTuple):
    """One step a job really runs: the step mapping, the workflow line to
    report it at, and the composite action file it came from (empty for a
    step written directly in the workflow)."""

    step: dict
    line: int
    origin: str


def flatten(
    steps: list[dict], anchor: int, origin: str, seen: frozenset
) -> list[FlatStep]:
    """Every step a job really runs, with the workflow line to report it at.

    A `uses: ./…` step is kept AND followed, because the composite action it names
    may hold both the cache and the install. A step that comes from an action keeps
    the ANCHOR line of the workflow step that reached it — the cache a finding asks
    for belongs to the job, which lives in the workflow — and carries ORIGIN, the
    action file, so the message can still say where the install is written. SEEN
    breaks a cycle between two actions that use each other.
    """
    flat: list[FlatStep] = []
    for step in steps:
        line = step.get("__line__", anchor) if not origin else anchor
        flat.append(FlatStep(step, line, origin))
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./"):
            continue
        target = _action_path(uses)
        if target is None or target in seen:
            continue
        doc = _load(target)
        if doc is not None:
            flat += flatten(
                _steps(doc.get("runs")), line, uses.rstrip("/"), seen | {target}
            )
    return flat


def check_file(path: Path) -> list[tuple[int, str]]:
    """(line, message) for every pinned download in an uncached job.

    A file this lint cannot parse — the workflow itself, or a composite action one
    of its jobs uses — is reported as a violation rather than passed as clean, so a
    syntax error can never read as "no uncached downloads here"."""
    try:
        return _check_parsed(path)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                1,
                f"could not parse this workflow or an action it uses as YAML "
                f"({first_line}); cannot check its jobs for uncached downloads — "
                "fix the syntax (or run actionlint) and re-check.",
            )
        ]


def _default_directory(doc: dict, job: dict) -> object:
    """The `defaults.run.working-directory` that applies to JOB: the job's own,
    else the workflow's, else the repo root."""
    for container in (job, doc):
        defaults = container.get("defaults")
        run = defaults.get("run") if isinstance(defaults, dict) else None
        if isinstance(run, dict) and "working-directory" in run:
            return run["working-directory"]
    return ""


def _directory(value: object) -> str | None:
    """A `working-directory` value relative to the repo root, or None when the
    text does not fix it (an expression, a non-string)."""
    if not isinstance(value, str) or "${{" in value:
        return None
    return value.removeprefix("./")


def _check_parsed(path: Path) -> list[tuple[int, str]]:
    doc = _load(path)
    jobs = doc.get("jobs") if doc else None
    if not isinstance(jobs, dict):
        return []
    blocks = _job_blocks(path.read_text(encoding="utf-8"))
    violations: list[tuple[int, str]] = []
    for name, job in jobs.items():
        steps = _steps(job)
        if not steps:
            continue  # a `uses:` job runs another workflow's steps, not its own
        key_line, block = blocks.get(str(name), (1, ""))
        flat = flatten(steps, key_line, "", frozenset({path}))
        # The job-scoped marker reads the surfaces a `#` may open a comment on:
        # a real YAML comment, and a `run:` value. A `#` inside any other scalar
        # is content. The step-scoped read below keeps the raw script, because
        # `flatten` also returns steps from a composite action in another file,
        # whose text this job's BLOCK does not hold.
        if any(annotated(line, OPT_OUT) for line in yaml_script_view(block)):
            continue
        scopes = [_cache_scope(step) for step, _, _ in flat if _caches(step)]
        if None in scopes:
            continue  # a cache the lint cannot read covers the whole job
        default_directory = _default_directory(doc, job)
        for step, line, origin in flat:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            if any(annotated(text, OPT_OUT) for text in script.splitlines()):
                continue
            action_dir = origin.removeprefix("./") if origin else None
            # `defaults.run` reaches only the workflow's own steps.
            directory = _directory(
                step.get("working-directory", "" if origin else default_directory)
            )
            holder = _relative(_action_path(origin) if origin else REPO_ROOT / path)
            uncovered = [
                fetch
                for fetch in _script_fetches(script, holder, action_dir, directory)
                if not any(_covers(scope, fetch) for scope in scopes)
            ]
            if not uncovered:
                continue
            what = ", ".join(sorted({fetch.what for fetch in uncovered})[:3])
            if origin:
                what += f" (installed by {origin})"
            why = NO_COVER if scopes else NO_CACHE
            violations.append((line, MESSAGE.format(job=name, what=what, why=why)))
    return violations


def main() -> int:
    total = 0
    # Only workflows are entry points: a composite action has no cache scope of its
    # own, so it is judged inside each job that uses it, where `flatten` reaches it.
    # `actions_dir` stays None for that reason. The shared discovery says so on
    # stderr when it finds no workflow, which keeps an unscanned tree apart from a
    # real pass — both exit 0.
    for path in workflow_files(WORKFLOWS_DIR):
        rel = path.relative_to(REPO_ROOT)
        try:
            findings = check_file(path)
        except PathologicalInputError as err:
            # A shape the grammar cannot parse safely fails LOUDLY: skipping it
            # would false-green exactly the input this lint exists to read.
            print(f"::error file={rel}::{err}")
            total += 1
            continue
        for line, message in findings:
            print(f"::error file={rel},line={line}::{message}")
            total += 1

    if total:
        print(f"\nERROR: {total} violation(s) found.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

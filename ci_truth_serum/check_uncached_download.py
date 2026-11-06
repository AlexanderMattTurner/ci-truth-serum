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
repo this lint came out of: a test shard had a 15-minute budget. One `npm
install -g` of a pinned package used 7.9 of those minutes, because the registry
was slow that hour. Three shards went "cancelled" at once. Nobody reads
"cancelled" as "your uncached download ran long", so the hunt went to the tests.
A cache keyed on the pinned version turns that 7.9 minutes into seconds.

A download is pinned in one of three ways:

  * the URL or the package spec names a fixed release (`/v2.7.0/`, `==0.14.0`,
    or a `*VERSION*` variable);
  * the same shell process checks the download against a digest. The check is
    a `sha256sum -c` command, or a test that compares the hash of the file
    (`[[ "$(sha256sum <f)" == "$SHA" ]]`). The tool can sit in an array or in
    a function. A digest that the run downloads changes with the bytes, so a
    comparison with it is no check. In Python, the same function hashes the
    bytes and compares the hash. A job that refuses every other byte has
    immutable input by its own rule;
  * the step calls a function whose body downloads a file and checks the
    digest of that file, directly or through a wrapper. The outermost call
    that carries a pin is the fetch, because it names the tool and its
    version.

The lint reads more than the `run:` text. `_cts_step_reach` follows each repo
script the step runs or sources, and each function call into that function's
body. A wrapper in front of a script (`timeout 900 bash setup.bash`) does not
hide it. It also records a Python file the step hands an interpreter, which this
module parses with `ast`. A path that the text does not fix ends the walk with
no finding, because a guessed path judges a file the step never runs. Measured
on agent-glovebox: both osv-scanner jobs download a pinned binary on every run
through a two-file chain, and only a walk that follows scripts sees it.

A cache in the job covers a download only when the cache can hold it:

  * its `key:` names the pin. That is the version, the digest, the version
    variable as `env.NAME`, or a `hashFiles()` of a file that holds the fetch,
    assigns its pin, or is read to compute its pin (`$(jq -r .v pins.json)`);
  * its `path:` holds the file the download writes, a path a later `cp`,
    `install` or `mv` copies it to, or an absolute path the step hands a
    downloader function;
  * it is the store of the installer: `~/.cache/pip` for pip, or the cache of
    a setup action whose cache input names the installer (`cache: pnpm`).

A variable is a pin only when its name says so (`*VERSION*`, `*SHA*`, …), or
when the text gives it a fixed version or digest. Thus a key that names
`env.ARCH` does not cover a download whose URL also reads `$ARCH`.

`_cts_cache_scope` reads each cache step's key and path, and the installers a
setup action caches. A key with a value it cannot read could name the pin, so
that cache covers every download in the job. Local composite actions
(`uses: ./.github/actions/foo`) are inlined first.

A fetcher can check a cache directory before it downloads. The `actions/cache`
step that restores that directory then covers it, when the key of that step
names the pin or hashes the fetcher. The lint cannot read where a Python fetch
writes, so a `path:` alone does not cover one.

Deliberately NOT reported, because the download is the point or has no stable key:

  * an install with any unpinned spec — `check-versionless-install` owns that, and
    a floating install has no cache key that could stay correct;
  * a `docker pull`;
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
from typing import NamedTuple, Protocol

import yaml
from tree_sitter import Node

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_bash_ast import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    PathologicalInputError,
    _inside_definition,
    command_arguments,
    is_lookup,
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
from _cts_cache_scope import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    ANY_INSTALLER,
    CacheScope,
    Context,
    Text,
    cache_scope,
    env_of,
    home,
    rendered,
)
from _cts_step_reach import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    Namespace,
    Unit,
    _launched,
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
    _PIP_ALIAS,
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

# The install flags that keep an installer from writing its store.
_NO_STORE_FLAGS = frozenset({"--no-cache-dir", "--no-cache"})
# Where each installer keeps the files it downloads. A cache whose path holds
# this directory holds the install's bytes. `~` is the home directory of the
# job, which is one of _HOMES.
_INSTALLER_STORES = {
    "pip": "~/.cache/pip",
    "pipx": "~/.cache/pip",
    "uv": "~/.cache/uv",
    "npm": "~/.npm",
    "pnpm": "~/.local/share/pnpm/store",
    "yarn": "~/.cache/yarn",
    "apt-get": "/var/cache/apt/archives",
    "apt": "/var/cache/apt/archives",
    "aptitude": "/var/cache/apt/archives",
}
# The home directories a GitHub job runs in: the hosted runner's user, root,
# and the home a container job gets.
_HOMES = ("~", "/home/runner", "/root", "/github/home")

# A version that is not a version: the registry still resolves it fresh, so the
# download has no stable cache key and this lint must leave it alone.
_FLOATING = re.compile(r"[=@](?:latest|next|stable|\*|main|master)$", re.IGNORECASE)

_DOWNLOADERS = frozenset({"curl", "wget"})
# The nodes whose body runs only on some runs: a `list` is `a && b` or `a || b`.
_CONDITIONAL_NODES = frozenset(
    {
        "if_statement",
        "elif_clause",
        "else_clause",
        "case_item",
        "while_statement",
        "for_statement",
        "c_style_for_statement",
    }
)
# The commands that copy a file's bytes to another path.
_COPIERS = frozenset({"cp", "install", "mv"})
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
# The nodes a checksum command's output passes through on its way to a
# comparison or an assignment, and the operators that compare it.
_HASH_OUTPUT_PATH = frozenset(
    {
        "redirected_statement",
        "pipeline",
        "command_substitution",
        "string",
        "concatenation",
    }
)
_COMPARE_OPERATORS = frozenset({"==", "=", "!="})

# The short flags of curl and wget that take no value. Only these may come
# before a value flag in one word: in `-Ttool.tgz` the letters after `-T`
# are its value, so the word holds no `-o`.
_CURL_BARE = "fsSLkqvIiOjJgN46l0123nRpZ#GBaMV:"
_WGET_BARE = "qvcNrkKmpbxSdF46EHLV"
# curl's and wget's flags that name the file the download writes. A short flag
# closes a cluster (`-fsSLo FILE`) or carries its value (`-o/tmp/t`, `-qO-`).
_OUTPUT_FLAGS = {
    "curl": (re.compile(rf"^-[{_CURL_BARE}]*o(?P<value>.*)$"), "--output"),
    "wget": (re.compile(rf"^-[{_WGET_BARE}]*O(?P<value>.*)$"), "--output-document"),
}
# curl's `-O` writes the URL's last path segment. The letters after it do
# not matter: `-OH x` still writes that file.
_REMOTE_NAME = re.compile(rf"^(?:-[{_CURL_BARE}]*O|--remote-name$)")
# curl's and wget's flags that name the directory the file lands in.
_DIRECTORY_FLAGS = {
    "curl": (None, "--output-dir"),
    "wget": (re.compile(rf"^-[{_WGET_BARE}]*P(?P<value>.*)$"), "--directory-prefix"),
}
# The source that stands for the workflow or action file a `run:` body is
# written in, until `_script_fetches` knows which file that is.
_RUN_BODY = "<run body>"

# The bash nodes whose value a command computes.
_COMPUTED_WORDS = frozenset(
    {"command_substitution", "process_substitution", "arithmetic_expansion"}
)

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
    relative paths the bytes land at in a directory the text does not fix.
    INSTALLER is the package manager that makes the fetch, empty for a
    download."""

    what: str
    pins: frozenset[str]
    sources: frozenset[str]
    outputs: frozenset[str]
    output_names: frozenset[str] = frozenset()
    installer: str = ""


# ── shell: what a namespace downloads ───────────────────────────────────────


def _relative(path: Path | None) -> str | None:
    if path is None or not path.is_relative_to(REPO_ROOT):
        return None
    return path.relative_to(REPO_ROOT).as_posix()


def _callees(namespace: Namespace) -> dict[str, frozenset[str]]:
    """Each function the namespace defines, mapped to the functions its body
    may call."""
    defined = namespace.functions.keys()
    return {
        name: frozenset(
            word
            for command, _ in namespace.function_commands(name)
            for word in callable_words(command)
            if word in defined
        )
        for name in namespace.functions
    }


def _callers_of(
    found: set[str] | frozenset[str], callees: dict[str, frozenset[str]]
) -> frozenset[str]:
    """FOUND, and each function that calls one of them at any depth."""
    found = set(found)
    changed = True
    while changed:
        before = len(found)
        found |= {name for name, called in callees.items() if called & found}
        changed = len(found) != before
    return frozenset(found)


class _NodeTest(Protocol):
    """A question about one syntax node."""

    def __call__(self, node: Node, /) -> bool: ...


def _functions_running(
    namespace: Namespace, callees: dict[str, frozenset[str]], runs: _NodeTest
) -> frozenset[str]:
    """The functions whose bodies run a command that RUNS accepts, directly or
    through another such function. With `_runs_downloader`,
    `pinned_curl() { curl … "$url"; }` is one."""
    seeds = {
        name
        for name in namespace.functions
        if any(runs(command) for command, _ in namespace.function_commands(name))
    }
    return _callers_of(seeds, callees)


def _ancestor(node: Node, kind: str) -> Node | None:
    """The innermost node of type KIND above NODE, if any."""
    scope = node.parent
    while scope is not None and scope.type != kind:
        scope = scope.parent
    return scope


def _enclosing_function(node: Node) -> str | None:
    """The name of the innermost function whose body holds NODE, if any."""
    scope = _ancestor(node, "function_definition")
    name = scope.child_by_field_name("name") if scope is not None else None
    return node_text(name) if name is not None else None


def _downloads(command: Node, downloaders: frozenset[str]) -> bool:
    """True when COMMAND runs curl or wget, or calls a function DOWNLOADERS
    names."""
    return _runs_downloader(command) or bool(set(callable_words(command)) & downloaders)


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


def _followed(nodes: list[Node], namespace: Namespace) -> set[str]:
    """The variables NODES expand, and each variable an assignment of one of
    them expands, to a fixed point."""
    names: set[str] = set()
    pending = [name for node in nodes for name in _expanded_names(node)]
    while pending:
        name = pending.pop()
        if name in names:
            continue
        names.add(name)
        for assignment, _ in namespace.assignments.get(name, []):
            pending += _expanded_names(assignment)
    return names


def _pins_of(nodes: list[Node], namespace: Namespace, unit: Unit) -> set[str]:
    """The words a cache key may carry to name what NODES pin: each version or
    digest written in them, and each pin variable they expand. A variable
    assigned from another one adds that one too, so `version="${TOOL_VERSION#v}"`
    lets a key name `TOOL_VERSION`."""
    names = _followed(nodes, namespace)
    values = {name: namespace.variable(name, unit) for name in names}
    pins = {name for name, value in values.items() if _is_pin_name(name, value)}
    texts = [node_text(node) for node in nodes]
    texts += [namespace.value(node, unit) or "" for node in nodes]
    texts += [value or "" for value in values.values()]
    for text in texts:
        pins |= _literals(text)
    return pins


def _sources_of(
    nodes: list[Node], pins: set[str], namespace: Namespace, unit: Unit
) -> set[str]:
    """The files a `hashFiles()` key may hash to name the fetch that NODES
    spell: the file that holds it, each file that assigns one of its PINS, and
    each file a variable it uses is read from. `_RUN_BODY` stands for the
    workflow or action file when that file is one of them."""
    paths = {unit.path} if unit.path else set()
    for pin in pins:
        paths |= namespace.assigned_in(pin)
    sources = {rel for path in paths if (rel := _relative(path))}
    sources |= _read_globs(_followed(nodes, namespace) | pins, namespace)
    in_body = unit.path is None or any(
        owner.path is None
        for pin in pins
        for _, owner in namespace.assignments.get(pin, [])
    )
    return sources | {_RUN_BODY} if in_body else sources


def _read_globs(names: set[str], namespace: Namespace) -> set[str]:
    """The files the values of NAMES are read from, as globs relative to the
    repo root. `v="$(jq -r .version "$dir/$tool.json")"` reads
    `<dir>/*.json`: a part the text does not fix reads as `*`. A word the text
    fixes no part of could be any file, so it is left out, and so is a flag.
    A relative path lands in the reading command's directory, or in any
    directory (`**/`) when the text does not fix that."""
    globs = set()
    for name in names:
        for assignment, owner in namespace.assignments.get(name, []):
            value = assignment.child_by_field_name("value")
            for command, words in _reads(value):
                directory = namespace.directory_at(command, owner) if command else None
                for word in words:
                    spelled = _spelled(word, namespace, owner, unknown="*")
                    if spelled.startswith("-") or not spelled.replace("*", "").strip(
                        "/"
                    ):
                        continue
                    if rel := _read_glob(spelled, directory):
                        globs.add(rel)
    return globs


def _reads(value: Node | None) -> list[tuple[Node | None, list[Node]]]:
    """Each command in VALUE with the words that may name a file it reads, its
    arguments and its `<` targets. A `$(<f)` runs no command, so it pairs
    None with its target."""
    if value is None:
        return []
    found: list[tuple[Node | None, list[Node]]] = [
        (command, _file_words(command, ("<",), "0"))
        for command in iter_nodes(value, "command")
    ]
    for substitution in iter_nodes(value, "command_substitution"):
        if targets := _substituted_files(substitution):
            found.append((None, targets))
    return found


def _substituted_files(substitution: Node) -> list[Node]:
    """The file a `$(<f)` SUBSTITUTION reads, which runs no command."""
    return [
        target
        for child in substitution.named_children
        if child.type == "file_redirect"
        and (target := child.child_by_field_name("destination")) is not None
    ]


def _read_glob(spelled: str, directory: Path | None) -> str | None:
    """The repo-relative glob of the file SPELLED names, read from DIRECTORY,
    or None when it lies outside the repo."""
    if spelled.startswith(("/", "~", "$")):
        return _relative(Path(posixpath.normpath(spelled)))
    if directory is None:
        return "**/" + posixpath.normpath(spelled)
    return _relative(Path(posixpath.normpath(posixpath.join(str(directory), spelled))))


def _array_programs(word: Node, namespace: Namespace) -> set[str] | None:
    """The programs a command word that expands one array may name:
    `"${SHA_ARGV[@]}"` names `sha256sum` when each assignment of `SHA_ARGV` is
    an array that starts with it. A `+=` only appends options, so it names no
    program. None when the word is anything else."""
    if word.type == "string" and len(word.named_children) == 1:
        word = word.named_children[0]
    if word.type not in ("simple_expansion", "expansion"):
        return None
    names = list(iter_nodes(word, "variable_name"))
    assignments = namespace.assignments.get(node_text(names[0]), []) if names else []
    programs = set()
    for assignment, _ in assignments:
        if any(child.type == "+=" for child in assignment.children):
            continue
        value = assignment.child_by_field_name("value")
        if value is None or value.type != "array" or not value.named_children:
            return None
        programs.add(program_name(node_text(value.named_children[0])))
    return programs or None


def _runs_digest_tool(command: Node, namespace: Namespace) -> bool:
    """True when COMMAND runs a checksum tool: by name, or through an array
    that holds the tool and its options."""
    args = command_arguments(command)
    if not args:
        return False
    if _DIGEST_TOOLS.fullmatch(program_name(tokens(command)[0])):
        return True
    programs = _array_programs(args[0], namespace)
    return bool(programs) and all(_DIGEST_TOOLS.fullmatch(p) for p in programs)


class _Floats:
    """Asks whether a value comes from a download. A digest that the run
    downloads (`want="$(curl …/latest/t.sha256)"`, `$(cat t.sha256)` after a
    curl writes that file, or `$(gh api …)`) changes with the bytes it vouches
    for, so a check against it pins nothing. A digest read from a committed
    file (`want="$(pinned_sha256 kata)"`) is fixed by the tree, and stays a
    pin. A variable is followed through each assignment it takes its value
    from, so `sha="${line%% *}"` floats when `line` does. A variable that is
    only an argument inside a substitution (`$(pinned_sha256 "$tool")`) is
    no part of the value, so it is not followed."""

    def __init__(self, namespace: Namespace, downloaders: frozenset[str]):
        self.namespace = namespace
        self.downloaders = downloaders
        # A command no walk runs (in a function nothing calls) reads nothing.
        self.units = {command.id: unit for command, unit in namespace.executed}
        # Only curl and wget count: an argument of a downloader function
        # (`fetch_verified kata …`) names a tool as often as a file. A name
        # only the run decides matches by the variable that holds it
        # (`-o "$sums"` and `grep x "$sums"`), and a positional parameter
        # matches nothing, since `$2` in two functions is two values.
        self.downloaded = {
            name
            for name in _written_names(namespace.executed, namespace, frozenset())
            if not name.startswith("$")
        } | {
            name
            for command, _ in namespace.executed
            if _runs_downloader(command)
            for name in _held_variables(_output_words(command))
        }

    def __call__(self, node: Node, unit: Unit) -> bool:
        """True when NODE, read in UNIT, holds a value a download gives."""
        values, followed, pending = [(node, unit)], set(), _direct_names(node)
        while pending:
            name = pending.pop()
            followed.add(name)
            for assignment, owner in self.namespace.assignments.get(name, []):
                value = assignment.child_by_field_name("value")
                if value is not None:
                    values.append((value, owner))
                    pending |= _direct_names(value) - followed
        return any(
            self._substitution_reads(substitution, owner)
            for value, owner in values
            for substitution in iter_nodes(value, "command_substitution")
        )

    def _substitution_reads(self, substitution: Node, unit: Unit) -> bool:
        """True when SUBSTITUTION reads a download: through a command it runs,
        or as `$(<f)`, which runs none and reads F."""
        files = _substituted_files(substitution)
        return self._names_a_download(files, unit) or any(
            self._reads_a_download(command, owner)
            for command in iter_nodes(substitution, "command")
            if (owner := self.units.get(command.id)) is not None
        )

    def _reads_a_download(self, command: Node, unit: Unit) -> bool:
        """True when COMMAND downloads, asks a remote, or reads a file a
        download writes."""
        if _downloads(command, self.downloaders) or _asks_a_remote(command):
            return True
        return self._names_a_download(_file_words(command, ("<",), "0"), unit)

    def _names_a_download(self, nodes: list[Node], unit: Unit) -> bool:
        """True when one of NODES names a file a download writes. A URL names
        no file on disk, though its last segment may match one."""
        files = [
            node
            for node in nodes
            if not _URL.match(_spelled(node, self.namespace, unit))
        ]
        names = _base_names(files, self.namespace, unit) | _held_variables(files)
        return bool(names & self.downloaded)

    def feeds(self, checker: Node, scope: Node, unit: Unit) -> bool:
        """True when the digests the `sha256sum -c` CHECKER reads come from a
        download. SCOPE is the pipeline or redirected statement around it.
        They do when the checksum file it names is one a download writes,
        when a command that pipes into it reads a download, or when a printed
        line or a here-string carries a value that floats. A printed line
        names the file it checks too, so only its values are followed."""
        if self._names_a_download(_file_words(checker, ("<",), "0"), unit):
            return True
        for command in iter_nodes(scope, "command"):
            words = tokens(command)
            if command.id == checker.id or not words:
                continue
            if MESSAGE_PREFIX.match(words[0]):
                if any(self(arg, unit) for arg in command_arguments(command)[1:]):
                    return True
            elif self._reads_a_download(command, unit):
                return True
        return any(
            self(child, unit)
            for here in iter_nodes(scope, "herestring_redirect")
            for child in here.named_children
        )


def _file_words(
    command: Node, operators: tuple[str, ...], descriptor: str
) -> list[Node]:
    """COMMAND's argument words and the targets of its redirects with one of
    OPERATORS on DESCRIPTOR: the words that may name a file it touches."""
    return command_arguments(command)[1:] + _redirect_targets(
        command, operators, descriptor
    )


def _output_words(command: Node) -> list[Node]:
    """The words that name the file the curl or wget COMMAND writes: the value
    after a separate output flag (`-o "$sums"`), and each stdout redirect
    target. A value attached to its flag is never one whole variable."""
    words = tokens(command)
    program = next((p for w in words if (p := program_name(w)) in _OUTPUT_FLAGS), "")
    if not program:
        return []
    short, long = _OUTPUT_FLAGS[program]
    args = command_arguments(command)
    found = [
        node
        for flag, node in zip(words, args[1:])
        if flag == long or ((match := short.match(flag)) and not match["value"])
    ]
    return found + _redirect_targets(command, _WRITE_OPERATORS, "1")


def _direct_names(node: Node) -> set[str]:
    """The variables NODE expands outside every command substitution in it:
    the ones its value is made of."""
    names = set()
    for expansion in iter_nodes(node, "simple_expansion", "expansion"):
        between, parent = [], expansion.parent
        while parent is not None and parent.id != node.id:
            between.append(parent.type)
            parent = parent.parent
        if "command_substitution" not in [*between, node.type]:
            names |= _expanded_names(expansion)
    return names


def _held_variables(nodes: list[Node]) -> set[str]:
    """`$NAME` for each of NODES that is one whole named variable (`"$sums"`,
    `${sums}`). Positional and special parameters are left out."""
    held = set()
    for node in nodes:
        if node.type == "string" and len(node.named_children) == 1:
            node = node.named_children[0]
        names = list(iter_nodes(node, "variable_name"))
        plain = node.type in ("simple_expansion", "expansion") and len(names) == 1
        if plain and node_text(node) in (
            f"${node_text(names[0])}",
            f"${{{node_text(names[0])}}}",
        ):
            held.add(f"${node_text(names[0])}")
    return held


def _asks_a_remote(command: Node) -> bool:
    """True when COMMAND reads a remote: every `gh` subcommand asks GitHub,
    and `git ls-remote` asks a remote which commit a ref names now."""
    words = _launched(tokens(command))
    name = program_name(words[0] or "") if words else ""
    return name == "gh" or (name == "git" and "ls-remote" in words[1:])


def _against_a_value(expression: Node, hashed: _NodeTest, floats: _NodeTest) -> bool:
    """True when EXPRESSION compares, with `==`, `=` or `!=`, a side that
    HASHED accepts against a fixed value. `!= ""` accepts every file a
    checksum tool prints a line for, and a value that FLOATS changes with
    the download, so neither is a digest check."""
    operator = expression.child_by_field_name("operator")
    if operator is None or operator.type not in _COMPARE_OPERATORS:
        return False
    left = expression.child_by_field_name("left")
    right = expression.child_by_field_name("right")
    if left is None or right is None:
        return False
    return any(
        hashed(side) and unquote(node_text(other)) != "" and not floats(other)
        for side, other in ((left, right), (right, left))
    )


def _compares(test: Node, name: str, floats: _NodeTest) -> bool:
    """True when the `[[ ]]` or `[ ]` TEST compares the variable NAME with a
    fixed value."""
    return any(
        _against_a_value(expression, lambda side: name in _expanded_names(side), floats)
        for expression in iter_nodes(test, "binary_expression")
    )


def _comparison_scopes(command: Node, unit: Unit, floats: _NodeTest) -> list[Node]:
    """The nodes that compare what the hashing COMMAND prints, with the node
    that names its input file first. Two shapes count:
    `[[ "$(sha256sum <f)" == "$want" ]]` comes back as its test, and
    `got="$(sha256sum f)"` comes back as that assignment and each test that
    compares `$got` and runs where the assignment runs: in the same function
    body, or at the top level of the same file."""
    node, substituted = command, False
    while node.parent is not None and node.parent.type in _HASH_OUTPUT_PATH:
        node = node.parent
        substituted |= node.type == "command_substitution"
    parent = node.parent
    if not substituted or parent is None:
        return []
    if parent.type == "binary_expression":
        test = _ancestor(node, "test_command")
        compared = _against_a_value(parent, lambda side: side.id == node.id, floats)
        return [test] if test is not None and compared else []
    if parent.type != "variable_assignment":
        return []
    name = node_text(parent.child_by_field_name("name"))
    region = _ancestor(parent, "function_definition")
    scope = region.child_by_field_name("body") if region is not None else unit.root
    tests = [
        test
        for test in iter_nodes(scope, "test_command")
        if not _inside_definition(test, scope) and _compares(test, name, floats)
    ]
    return [parent, *tests] if tests else []


def _digest_checks(
    namespace: Namespace, hashers: frozenset[str], floats: _Floats
) -> list[tuple[Node, Unit]]:
    """The digest checks the namespace runs, each as the nodes that hold its
    digest and its file name:
      * a `sha256sum -c` style command, as the pipeline or redirected statement
        around it, since the digest and the file name usually arrive on stdin;
      * a comparison of what a checksum tool, or a function HASHERS names,
        prints for a file with a value that does not FLOAT
        (`_comparison_scopes` lists the two shapes)."""
    found = []
    for command, unit in namespace.executed:
        words = tokens(command)
        if not words:
            continue
        tool = _runs_digest_tool(command, namespace)
        if tool and any(_CHECK_FLAG.match(word) for word in words[1:]):
            scope = command
            while scope.parent is not None and scope.parent.type in _CHECK_SCOPES:
                scope = scope.parent
            if not floats.feeds(command, scope, unit):
                found.append((scope, unit))
        elif tool or words[0] in hashers:
            floats_here = functools.partial(floats, unit=unit)
            found += [
                (scope, unit)
                for scope in _comparison_scopes(command, unit, floats_here)
            ]
    return found


def _checked_names(checks: list[tuple[Node, Unit]], namespace: Namespace) -> set[str]:
    """The base names of the files the digest checks name, as far as the text
    fixes them, from argument words, here-strings and stdin redirects:
    `echo "$SHA  tool" | sha256sum -c -` names `tool`,
    `sha256sum -c tool.tgz.sha256` names `tool.tgz` through its checksum file,
    and `[[ "$(sha256sum <tool)" == "$SHA" ]]` names `tool`."""
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
        nodes += [
            destination
            for redirect in iter_nodes(scope, "file_redirect")
            if redirect_parts(redirect).operator == "<"
            and (destination := redirect.child_by_field_name("destination"))
        ]
        names |= {
            name
            for base in _base_names(nodes, namespace, unit)
            for name in (base, _CHECKSUM_SUFFIX.sub("", base))
        }
    return names


def _base_names(nodes: list[Node], namespace: Namespace, unit: Unit) -> set[str]:
    """The base names of the words NODES spell, minus options and stdin."""
    return {
        posixpath.basename(word)
        for node in nodes
        for word in _spelled(node, namespace, unit).split()
        if not word.startswith("-")
    }


def _spelled(
    node: Node,
    namespace: Namespace,
    unit: Unit,
    following: frozenset[str] = frozenset(),
    unknown: str | None = None,
) -> str:
    """NODE's value when the text fixes it. Otherwise its text after quote
    removal. A variable with one assignment spells that assignment, so
    `D="$HOME/bin"` makes `$D/tool` read `$HOME/bin/tool`. Any other expansion
    is spelled `$NAME`, so one variable reads the same in an output flag and
    in a here-string, or UNKNOWN when it is set. FOLLOWING holds the names
    being spelled, to stop a cycle."""
    value = namespace.value(node, unit)
    if value is not None:
        return value
    if node.type in ("simple_expansion", "expansion"):
        name_node = next(iter_nodes(node, "variable_name"), None)
        if name_node is None:
            return node_text(node) if unknown is None else unknown
        name = node_text(name_node)
        assignments = namespace.assignments.get(name, [])
        default = _unset_default(node)
        if default is not None and not assignments:
            return "".join(
                _spelled(part, namespace, unit, following, unknown) for part in default
            )
        plain = node_text(node).strip() in (f"${name}", f"${{{name}}}")
        if plain and name not in following and len(assignments) == 1:
            assignment, owner = assignments[0]
            assigned = assignment.child_by_field_name("value")
            if assigned is not None:
                return _spelled(assigned, namespace, owner, following | {name}, unknown)
        return f"${name}" if unknown is None else unknown
    if unknown is not None and node.type in _COMPUTED_WORDS:
        return unknown
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
        pieces += [_spelled(child, namespace, unit, following, unknown)]
        start = child.end_byte - base
    pieces.append(raw[start:end].decode())
    return "".join(pieces)


def _unset_default(expansion: Node) -> list[Node] | None:
    """The parts of the default in `${X:-default}` or `${X-default}`, or None
    for any other expansion. The shell uses the default when no file assigns X."""
    inner = expansion.children[1:-1]
    if (
        len(inner) < 2
        or inner[0].type != "variable_name"
        or node_text(inner[1]) not in (":-", "-")
    ):
        return None
    return inner[2:]


def _flag_values(words: list[str], spelled: list[str], short, long: str) -> list[str]:
    """The values WORDS give the flag spelled SHORT (a pattern with a `value`
    group, or None) or LONG, in order: `-o FILE`, `-o/tmp/t`, `--output FILE`
    and `--output=FILE`. Each value comes from SPELLED, the words' values."""
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
    outputs: set[str], root_path: Path, directory: Path | None
) -> tuple[frozenset[str], frozenset[str]]:
    """OUTPUTS as paths a cache `path:` can name: absolute, under `~`, or
    relative to the workspace ROOT_PATH. A relative output lands in DIRECTORY.
    The second set holds each relative output whose directory the text does
    not fix: DIRECTORY is None, or the path is a variable only the run knows."""
    placed, names = set(), set()
    root = str(root_path)
    for output in outputs:
        out = posixpath.normpath(home(output))
        if out == root or out.startswith(root + "/"):
            placed.add(posixpath.relpath(out, root))
        elif out.startswith(("/", "~")):
            placed.add(out)
        elif directory is None or out.startswith("$"):
            names.add(out)
        else:
            cwd = posixpath.normpath(str(directory))
            if cwd == root or cwd.startswith(root + "/"):
                cwd = posixpath.relpath(cwd, root)
            placed.add(posixpath.normpath(posixpath.join(cwd, out)))
    return frozenset(placed), frozenset(names)


_WRITE_OPERATORS = (">", ">>", ">|")


def _redirect_targets(
    command: Node, operators: tuple[str, ...], descriptor: str
) -> list[Node]:
    """The files COMMAND's redirects name, for each redirect with one of
    OPERATORS on the stream DESCRIPTOR names (that number, or none written):
    `<f` reads stdin ("0") and `>f` writes stdout ("1")."""
    parent = command.parent
    if parent is None or parent.type != "redirected_statement":
        return []
    return [
        target
        for child in parent.children
        if child.type == "file_redirect"
        and (parts := redirect_parts(child)).operator in operators
        and parts.descriptor in (None, descriptor)
        and (target := child.child_by_field_name("destination")) is not None
    ]


def _stdout_redirects(command: Node) -> set[str]:
    """The files COMMAND's `>`/`>>` redirects write its stdout to."""
    found = {
        unquote(node_text(target))
        for target in _redirect_targets(command, _WRITE_OPERATORS, "1")
    }
    return found - {""}


class _Shell(NamedTuple):
    """What a namespace knows that every command in it shares: the functions
    each function calls, its downloader functions, the functions whose own
    body downloads a file and checks its digest, those functions with every
    function that calls them, the pins its digest checks read (all of them,
    and by the function whose body holds the check), and the base names of
    the files those checks name."""

    namespace: Namespace
    callees: dict[str, frozenset[str]]
    downloaders: frozenset[str]
    verifiers: frozenset[str]
    checked_downloaders: frozenset[str]
    digest_pins: frozenset[str]
    function_digest_pins: dict[str, frozenset[str]]
    checked_names: frozenset[str]


def _written_names(
    commands: list[tuple[Node, Unit]], namespace: Namespace, downloaders: frozenset[str]
) -> set[str]:
    """The base names of the files the downloads among COMMANDS may write:
    each argument word of a command that runs curl or wget or calls a
    function DOWNLOADERS names, and each file its stdout goes to. They are
    spelled as `_checked_names` spells a check's files, so the two compare."""
    return {
        name
        for command, unit in commands
        if _downloads(command, downloaders)
        for name in _base_names(
            command_arguments(command)[1:]
            + _redirect_targets(command, _WRITE_OPERATORS, "1"),
            namespace,
            unit,
        )
    }


def _shell_context(namespace: Namespace) -> _Shell:
    callees = _callees(namespace)
    downloaders = _functions_running(namespace, callees, _runs_downloader)
    hashers = _functions_running(
        namespace, callees, lambda command: _runs_digest_tool(command, namespace)
    )
    checks = _digest_checks(namespace, hashers, _Floats(namespace, downloaders))
    pins: set[str] = set()
    by_function: dict[str, frozenset[str]] = {}
    checked: dict[str, set[str]] = {}
    for node, unit in checks:
        found = _pins_of([node], namespace, unit)
        pins |= found
        name = _enclosing_function(node)
        if name is None:
            continue
        by_function[name] = by_function.get(name, frozenset()) | found
        if name in downloaders:
            checked.setdefault(name, set()).update(
                _checked_names([(node, unit)], namespace)
            )
    # The check names a file that a download in the same body writes, so a
    # check of some other file, in that body or a sibling, pins nothing. Every
    # caller of such a function also downloads, so the closure stays inside
    # DOWNLOADERS.
    verifiers = frozenset(
        name
        for name, names in checked.items()
        if names
        & _written_names(namespace.function_commands(name), namespace, downloaders)
    )
    return _Shell(
        namespace,
        callees,
        downloaders,
        verifiers,
        _callers_of(verifiers, callees),
        frozenset(pins),
        by_function,
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
    install_args, sometimes = _with_array_elements(args, namespace)
    install_words = [unquote(node_text(node)) for node in install_args]
    installer, specs = _pinned_install(install_words, install_args)
    if _NO_STORE_FLAGS & set(install_words):
        installer = ""  # the bytes reach no store, so no store cache holds them
    store = _INSTALLER_STORES.get(installer, "")
    for spec in specs:
        node = install_args[install_words.index(spec)]
        if node.id in sometimes:
            continue
        pins = _pins_of([node], namespace, unit) | _literals(spec)
        fetches.append(
            Fetch(
                spec + where,
                frozenset(pins),
                frozenset(_sources_of([node], pins, namespace, unit)),
                frozenset(_at_each_home(store)),
                frozenset(),
                installer,
            )
        )
    if not _runs_downloader(command) and not shell.downloaders & set(words):
        return fetches
    values = [namespace.value(node, unit) for node in args]
    spelled = [_spelled(node, namespace, unit) for node in args]
    written = _outputs(command, words, spelled)
    directory = namespace.directory_at(command, unit)
    outputs, output_names = _placed(written, namespace.repo_root, directory)
    held, held_names = _placed(
        _copies(written | _held_paths(command, spelled), namespace),
        namespace.repo_root,
        directory,
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
                frozenset(_sources_of([node], pins, namespace, unit)),
                held,
                held_names,
            )
        )
    return fetches


def _at_each_home(store: str) -> set[str]:
    """STORE at each of _HOMES when it lies under `~`, else STORE alone."""
    if not store.startswith("~/"):
        return {store} if store else set()
    return {home + store[1:] for home in _HOMES}


def _held_paths(command: Node, spelled: list[str]) -> set[str]:
    """Where a call to a downloader function may leave its bytes: each
    absolute path among its arguments (`pinned_fetch /tmp/c/t.tgz URL`). Its
    body decides where it writes, and a path it is handed is where a cache
    can find them. A curl or wget command says where it writes in its flags."""
    if _runs_downloader(command):
        return set()
    return {word for word in spelled[1:] if word.startswith(("/", "~", "$HOME/"))}


def _copies(paths: set[str], namespace: Namespace) -> set[str]:
    """PATHS plus each path a `cp`, `install` or `mv` in NAMESPACE copies one
    of them to, followed to a fixed point. `curl -o "$work/t" …` and then
    `install "$work/t" "$cache/t"` leave the bytes where a cache of `$cache`
    holds them."""
    copies = []
    for command, unit in namespace.executed:
        words = tokens(command)
        launched = _launched(words)
        if not launched or program_name(launched[0] or "") not in _COPIERS:
            continue
        nodes = command_arguments(command)[len(words) - len(launched) + 1 :]
        named = [_spelled(node, namespace, unit) for node in nodes]
        named = [word for word in named if not word.startswith("-")]
        if len(named) >= 2:
            copies.append((frozenset(named[:-1]), named[-1]))
    found, pending = set(paths), True
    while pending:
        pending = False
        for sources, target in copies:
            copied = sources & found
            if copied and target not in found:
                found.add(target)
                found |= {posixpath.join(target, posixpath.basename(s)) for s in copied}
                pending = True
    return found


def _pinned_install(words: list[str], args: list) -> tuple[str, list[str]]:
    """The installer of the install invocation in WORDS, and its pinned
    package specs."""
    invocation = _find_install(words)
    if not invocation:
        return "", []
    family, index = invocation
    if family == NODE and not writes_outside_the_lockfile(words, index):
        return (
            "",
            [],
        )  # a local install is covered by the lockfile and setup-node's cache
    pinned, unpinned = spec_scan(args[index:], family)
    if unpinned:
        return "", []
    # A spec that opens with an expansion (`"$@"`, `${ARGS[@]}`) names no
    # package the text fixes, so it is no download this lint can name.
    return _installer(words, index), [
        spec
        for spec in pinned
        if not _FLOATING.search(spec) and not spec.startswith("$")
    ]


def _with_array_elements(
    args: list[Node], namespace: Namespace
) -> tuple[list[Node], set[int]]:
    """ARGS with each argument that expands a whole array (`"${specs[@]}"`)
    replaced by the elements the array's assignments give it, and the ids of
    the elements only some runs add. Thus `specs+=("ruff==$V")` and then
    `pip install "${specs[@]}"` installs `ruff==$V`. An array that one
    assignment sets to anything but a list stays as written. An assignment
    under a condition or in a loop adds its elements only on some runs:
    `case "$tool" in pre-commit) specs+=(…)` adds its element only on the
    runs that pass `pre-commit`, so the text does not fix that the install
    fetches it. Such an element that names no version still makes the
    install float."""
    expanded, sometimes = [], set()
    for node in args:
        elements = _array_elements(node, namespace)
        if elements is None:
            expanded.append(node)
            continue
        for element, conditional in elements:
            expanded.append(element)
            if conditional:
                sometimes.add(element.id)
    return expanded, sometimes


def _array_elements(word: Node, namespace: Namespace) -> list[tuple[Node, bool]] | None:
    """The elements of the array WORD expands whole, across every assignment
    of it, each with whether its assignment is conditional. None when WORD is
    not such an expansion."""
    if word.type == "string" and len(word.named_children) == 1:
        word = word.named_children[0]
    names = list(iter_nodes(word, "variable_name")) if word.type == "expansion" else []
    name = node_text(names[0]) if names else ""
    if not name or node_text(word) not in (f"${{{name}[@]}}", f"${{{name}[*]}}"):
        return None
    elements = []
    for assignment, _ in namespace.assignments.get(name, []):
        value = assignment.child_by_field_name("value")
        if value is None or value.type != "array":
            return None
        conditional = _conditional(assignment)
        elements += [(element, conditional) for element in value.named_children]
    return elements


def _conditional(node: Node) -> bool:
    """True when NODE runs only on some runs of the body that holds it: under
    an `if`, a `case` item, a loop, or after `&&` or `||`. The command before
    the operator always runs."""
    child, parent = node, node.parent
    while parent is not None and parent.type not in ("function_definition", "program"):
        if parent.type in _CONDITIONAL_NODES:
            return True
        if parent.type == "list" and parent.named_children[0].id != child.id:
            return True
        child, parent = parent, parent.parent
    return False


def _installer(words: list[str], index: int) -> str:
    """The package manager of an install whose command words end at INDEX:
    `uv` for `uv pip install` and `uv tool install`, `yarn` for `yarn global
    add`, and `pip` for `pip3 install` or `python -m pip install`."""
    first = words[index - 3] if index >= 3 else ""
    if first in ("uv", "yarn"):
        return first
    second = program_name(words[index - 2])
    return "pip" if _PIP_ALIAS.match(second) else second


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


class _CheckedCall(NamedTuple):
    """A command that calls a downloader function that checks a digest: the
    checking functions it CALLS, the PINS its arguments carry, and the
    function whose body holds it (None at the top level)."""

    command: Node
    unit: Unit
    called: frozenset[str]
    pins: frozenset[str]
    enclosing: str | None


class _CallChains:
    """The checked calls of one namespace, indexed by each function they call
    and by the function whose body holds them. CHECKING names the downloader
    functions that check a digest."""

    def __init__(self, calls: list[_CheckedCall], checking: frozenset[str]):
        self.checking = checking
        self.by_callee: dict[str, list[_CheckedCall]] = {}
        self.by_holder: dict[str | None, list[_CheckedCall]] = {}
        for call in calls:
            self.by_holder.setdefault(call.enclosing, []).append(call)
            for name in call.called:
                self.by_callee.setdefault(name, []).append(call)
        self._above: dict[str, bool] = {}
        self._covered: dict[frozenset[str], bool] = {}

    def pinned_above(self, name: str | None) -> bool:
        """True when a call of the checking function NAME, or of a checking
        function that holds such a call, carries a pin."""
        if name not in self.checking:
            return False
        if name not in self._above:
            self._above[name] = False  # a call cycle adds no pin
            self._above[name] = any(
                call.pins or self.pinned_above(call.enclosing)
                for call in self.by_callee.get(name, [])
            )
        return self._above[name]

    def covered(self, call: _CheckedCall) -> bool:
        """True when CALL names the pin of each download under it: it carries
        a pin itself, or the functions it calls hold checked calls and every
        one of them is covered."""
        if call.pins:
            return True
        if call.called not in self._covered:
            self._covered[call.called] = False  # a call cycle covers nothing
            inner = [c for name in call.called for c in self.by_holder.get(name, [])]
            self._covered[call.called] = bool(inner) and all(
                self.covered(c) for c in inner
            )
        return self._covered[call.called]

    def reports(self, call: _CheckedCall) -> bool:
        """True when CALL is the site that reports its download: no call above
        it names a pin, and it carries a pin itself, or it is the outermost
        call and some download under it has no call that names a pin."""
        if self.pinned_above(call.enclosing):
            return False
        if call.pins:
            return True
        return call.enclosing not in self.checking and not self.covered(call)


def _checked_calls(
    commands: list[tuple[Node, Unit]], shell: _Shell, reported: frozenset[str]
) -> list[Fetch]:
    """The pinned downloads COMMANDS make by calling a downloader function that
    checks a digest. A function that REPORTED names already yields a fetch from
    a command inside it, so a call to it adds none. A lookup
    (`command -v fetch_verified`) runs nothing.

    One chain of calls is one download, so one call reports it: the outermost
    call whose arguments carry a pin, since that call names the tool and its
    version. An argument-less wrapper above it (`provision() { fetch_verified
    kata "$kata_version" …; }`) does not take its place. The outermost call
    reports any download under it that no call names a pin for."""
    namespace = shell.namespace
    calls = []
    for command, unit in commands:
        called = set(callable_words(command)) & shell.checked_downloaders
        if not called or called & reported or is_lookup(tokens(command)):
            continue
        pins = _pins_of(command_arguments(command)[1:], namespace, unit)
        calls.append(
            _CheckedCall(
                command,
                unit,
                frozenset(called),
                frozenset(pins),
                _enclosing_function(command),
            )
        )
    chains = _CallChains(calls, shell.checked_downloaders)
    return [_checked_fetch(call, shell) for call in calls if chains.reports(call)]


def _checked_fetch(call: _CheckedCall, shell: _Shell) -> Fetch:
    """The fetch CALL makes. Its pins are the ones its arguments carry and the
    ones the digest checks in the checking functions it reaches read. A
    `hashFiles()` key may hash the calling file, a file that assigns one of its
    pins, or a file that defines a checking function the call reaches."""
    namespace = shell.namespace
    chain, pending = set(), list(call.called)
    while pending:
        name = pending.pop()
        if name not in chain:
            chain.add(name)
            pending += shell.callees[name] & shell.checked_downloaders
    pins = set(call.pins)
    for name in chain:
        pins |= shell.function_digest_pins.get(name, frozenset())
    sources = _sources_of(
        command_arguments(call.command)[1:], pins, namespace, call.unit
    ) | {
        rel
        for name in chain
        for _, owner in namespace.functions[name]
        if (rel := _relative(owner.path))
    }
    where = f" in {rel}" if (rel := _relative(call.unit.path)) else ""
    label = " ".join(tokens(call.command)) + " (digest-checked)" + where
    return Fetch(label, frozenset(pins), frozenset(sources), frozenset())


@functools.lru_cache(maxsize=1024)
def _namespace_fetches(namespace: Namespace) -> list[Fetch]:
    """Every pinned download NAMESPACE makes, computed once per namespace: a
    shared script is reached from many jobs."""
    shell = _shell_context(namespace)
    found: list[Fetch] = []
    unmatched: list[tuple[Node, Unit]] = []
    # A verifier whose own download already yields a fetch (its URL carries a
    # pin) needs no second fetch from each call of it. Only that case: a
    # wrapper that also runs `pip install x==1` still owes the fetch of the
    # checked download it calls.
    holders = set()
    for command, unit in namespace.executed:
        fetches = _command_fetches(command, unit, shell)
        found += fetches
        if not fetches:
            unmatched.append((command, unit))
        elif _runs_downloader(command) and (
            (name := _enclosing_function(command)) in shell.verifiers
        ):
            holders.add(name)
    reported = _callers_of(holders, shell.callees)
    return found + _checked_calls(unmatched, shell, reported)


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
    """True when SCOPE's key names FETCH's pin or a file that sets it, its
    path holds FETCH's output, or it is the store of FETCH's installer."""
    if scope.key is None:
        return True
    if fetch.installer and scope.installers & {fetch.installer, ANY_INSTALLER}:
        return True
    if any(_pin_in_key(pin).search(scope.key) for pin in fetch.pins):
        return True
    for pattern in scope.hashed:
        pattern = pattern.removeprefix("./")
        if any(
            glob_to_regex(pattern).match(src) or glob_to_regex(src).match(pattern)
            for src in fetch.sources
        ):
            return True
        if fetch.pins & _hashed_assignments(REPO_ROOT, pattern):
            return True
    for out in fetch.outputs:
        for path in scope.paths:
            if glob_to_regex(path).match(out) or glob_to_regex(
                path.rstrip("/") + "/**"
            ).match(out):
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
    report it at, the composite action file it came from (empty for a step
    written directly in the workflow), and what its expressions can read."""

    step: dict
    line: int
    origin: str
    context: Context


def flatten(
    steps: list[dict], anchor: int, origin: str, seen: frozenset, context: Context
) -> list[FlatStep]:
    """Every step a job really runs, with the workflow line to report it at.

    A `uses: ./…` step is kept AND followed, because the composite action it names
    may hold both the cache and the install. A step that comes from an action keeps
    the ANCHOR line of the workflow step that reached it — the cache a finding asks
    for belongs to the job, which lives in the workflow — and carries ORIGIN, the
    action file, so the message can still say where the install is written. SEEN
    breaks a cycle between two actions that use each other. CONTEXT is what the
    expressions in STEPS can read.
    """
    flat: list[FlatStep] = []
    for step in steps:
        line = step.get("__line__", anchor) if not origin else anchor
        flat.append(FlatStep(step, line, origin, context))
        uses = step.get("uses")
        if not isinstance(uses, str) or not uses.startswith("./"):
            continue
        target = _action_path(uses)
        if target is None or target in seen:
            continue
        doc = _load(target)
        if doc is not None:
            inner = _steps(doc.get("runs"))
            flat += flatten(
                inner,
                line,
                uses.rstrip("/"),
                seen | {target},
                _action_context(doc, inner, step, context),
            )
    return flat


def _action_context(
    doc: dict, steps: list[dict], caller: dict, context: Context
) -> Context:
    """The context of a composite action's STEPS when the step CALLER uses it.
    Each input is what CALLER's `with:` passes, read in the caller's CONTEXT,
    or else the input's default. The environment is the caller's."""
    env = {**context.env, **env_of(caller)}
    own = Context(tuple(steps), {}, env)
    declared, passed = doc.get("inputs"), caller.get("with")
    passed = passed if isinstance(passed, dict) else {}
    inputs: dict[str, Text | None] = {}
    for name, spec in declared.items() if isinstance(declared, dict) else []:
        if name == "__line__":
            continue
        if name in passed:
            inputs[name] = rendered(passed[name], caller, context)
            continue
        default = spec.get("default", "") if isinstance(spec, dict) else ""
        inputs[name] = rendered(default, caller, own)
    return own._replace(inputs=inputs)


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
        context = Context(tuple(steps), None, {**env_of(doc), **env_of(job)})
        flat = flatten(steps, key_line, "", frozenset({path}), context)
        # The job-scoped marker reads the surfaces a `#` may open a comment on:
        # a real YAML comment, and a `run:` value. A `#` inside any other scalar
        # is content. The step-scoped read below keeps the raw script, because
        # `flatten` also returns steps from a composite action in another file,
        # whose text this job's BLOCK does not hold.
        if any(annotated(line, OPT_OUT) for line in yaml_script_view(block)):
            continue
        scopes = [
            scope for f in flat if (scope := cache_scope(f.step, f.context)) is not None
        ]
        default_directory = _default_directory(doc, job)
        for step, line, origin, _ in flat:
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

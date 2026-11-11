"""Which jobs of a workflow post a check that can hold a merge back.

PROBLEM CLASS — a repository writes its gating checks in one of two places: a
`# required-check: true` marker on a job, or a Mergify queue rule that names the
check in `check-success=<name>`. A repository that moves to Mergify deletes its
markers. A check that reads only the markers then finds no gate, and it passes
while it judges nothing. `GatingSource` reads both places.

Mergify reads the config at the first of `CONFIG_PATHS` that exists. That
config gates in three places:

- `queue_conditions`, which Mergify reads on the pull request, on every event.
- `merge_conditions`, which it reads on its draft, the queue build, or on the
  pull request itself with in-place checks.
- the `conditions` of a `pull_request_rules` entry with a `queue` action.

A condition list is an AND, `and:`/`or:` nest, and the queue rules read as one
`or`. A check on every path is REQUIRED, and a check on some paths is
CONDITIONAL. Both gate. A guess can drop a gate, so the reader refuses `not:`,
`extends`, and every check condition except an equality on `check-success`,
`check-success-or-neutral` or `status-success`.
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LOCAL_CALL,
    PR_EVENTS,
    WORKFLOW_GLOBS,
    _job_names,
    _marked_jobs,
    has_trigger,
)

# The places Mergify reads its config from, in the order it reads them.
CONFIG_PATHS = (".mergify.yml", ".mergify/config.yml", ".github/mergify.yml")

# The values of the `--gating-source` flag that the gating checks take.
MARKERS = "markers"
MERGIFY = "mergify"
BOTH = "both"
SOURCES = (BOTH, MARKERS, MERGIFY)

# Mergify's spellings of "this check concluded success"; a neutral run also
# satisfies the `-or-neutral` one.
_SUCCESS_ATTRIBUTES = frozenset(
    {"check-success", "check-success-or-neutral", "status-success"}
)
# A Mergify operator starts at the first of these characters.
_OPERATOR_CHARS = frozenset("=~!<>:≠≥≤ ")
# The app prefix that pins a check name to GitHub Actions: `@github-actions/CI`.
_ACTIONS_APP = "@github-actions/"


class MergifyConfigError(ValueError):
    """A Mergify config that this reader cannot read without a guess."""


@dataclass(frozen=True, kw_only=True, slots=True)
class QueueChecks:
    """The check names one condition key names, split by whether every path needs them."""

    required: frozenset[str]
    conditional: frozenset[str]

    def gating(self) -> frozenset[str]:
        """Every check that can hold a merge back."""
        return self.required | self.conditional


@dataclass(frozen=True, kw_only=True, slots=True)
class _Paths:
    """A condition that can hold: the checks every path needs, and every check a path names."""

    every: frozenset[str]
    named: frozenset[str]


_NOTHING = _Paths(every=frozenset(), named=frozenset())
_EMPTY = QueueChecks(required=frozenset(), conditional=frozenset())


class _Condition(NamedTuple):
    """One string condition: its `-`/`#` modifiers, its attribute, and the value
    after an equality operator (None for every other operator)."""

    prefix: str
    attribute: str
    value: str | None


def _split(condition: str) -> _Condition:
    """CONDITION split at its modifiers and its operator."""
    text = condition.strip()
    body = text.lstrip("-#")
    end = next((i for i, char in enumerate(body) if char in _OPERATOR_CHARS), len(body))
    rest = body[end:].lstrip()
    # Mergify writes equality as `=`, `==` or `:`.
    operator = next((op for op in ("==", "=", ":") if rest.startswith(op)), None)
    plain = operator is not None and rest[len(operator) : len(operator) + 1] != "~"
    value = rest[len(operator) :].strip() if plain and operator else None
    return _Condition(text[: len(text) - len(body)], body[:end], value)


def _check_name(value: str) -> str:
    """VALUE with the GitHub Actions app prefix removed, as a job posts it."""
    return value[len(_ACTIONS_APP) :] if value.startswith(_ACTIONS_APP) else value


def _leaf(condition: str, base: str | None) -> _Paths | None:
    """One string condition: its checks, or None when it cannot hold on BASE."""
    prefix, attribute, value = _split(condition)
    if attribute.startswith(("check-", "status-")):
        if prefix or attribute not in _SUCCESS_ATTRIBUTES or not value:
            raise MergifyConfigError(
                f"this reader cannot read the check condition {condition!r}; "
                "only `check-success=<name>` names a gate it can read"
            )
        name = _check_name(value)
        return _Paths(every=frozenset({name}), named=frozenset({name}))
    if base is not None and attribute == "base" and value is not None:
        if prefix in ("", "-"):
            holds = (value == base) != (prefix == "-")
            return _NOTHING if holds else None
    return _NOTHING


def _all(conditions: object, base: str | None) -> _Paths | None:
    """A condition LIST, which Mergify reads as an AND."""
    if not isinstance(conditions, list):
        raise MergifyConfigError(f"a condition list is not a list: {conditions!r}")
    every: set[str] = set()
    named: set[str] = set()
    for condition in conditions:
        paths = _node(condition, base)
        if paths is None:
            return None
        every |= paths.every
        named |= paths.named
    return _Paths(every=frozenset(every), named=frozenset(named))


def _any(arms: Sequence[_Paths | None]) -> _Paths | None:
    """An OR over ARMS: every path needs a check only if every live arm needs it."""
    live = [arm for arm in arms if arm is not None]
    if not live:
        return None
    first, rest = live[0], live[1:]
    return _Paths(
        every=first.every.intersection(*(arm.every for arm in rest)),
        named=first.named.union(*(arm.named for arm in rest)),
    )


def _node(condition: object, base: str | None) -> _Paths | None:
    """One condition: a string, or a mapping with the one key `and` or `or`."""
    if isinstance(condition, str):
        return _leaf(condition, base)
    if isinstance(condition, dict) and len(condition) == 1:
        [(operator, children)] = condition.items()
        if operator == "and":
            return _all(children, base)
        if operator == "or":
            if not isinstance(children, list):
                raise MergifyConfigError(f"`or:` holds no list: {children!r}")
            return _any([_node(child, base) for child in children])
    raise MergifyConfigError(f"this reader cannot read the condition {condition!r}")


def _config(text: str) -> dict:
    """Config TEXT as a mapping, refusing a shape this reader cannot read."""
    try:
        config = safe_load(text)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        raise MergifyConfigError(
            f"the config is not valid YAML ({first_line})"
        ) from None
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise MergifyConfigError("the config is not a mapping")
    if "extends" in config:
        raise MergifyConfigError(
            "`extends` pulls rules from another repository, which this reader cannot read"
        )
    return config


def _rule_list(config: dict, key: str) -> list[dict]:
    """The rules under KEY of CONFIG. An absent KEY holds no rule."""
    rules = config.get(key)
    if rules is None:
        return []
    if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
        raise MergifyConfigError(f"`{key}` is not a list of rules")
    return rules


def _either(arms: list[_Paths | None]) -> QueueChecks:
    """ARMS read as one `or`."""
    paths = _any(arms)
    if paths is None:
        return _EMPTY
    return QueueChecks(required=paths.every, conditional=paths.named - paths.every)


def _checks(text: str, key: str, base: str | None) -> QueueChecks:
    """The checks KEY names across every queue rule, which read as one `or`.

    A rule that leaves KEY out takes it from `defaults.queue_rule`, and with no
    default it has no condition. `true` is Mergify's spelling of no condition."""
    config = _config(text)
    defaults = config.get("defaults") or {}
    default_rule = defaults.get("queue_rule") if isinstance(defaults, dict) else None
    if not isinstance(default_rule, (dict, type(None))):
        raise MergifyConfigError("`defaults.queue_rule` is not a mapping")
    default_rule = default_rule or {}
    arms: list[_Paths | None] = []
    for rule in _rule_list(config, "queue_rules"):
        conditions = rule.get(key, default_rule.get(key, []))
        arms.append(_NOTHING if conditions is True else _all(conditions, base))
    return _either(arms)


def queue_action_checks(text: str, *, base: str | None = None) -> QueueChecks:
    """The checks that the `conditions` of a `queue` action in `pull_request_rules`
    name. A pull request enters the queue only through such a rule, so those
    checks gate the pull request like `queue_conditions` do."""
    arms: list[_Paths | None] = []
    for rule in _rule_list(_config(text), "pull_request_rules"):
        actions = rule.get("actions")
        if isinstance(actions, dict) and "queue" in actions:
            arms.append(_all(rule.get("conditions", []), base))
    return _either(arms)


def queue_checks(text: str, *, base: str | None = None) -> QueueChecks:
    """The checks a pull request into BASE must pass to enter the queue.

    None for BASE reads every base. Raises MergifyConfigError for a config that
    this reader cannot read without a guess."""
    return _checks(text, "queue_conditions", base)


def merge_checks(text: str, *, base: str | None = None) -> QueueChecks:
    """The checks Mergify's draft, the queue build, must pass to merge into BASE."""
    return _checks(text, "merge_conditions", base)


def config_path(root: Path) -> Path | None:
    """The Mergify config under ROOT that Mergify reads, or None."""
    return next((root / name for name in CONFIG_PATHS if (root / name).is_file()), None)


@dataclass(frozen=True, kw_only=True, slots=True)
class Gate:
    """Why one job gates: its marker, and the config names that its check posts."""

    marker: bool = False
    pull_request: frozenset[str] = frozenset()
    queue_build: frozenset[str] = frozenset()

    def describe(self, config: str | None) -> str:
        """The reason this job gates, for a finding's message."""
        if self.marker:
            return "the '# required-check: true' marker"
        names = ", ".join(
            f"'{n}'" for n in sorted(self.pull_request | self.queue_build)
        )
        return f"{config or 'the Mergify config'} gates on {names}"


@dataclass(frozen=True, kw_only=True)
class GatingSource:
    """Where the gating checks of a repository are written.

    MARKERS says whether a `# required-check: true` marker gates. PULL_REQUEST and
    QUEUE_BUILD are the names the Mergify config gates on, read on the pull
    request and on the draft. CALLED maps a workflow file to the jobs whose check
    another workflow posts under a gating `<caller> / <callee>` name."""

    markers: bool = True
    pull_request: frozenset[str] = frozenset()
    queue_build: frozenset[str] = frozenset()
    config: str | None = None
    called: dict[Path, dict[str, frozenset[str]]] = field(default_factory=dict)

    @classmethod
    def load(
        cls, root: Path, source: str = BOTH, workflows_dir: Path | None = None
    ) -> "GatingSource":
        """The gating source of the repository at ROOT, per SOURCE.

        Raises MergifyConfigError for a config this reader refuses, and for
        SOURCE `mergify` when no config gates on any check."""
        if source == MARKERS:
            return cls()
        path = config_path(root)
        if path is None:
            if source == MERGIFY:
                raise MergifyConfigError(
                    f"no Mergify config exists at {' or '.join(CONFIG_PATHS)}"
                )
            return cls()
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        try:
            pull_request = (
                queue_checks(text).gating() | queue_action_checks(text).gating()
            )
            queue_build = merge_checks(text).gating()
        except MergifyConfigError as err:
            raise MergifyConfigError(f"{rel}: {err}") from None
        if source == MERGIFY and not pull_request | queue_build:
            raise MergifyConfigError(f"{rel}: no queue rule gates on a check")
        names = pull_request | queue_build
        directory = (
            root / ".github" / "workflows" if workflows_dir is None else workflows_dir
        )
        return cls(
            markers=source == BOTH,
            pull_request=pull_request,
            queue_build=queue_build,
            config=rel,
            called=_called_gates(directory, names) if names else {},
        )

    def jobs(self, path: Path, blocks: dict, jobs: dict) -> dict[str, Gate]:
        """The jobs of the workflow at PATH that post a gating check, with why.

        BLOCKS is `_job_blocks` of the workflow text and JOBS its parsed `jobs:`."""
        marked = {str(k) for k in _marked_jobs(blocks, jobs)} if self.markers else set()
        names = self.pull_request | self.queue_build
        called = self.called.get(path.resolve(), {})
        found: dict[str, Gate] = {}
        for key, cfg in jobs.items():
            if not isinstance(cfg, dict):
                continue
            posted = (set(_job_names(str(key), cfg)) & names) | called.get(
                str(key), set()
            )
            if str(key) in marked or posted:
                found[str(key)] = Gate(
                    marker=str(key) in marked,
                    pull_request=frozenset(posted & self.pull_request),
                    queue_build=frozenset(posted & self.queue_build),
                )
        return found


def _called_gates(
    workflows_dir: Path, names: frozenset[str]
) -> dict[Path, dict[str, frozenset[str]]]:
    """Each workflow file mapped to its jobs that post a gating check through a
    reusable-workflow call, with the gating names each one posts.

    GitHub names a job that a called workflow runs `<caller> / <callee>`. So a
    gate on that name makes the callee job gate, and the caller job too, because
    the caller's `if:` and `needs:` decide whether the callee runs. A call that
    leaves the directory, or that loops, posts nothing here."""
    sources: dict[str, Path] = {
        path.name: path
        for glob in WORKFLOW_GLOBS
        for path in sorted(workflows_dir.glob(glob))
    }
    docs: dict[str, dict] = {}
    for name, path in sources.items():
        try:
            doc = safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError:
            continue  # the check that reads the file reports it
        if isinstance(doc, dict):
            docs[name] = doc
    found: dict[Path, dict[str, set[str]]] = {}

    def walk(file: str, prefixes: list[str], stack: tuple[str, ...]) -> set[str]:
        jobs = docs[file].get("jobs")
        hits: set[str] = set()
        if not isinstance(jobs, dict):
            return hits
        for key, cfg in jobs.items():
            if not isinstance(cfg, dict):
                continue
            posted = [
                f"{prefix} / {own}" if prefix else own
                for prefix in prefixes
                for own in _job_names(str(key), cfg)
            ]
            uses = cfg.get("uses")
            local = None if uses is None else LOCAL_CALL.match(str(uses))
            if uses is None:
                job_hits = set(posted) & names
            elif local and local["file"] in docs and local["file"] not in stack:
                job_hits = walk(local["file"], posted, (*stack, local["file"]))
            else:
                job_hits = set()  # a remote call, or one that loops: unreadable here
            # A top-level job that runs steps posts its own name, which `jobs`
            # matches directly. Record the callers and the called jobs.
            if job_hits and (uses is not None or prefixes != [""]):
                found.setdefault(sources[file].resolve(), {}).setdefault(
                    str(key), set()
                ).update(job_hits)
            hits |= job_hits
        return hits

    for file, doc in docs.items():
        if has_trigger(doc, *PR_EVENTS):
            walk(file, [""], (file,))
    return {
        path: {key: frozenset(hit) for key, hit in jobs.items()}
        for path, jobs in found.items()
    }


def add_source_flag(parser: argparse.ArgumentParser) -> None:
    """Add the `--gating-source` flag that every gating check takes to PARSER."""
    parser.add_argument(
        "--gating-source",
        choices=SOURCES,
        default=BOTH,
        help="where the gating checks are written: the `# required-check: true` "
        "markers, the Mergify config, or both (default: both)",
    )


def source_from_argv(
    argv: list[str] | None, description: str | None, root: Path, workflows_dir: Path
) -> GatingSource | None:
    """The gating source that a check's command line picks. A config this reader
    refuses prints as an `::error::` annotation, and the answer is None."""
    parser = argparse.ArgumentParser(description=description)
    add_source_flag(parser)
    # pre-commit passes no files (`pass_filenames: false`); discovery globs the tree.
    parser.add_argument("files", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return GatingSource.load(root, args.gating_source, workflows_dir)
    except MergifyConfigError as err:
        print(f"::error::{err}")
        return None

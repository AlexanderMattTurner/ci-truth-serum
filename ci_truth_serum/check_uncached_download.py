#!/usr/bin/env python3
"""Demand a cache in any job that downloads a version-pinned tool.

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

This lint reports a pinned install in a job whose steps hold **no cache at all**.
Local composite actions (`uses: ./.github/actions/foo`) are inlined first, so a
job that gets its caching from a shared setup action counts as cached.

The scan runs on the real bash grammar (`_bash_ast`), so an install written in a
comment, or inside a message a command prints, is not a command.

Deliberately NOT reported, because the download is the point or has no stable key:

  * an install with any unpinned spec — `check-versionless-install` owns that, and
    a floating install has no cache key that could stay correct;
  * a job that already holds one cache — a second, better-targeted cache is a
    judgement call this lint cannot make;
  * a `docker pull`, and any install inside a job whose purpose is to prove a
    fresh machine can install the thing.

Fix it by adding `actions/cache` keyed on the pinned version, and by making the
install skip its own work when the cache restores. Probe the post-condition (the
digest of the file, the version the binary reports), never the `cache-hit` output
alone: the post-job save runs even after a failed install, so a cache entry can
hold a partial tree.

A job that must download every time opts out with a `# cache-exempt: <reason>`
comment, either in the `run:` body or anywhere in the job's block. The reason is
REQUIRED; a bare annotation does not suppress.

This lint is opinionated (Tier 2): it prescribes a cache for a class of cost that
is real but never fails loudly.
"""

import re
import sys
from pathlib import Path
from typing import NamedTuple

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
    annotated,
    LineLoader,
    MESSAGE_PREFIX,
    workflow_files,
    _job_blocks,
)
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
    "job '{job}' downloads pinned {what} with no cache step anywhere in the job — "
    "the bytes are identical on every run, so every push to every pull request "
    "pays for the same fetch, and a slow registry turns that cost into a job "
    "timeout that GitHub reports as 'cancelled'. Add actions/cache keyed on the "
    "pinned version and skip the install when the cache restores, or annotate "
    f"'# {OPT_OUT}: <reason>'."
)

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


def _caches(step: dict) -> bool:
    """True when STEP restores or saves a cache.

    The `uses:` value carries a ref (`actions/cache@0057852…`), so the action's
    name is the part before the `@`."""
    uses = step.get("uses")
    if not isinstance(uses, str):
        return False
    action = uses.split("@", 1)[0].strip()
    if action.startswith(_CACHE_ACTIONS) or action in _ALWAYS_CACHING_ACTIONS:
        return True
    inputs = _SETUP_CACHE_INPUTS.get(action)
    with_block = step.get("with")
    if not inputs or not isinstance(with_block, dict):
        return False
    return any(
        str(with_block.get(key, "")).strip() not in ("", "false") for key in inputs
    )


def _pinned_downloads(script: str) -> list[str]:
    """What SCRIPT fetches at a version that cannot change between runs.

    Each entry names the thing downloaded, for the message. A command with any
    unpinned spec yields nothing: `check-versionless-install` owns it, and it has
    no cache key to be given."""
    found: list[str] = []
    for command in iter_nodes(parse(script), "command"):
        args = command_arguments(command)
        tokens = [unquote(node_text(node)) for node in args]
        if not tokens or MESSAGE_PREFIX.match(tokens[0]):
            continue
        found += _pinned_install(tokens, args) + _pinned_fetch(tokens)
    return found


def _pinned_install(tokens: list[str], args: list) -> list[str]:
    """The pinned package specs of an install invocation in TOKENS."""
    invocation = _find_install(tokens)
    if not invocation:
        return []
    family, index = invocation
    if family == NODE and not writes_outside_the_lockfile(tokens, index):
        return []  # a local install is covered by the lockfile and setup-node's cache
    pinned, unpinned = spec_scan(args[index:], family)
    if unpinned:
        return []
    return [spec for spec in pinned if not _FLOATING.search(spec)]


def _pinned_fetch(tokens: list[str]) -> list[str]:
    """The release URLs a downloader in TOKENS fetches at a fixed version.

    The downloader is looked for at any position, so a leading wrapper (`sudo
    curl …`, `retry wget …`) does not hide it."""
    if not any(token.split("/")[-1] in _DOWNLOADERS for token in tokens):
        return []
    return [token for token in tokens if _PINNED_URL.match(token)]


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
        if any(_caches(step) for step, _, _ in flat):
            continue
        if any(annotated(line, OPT_OUT) for line in block.splitlines()):
            continue
        for step, line, origin in flat:
            script = step.get("run")
            if not isinstance(script, str):
                continue
            if any(annotated(text, OPT_OUT) for text in script.splitlines()):
                continue
            downloads = _pinned_downloads(script)
            if not downloads:
                continue
            what = ", ".join(sorted(set(downloads))[:3])
            if origin:
                what += f" (installed by {origin})"
            violations.append((line, MESSAGE.format(job=name, what=what)))
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

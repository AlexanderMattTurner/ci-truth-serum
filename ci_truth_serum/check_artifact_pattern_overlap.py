#!/usr/bin/env python3
"""A download `pattern:` must collect artifacts from one upload family only.

`pattern: test-durations-*` collects every matching artifact of this run. An
upload named `test-durations-map` sits inside that prefix, so the download hands
the next step a merged directory of two shapes, and nothing reports it.

Each upload's `name:` is a template. A `${{ … }}` expression becomes one `*`
character, and the check matches the pattern against that text. So a pattern
collects a family only when its own wildcard covers the expression. A whole
matrix of shards therefore counts as ONE family. An expression the pattern does
not cover is unknown, and the check stays silent on it. A name that is only an
expression says nothing, so it never counts as a family.

The check expands a local composite action at the call site. Each
`${{ inputs.X }}` takes the caller's `with: X` or the input's default. Known
blind spots, all false negatives: a pattern that holds an expression, a download
from another run (`run-id:`), a remote wrapper action, uploads in a called
reusable workflow, and `{a,b}` braces.
Opt out with `# artifact-pattern-ok: <reason>` in the download step.
"""

import argparse
import fnmatch
import re
import sys
from pathlib import Path
from typing import Any, NamedTuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_fastyaml import safe_load  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    LineLoader,
    _job_blocks,
    annotated_near,
    container_block_end,
    step_span_ends,
    workflow_files,
    yaml_script_view,
)

JsonObject = dict[str, Any]

OPT_OUT = "artifact-pattern-ok"
UPLOAD_ACTION = "actions/upload-artifact"
DOWNLOAD_ACTION = "actions/download-artifact"
# actions/upload-artifact names the artifact `artifact` when `name:` is empty.
UPLOAD_DEFAULT_NAME = "artifact"

# A whole `${{ … }}` expression. Non-greedy to the first `}}`, so a `{0}` inside
# a `format(...)` call stays inside the expression it belongs to.
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)
# An expression that is exactly one input reference, the only kind expanded.
_INPUT_REF = re.compile(r"\$\{\{\s*inputs\.(?P<name>[A-Za-z_][\w-]*)\s*\}\}")
# `run-id:` set to this run reads this run, like an empty `run-id:`.
_THIS_RUN = re.compile(r"^\$\{\{\s*github\.run_id\s*\}\}$")


class Transfer(NamedTuple):
    """One upstream artifact call that a workflow step makes, after expansion."""

    action: str
    inputs: dict[str, str]


def artifact_glob(name: str) -> str:
    """NAME as a template: each `${{ … }}` expression becomes `*`."""
    return _EXPRESSION.sub("*", name)


def _action_ref(uses: object) -> str:
    return uses.split("@", 1)[0] if isinstance(uses, str) else ""


def _with(step: JsonObject) -> dict[str, str]:
    raw = step.get("with")
    if not isinstance(raw, dict):
        return {}
    return {
        str(k).lower(): "" if v is None else str(v)
        for k, v in raw.items()
        if k != "__line__"
    }


class Expander:
    """Expands a step through local composite actions down to upstream calls."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self._docs: dict[str, JsonObject | None] = {}

    def _composite(self, ref: str) -> JsonObject | None:
        if ref not in self._docs:
            base = self.repo_root / ref
            found = [
                p for p in (base / "action.yml", base / "action.yaml") if p.is_file()
            ]
            doc = safe_load(found[0].read_text(encoding="utf-8")) if found else None
            self._docs[ref] = doc if isinstance(doc, dict) else None
        return self._docs[ref]

    def transfers(
        self, step: JsonObject, seen: frozenset[str] = frozenset()
    ) -> list[Transfer]:
        """Every upstream upload or download STEP reaches, with its inputs.

        A local action is read from the repository and its steps are expanded
        with the caller's inputs. A missing action, or a cycle, yields nothing.
        """
        ref = _action_ref(step.get("uses"))
        if ref in (UPLOAD_ACTION, DOWNLOAD_ACTION):
            return [Transfer(ref, _with(step))]
        if not ref.startswith("./") or ref in seen:
            return []
        doc = self._composite(ref)
        runs = doc.get("runs") if doc else None
        inner = runs.get("steps") if isinstance(runs, dict) else None
        if not isinstance(inner, list):
            return []
        values = _input_values(doc, _with(step))
        out: list[Transfer] = []
        for child in inner:
            if isinstance(child, dict):
                bound = {**child, "with": _bind(_with(child), values)}
                out += self.transfers(bound, seen | {ref})
        return out


def _input_values(doc: JsonObject, given: dict[str, str]) -> dict[str, str]:
    """The value of each declared input: the caller's, else its default."""
    declared = doc.get("inputs")
    values: dict[str, str] = {}
    for name, spec in declared.items() if isinstance(declared, dict) else []:
        default = spec.get("default") if isinstance(spec, dict) else None
        values[str(name).lower()] = "" if default is None else str(default)
    return {**values, **given}


def _bind(raw: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """RAW with each whole `${{ inputs.X }}` replaced by X's value.

    An unknown input stays an expression, which later reads as `*`.
    """

    def one(match: re.Match[str]) -> str:
        return values.get(match.group("name").lower(), match.group(0))

    return {k: _INPUT_REF.sub(one, v) for k, v in raw.items()}


def _upload_glob(transfer: Transfer) -> str | None:
    """The family glob an upload publishes, or None when the name is unknown."""
    glob = artifact_glob(transfer.inputs.get("name") or UPLOAD_DEFAULT_NAME)
    return None if set(glob) <= {"*"} else glob


def _pattern(transfer: Transfer) -> str | None:
    """The literal pattern a download in THIS run collects, or None."""
    pattern = transfer.inputs.get("pattern", "")
    run_id = transfer.inputs.get("run-id", "").strip()
    if not pattern or _EXPRESSION.search(pattern):
        return None
    if run_id and not _THIS_RUN.match(run_id):
        return None
    return pattern


class Download(NamedTuple):
    """A pattern download, at the workflow step that reaches it."""

    step_line: int
    span_end: int | None
    pattern: str


def _job_steps(doc: JsonObject) -> list[tuple[str, list[JsonObject]]]:
    jobs = doc.get("jobs")
    out = []
    for name, job in jobs.items() if isinstance(jobs, dict) else []:
        steps = job.get("steps") if isinstance(job, dict) else None
        if isinstance(steps, list):
            out.append((str(name), [s for s in steps if isinstance(s, dict)]))
    return out


def analyze(doc: object, text: str, expander: Expander) -> list[tuple[int, str]]:
    """(download step's 1-based line, message) for each pattern that collects
    two or more upload families in one workflow DOC parsed from TEXT."""
    if not isinstance(doc, dict):
        return []
    lines = text.splitlines()
    blocks = _job_blocks(text)
    families: set[str] = set()
    downloads: list[Download] = []
    for name, steps in _job_steps(doc):
        ends = step_span_ends(
            steps, container_block_end(blocks, name, max(len(lines), 1))
        )
        for step in steps:
            line = step.get("__line__", 1)
            for transfer in expander.transfers(step):
                if transfer.action == UPLOAD_ACTION:
                    glob = _upload_glob(transfer)
                    families |= {glob} if glob else set()
                elif (pattern := _pattern(transfer)) is not None:
                    downloads.append(Download(line, ends.get(line), pattern))
    comments = yaml_script_view(text)
    found: list[tuple[int, str]] = []
    for line, span_end, pattern in sorted(set(downloads)):
        matched = sorted(f for f in families if fnmatch.fnmatchcase(f, pattern))
        if len(matched) < 2:
            continue
        if annotated_near(lines, line, OPT_OUT, span_end=span_end, comments=comments):
            continue
        found.append((line, _message(pattern, matched)))
    return found


def _message(pattern: str, matched: list[str]) -> str:
    return (
        f"download pattern `{pattern}` collects {len(matched)} upload families "
        f"in this workflow: {matched}. The next step receives artifacts of a "
        "shape it does not expect. Rename one upload out of the pattern's "
        f"prefix, or annotate `# {OPT_OUT}: <reason>`."
    )


def violations(text: str, repo_root: Path) -> list[tuple[int, str]]:
    """(1-based line, message) for every finding in one workflow's TEXT.

    A file this cannot parse as YAML is reported at line 1, not passed as clean.
    """
    try:
        doc = yaml.load(text, Loader=LineLoader)
    except yaml.YAMLError as err:
        first_line = str(err).partition("\n")[0]
        return [
            (
                1,
                f"could not parse as YAML ({first_line}); cannot verify that each "
                "download pattern collects one upload family. Fix the syntax and "
                "re-check.",
            )
        ]
    return analyze(doc, text, Expander(repo_root))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="the repository root to scan (default: the current directory)",
    )
    parser.add_argument("files", nargs="*", help="ignored; every workflow is read")
    args = parser.parse_args(argv)

    total = 0
    for path in workflow_files(args.repo_root / ".github" / "workflows"):
        rel = path.relative_to(args.repo_root)
        for line, message in violations(
            path.read_text(encoding="utf-8"), args.repo_root
        ):
            print(f"::error file={rel},line={line}::{message}")
            total += 1
    if total:
        print(
            f"\nERROR: {total} download pattern(s) collect more than one upload family."
        )
        print(
            "A pattern download merges every artifact whose name matches, so two "
            "families under one prefix reach the next step as one input."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()

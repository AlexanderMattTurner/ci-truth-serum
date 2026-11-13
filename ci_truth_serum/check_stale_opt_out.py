#!/usr/bin/env python3
"""Report an opt-out annotation that no longer suppresses anything.

An opt-out such as `# allow-exit-suppress: <reason>` marks a place where a check
fired and an author overruled it. The code below it can change, or the check can
become more exact. Then the check stops firing there, but the annotation stays.
Nothing removes it, so these annotations accumulate. This check finds them, as
ruff's RUF100 finds an unused `noqa`. Fix a finding by deleting the annotation.

For each file that holds an annotation, this check runs the check that owns the
token again, inside ``_cts_linecheck.audit_opt_outs``. There the shared helper
``annotated_near`` records each window line that held the opt-out, and returns
its real answer. The check therefore takes the path of a normal run, and a
lookup that only runs after another opt-out cleared a finding is still made. An
annotation that no lookup found changes no lookup's answer, so it is stale.

The annotation is read through ``_cts_comments.marker_comments``: the grammar of
the file's language says where the comments are. A token inside a string, a
heredoc body, or a quoted YAML value is not an annotation, so it is never judged.

A check is covered when two conditions are true:

  * One file run reports what a whole-list run reports. The registry marks such
    a check `per_file`. ``_ONE_FILE_EXTRA`` adds the ones that have this property
    but stay unmarked for a reason that does not apply here.
  * It reads its opt-out through the shared helpers, so the audit sees the read.
    A helper that is told no line (``annotated``, ``opted_out``) credits every
    annotation of its token in the file, so it can hide a stale one but never
    report a live one.

A token that an uncovered check also names is never judged, because that check
can clear a finding this audit does not see. A covered check that refuses a file
makes the run fail, and its tokens in that file are not judged.
"""

import argparse
import ast
import contextlib
import importlib
import io
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cts_comments import marker_comments  # noqa: E402,I001  # pylint: disable=wrong-import-position
from _cts_linecheck import (  # noqa: E402,I001  # pylint: disable=wrong-import-position
    OptOutAudit,
    annotation_re,
    audit_opt_outs,
    is_prose_path,
    is_yaml_source,
    run_file_cli,
    unreadable_reason,
    yaml_scannable,
)
from _cts_registry import CHECKS, Check  # noqa: E402,I001  # pylint: disable=wrong-import-position
from run_tier import matches  # noqa: E402,I001  # pylint: disable=wrong-import-position

# `per_file` also requires that `main` does no work at the scale of the tree.
# This audit needs only the first property. check_argument_exit_swallow has it:
# its findings for a path come from that path and the files it sources, never
# from the other paths on argv. It stays unmarked only because its `main` lists
# the tracked tree on each call.
_ONE_FILE_EXTRA = frozenset({"check_argument_exit_swallow"})

# A check whose finding depends on a file the commit does not hold. Its verdict
# in one checkout is not its verdict in CI, so this audit leaves it out.
_NOT_AUDITED = frozenset(
    {
        # An import of a build output that `.gitignore` keeps untracked resolves
        # in a built checkout and dangles in a fresh clone.
        "check_relative_imports",
    }
)

# The names a check gives the token of its opt-out: OPT_OUT, EMPTY_OPT_OUT,
# _ALLOW, _ALLOW_TOKEN. A covered check that reads a token under another name
# makes the audit fail (``_credited``), so this list cannot fall behind.
_TOKEN_NAME = re.compile(r"^_?(?:[A-Z]+_)*(?:OPT_OUT|ALLOW)(?:_TOKEN)?$")


class Owner(NamedTuple):
    """One covered check, the tokens it declares, and the tokens judged here."""

    check: Check
    module: ModuleType
    declared: frozenset[str]
    judged: frozenset[str]


def _declared_tokens(module: ModuleType) -> frozenset[str]:
    return frozenset(
        value
        for name, value in vars(module).items()
        if _TOKEN_NAME.match(name) and isinstance(value, str)
    )


def _uncovered_strings(covered: set[str]) -> frozenset[str]:
    """Every string constant in a check this audit does not run."""
    found: set[str] = set()
    for path in Path(__file__).resolve().parent.glob("check_*.py"):
        if path.stem in covered or path.stem == Path(__file__).stem:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
    return frozenset(found)


def owners() -> list[Owner]:
    """The covered checks, each with the tokens this audit may judge."""
    loaded = []
    for check in CHECKS:
        if check.module in _NOT_AUDITED:
            continue
        if check.per_file or check.module in _ONE_FILE_EXTRA:
            module = importlib.import_module(f"ci_truth_serum.{check.module}")
            if declared := _declared_tokens(module):
                loaded.append((check, module, declared))
    shared = _uncovered_strings({check.module for check, _, _ in loaded})
    return [Owner(c, m, d, d - shared) for c, m, d in loaded]


def _is_annotation(comment: str, token: str, prose: bool) -> bool:
    """True when COMMENT holds TOKEN as an opt-out.

    A bare token that ends the comment (`# curl-retry-ok`) counts too, because
    some checks accept it. A token that running text names (`rather than
    pin-exempt.`) does not, and neither does a `:` with a placeholder reason,
    which is how a comment explains the syntax. Prose takes no bare form.
    """
    if annotation_re(token).first(comment):
        return True
    bare = annotation_re(token, require_reason=False).first(comment)
    return not prose and bare is not None and not comment[bare.end() :].strip()


def _annotations(
    said: dict[int, str], tokens: set[str], prose: bool
) -> set[tuple[int, str]]:
    return {
        (line, token)
        for line, comment in said.items()
        for token in tokens
        if _is_annotation(comment, token, prose)
    }


def _audit(owner: Owner, path: str, flags: list[str]) -> tuple[OptOutAudit, list[str]]:
    """Run OWNER over PATH under the audit; return the record and the refusals
    it printed.

    A finding prints as `<path>:<line>: …` and a refusal as `<path>: …`, which is
    the report format of every check in this pack (``run_message_checks``).
    """
    out = io.StringIO()
    with audit_opt_outs() as record:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            try:
                owner.module.main([*flags, path])
            except SystemExit as stop:
                if stop.code not in (0, 1, None):
                    raise RuntimeError(
                        f"{owner.check.module} exited {stop.code} on {path}:\n"
                        f"{out.getvalue()}"
                    ) from stop
    refusals = [
        line for line in out.getvalue().splitlines() if line.startswith(f"{path}: ")
    ]
    return record, refusals


def _credited(
    owner: Owner, record: OptOutAudit, raw: list[str], found: set[tuple[int, str]]
) -> set[tuple[int, str]]:
    """The annotations in FOUND that OWNER's lookups found, as RECORD holds them.

    A read whose comment text is on line N of the file names line N. Otherwise
    the line counts from some other text, such as a script inside a heredoc.
    That read credits each annotation of its token on a line that holds the
    same text, and no other. An annotation it might name is then never called
    stale, and a comment that only that other text holds is not judged here.
    """
    credited = set(record.scripts)
    for line, token, text in record.reads:
        if token not in owner.declared:
            raise RuntimeError(
                f"{owner.check.module} read the opt-out `{token}`, which it does "
                f"not declare under a name `_TOKEN_NAME` accepts."
            )
        if 1 <= line <= len(raw) and text in raw[line - 1]:
            credited.add((line, token))
            continue
        credited |= {(n, t) for n, t in found if t == token and text in raw[n - 1]}
    return credited


def judge(path: str, members: list[Owner], extra: dict[str, list[str]]) -> int:
    """Print each stale annotation in PATH; return 1 when any printed."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return 0
    mine = [o for o in members if o.judged and matches(path, o.check.kind)]
    if not mine:
        return 0
    # The comment readers err closed on a text they cannot parse, so they find no
    # annotation in it. Read here, that would be a clean pass over an unread file.
    reason = unreadable_reason(path, text)
    if reason is None and is_yaml_source(path) and not yaml_scannable(text):
        reason = "PyYAML cannot scan this file, so no comment in it was read."
    if reason is not None:
        print(f"{path}: {reason} (opt-outs not judged)", file=sys.stderr)
        return 1
    prose = is_prose_path(path)
    said = marker_comments(text, path, prose=prose)
    found = _annotations(said, {t for o in mine for t in o.judged}, prose)
    raw = text.split("\n")
    tokens = {token for _, token in found}
    live: set[tuple[int, str]] = set()
    unjudged: set[str] = set()
    status = 0
    for owner in (o for o in mine if o.judged & tokens):
        record, refusals = _audit(owner, path, extra.get(owner.check.module, []))
        for refusal in refusals:
            print(
                f"{refusal} ({owner.check.module}; opt-outs not judged)",
                file=sys.stderr,
            )
            status = 1
        if refusals:
            unjudged |= owner.declared
            continue
        live |= _credited(owner, record, raw, found)
        unjudged |= record.whole_file
    for line, token in sorted(found):
        if token in unjudged or (line, token) in live:
            continue
        names = [o.check.hook_id for o in mine if token in o.judged]
        verb = "reports" if len(names) == 1 else "report"
        print(
            f"{path}:{line}: the opt-out `{token}` suppresses nothing: "
            f"{' and '.join(names)} {verb} no finding that this line covers. "
            "Delete the annotation, or reword the comment so it does not read "
            "as one.",
            file=sys.stderr,
        )
        status = 1
    return status


def main(argv: list[str]) -> int:
    """Judge every opt-out annotation in the files on ARGV."""
    parser = argparse.ArgumentParser(prog="check_stale_opt_out")
    parser.add_argument(
        "--check-arg",
        action="append",
        default=[],
        metavar="CHECK=FLAG",
        help="a flag for one covered check, as `run_tier` takes it",
    )
    parser.add_argument("files", nargs="*")
    args = parser.parse_args(argv)
    extra: dict[str, list[str]] = {}
    for item in args.check_arg:
        module, sep, flag = item.partition("=")
        if not sep:
            parser.error(f"--check-arg takes <check>=<flag>, got {item!r}")
        extra.setdefault(module, []).append(flag)
    if not args.files:
        parser.error("no files to scan")
    members = owners()
    status = 0
    for path in args.files:
        status |= judge(path, members, extra)
    return status


if __name__ == "__main__":
    raise SystemExit(run_file_cli(main))

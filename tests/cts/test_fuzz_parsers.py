"""Property/fuzz tests for the ci-truth-serum lint parsers.

The single most important invariant for a linter is CRASH RESISTANCE: a hook is
fed whatever bytes happen to be staged -- adversarial shell, malformed YAML, weird
Unicode, BOMs, gigantic inputs, CR/CRLF/LS/PS line endings -- and must never raise
an *unexpected* exception. A crash turns a pre-commit hook into a hard block on an
unrelated commit; worse, in CI it can mask the very failure the lint exists to
surface. Each public text/doc entrypoint is driven here over generated input and
asserted to either return findings or cleanly do nothing.

Beyond no-crash, we assert domain invariants that hold for every input:

  * every reported line number is in range (1..#lines) and refers to a real line;
  * idempotence/stability where the API is a pure function of its text;
  * determinism -- the same input yields the same result.

These complement (do not replace) the example-based suites, which pin the exact
rule semantics. Fuzzing pins the contract that holds for ALL inputs.
"""

import string

from hypothesis import assume, given
from hypothesis import strategies as st

from tests._helpers import load_hook

# --- Loaded hooks (drive their public functions directly) --------------------
exit_suppression = load_hook("check_exit_suppression.py", "fuzz_exit_suppression")
stderr_suppression = load_hook("check_stderr_suppression.py", "fuzz_stderr_suppression")
pipefail_grep_pipe = load_hook("check_pipefail_grep_pipe.py", "fuzz_pipefail_grep_pipe")
pinned_downloads = load_hook("check_pinned_downloads.py", "fuzz_pinned_downloads")
pinned_base_images = load_hook("check_pinned_base_images.py", "fuzz_pinned_base_images")
global_stdio_swap = load_hook("check_global_stdio_swap.py", "fuzz_global_stdio_swap")
workflow_pipefail = load_hook("check_workflow_pipefail.py", "fuzz_workflow_pipefail")
inline_run_length = load_hook("check_inline_run_length.py", "fuzz_inline_run_length")
linecheck = load_hook("_linecheck.py", "fuzz_linecheck")

# `violations(text) -> list[int]` line-oriented detectors. Each maps text to the
# 1-based physical line numbers it flags.
LINE_DETECTORS = {
    "check_exit_suppression": exit_suppression.violations,
    "check_stderr_suppression": stderr_suppression.violations,
    "check_pipefail_grep_pipe": pipefail_grep_pipe.violations,
    "check_pinned_downloads": pinned_downloads.violations,
    "check_pinned_base_images": pinned_base_images.violations,
    "check_global_stdio_swap": global_stdio_swap.violations,
}


# --- Input strategies --------------------------------------------------------

# A grab-bag of tokens the detectors actually look for, so generated text isn't
# all inert noise -- it hits real branches (suppressors, downloaders, redirects,
# heredocs, FROM lines, stdio swaps, annotations, comment/quote boundaries).
_INTERESTING_TOKENS = [
    "|| true",
    "|| :",
    "| tee",
    "|& tee",
    "2>&1",
    ">|",
    "curl -o f https://x",
    "curl -O https://x",
    "wget -O t https://x",
    "sha256sum -c f",
    "cosign verify x",
    "gpg --verify x",
    "FROM node:22",
    "FROM scratch",
    "FROM x@sha256:" + "a" * 64,
    "AS build",
    "--platform=linux/amd64",
    "sys.stdout = x",
    "redirect_stdout(buf)",
    "set -o pipefail",
    "<<EOF",
    "<<'EOF'",
    "EOF",
    "$(cmd)",
    "`cmd`",
    "# allow-exit-suppress: x",
    "# pin-exempt: x",
    "# allow-stderr-suppress: x",
    "# allow-stdio-swap: x",
    "# allow-no-pipefail: x",
    "echo done",
    "docker compose up",
    "DC=(docker compose)",
    '"${DC[@]}" up',
    "var=$(x) || true",
    "  ",
    "#",
    "'",
    '"',
    "\\",
    "\t",
]

# Newline variants the parsers must tolerate: LF, CRLF, lone CR, vertical tab,
# form feed, and the Unicode breaks str.splitlines() recognises -- U+0085 (NEL),
# U+2028 (line sep), U+2029 (paragraph sep). Written as escapes so no invisible
# byte hides in this source (CLAUDE.md: centralize/escape special chars).
_NEWLINES = ["\n", "\r\n", "\r", "\x0b", "\x0c", "\x85", "\u2028", "\u2029"]

# Adversarial unicode: bidi override (U+202E), zero-width space/joiner
# (U+200B/U+200D), BOM (U+FEFF), a combining acute (U+0301), an astral emoji,
# and bracket noise. Escapes only -- a literal here once smuggled a NUL in.
_WEIRD_CHARS = st.sampled_from(
    ["\u202e", "\u200b", "\u200d", "\ufeff", "\u0301", "\U0001f600"] + list("(){}[]")
)


@st.composite
def source_text(draw: st.DrawFn) -> str:
    """A line-structured blob mixing interesting tokens, noise, and odd newlines."""
    n_lines = draw(st.integers(min_value=0, max_value=40))
    lines = []
    for _ in range(n_lines):
        kind = draw(st.integers(min_value=0, max_value=3))
        if kind == 0:
            lines.append(draw(st.sampled_from(_INTERESTING_TOKENS)))
        elif kind == 1:
            lines.append(
                draw(st.text(alphabet=string.printable, max_size=60))
                + draw(st.sampled_from(_INTERESTING_TOKENS))
            )
        elif kind == 2:
            lines.append(draw(st.text(_WEIRD_CHARS, max_size=20)))
        else:
            lines.append(draw(st.text(max_size=80)))
    sep = draw(st.sampled_from(_NEWLINES))
    text = sep.join(lines)
    if draw(st.booleans()):
        text += draw(st.sampled_from(_NEWLINES))
    return text


# YAML-ish strategy: real YAML mixed with garbage so the doc-level analyzers see
# both parseable documents (exercising the traversal) and parse errors.
_YAML_FRAGMENTS = [
    "on:\n  pull_request:\n    paths: ['a']\n",
    "on:\n  pull_request: null\n",
    "jobs:\n  a:\n    steps:\n      - run: cmd | tee log\n",
    "jobs:\n  a:\n    steps:\n      - run: cmd\n        shell: sh\n",
    "jobs:\n  a:\n    steps:\n      - with:\n          runCmd: cmd | tee x\n",
    "runs:\n  steps:\n    - run: a | b\n",
    "concurrency:\n  group: x\n",
    "concurrency:\n  group: ci-${{ github.ref }}\n  cancel-in-progress: true\n",
    "jobs:\n  report:\n    if: always()\n    # required-check: true\n",
    "jobs:\n  a:\n    name: t-${{ matrix.os }}\n    strategy:\n      matrix:\n        os: [x, y]\n",
    "[]",
    "null",
    "42",
    "- a\n- b\n",
    "key: : : :\n",
    "a: [unterminated\n",
    "\ttabs-are-illegal-indent: 1\n",
]


@st.composite
def yaml_text(draw: st.DrawFn) -> str:
    """A blob of concatenated YAML fragments plus optional garbage lines."""
    parts = draw(st.lists(st.sampled_from(_YAML_FRAGMENTS), max_size=4))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=60)))
    return "\n".join(parts)


# --- Generic invariants over the line detectors ------------------------------


def _assert_valid_linenos(text: str, hits: list[int]) -> None:
    n = len(text.splitlines())
    for lineno in hits:
        assert isinstance(lineno, int)
        # Every reported line must exist (1-based, within the file).
        assert 1 <= lineno <= n, (lineno, n)
    # Findings are line numbers, reported once each, in nondecreasing order
    # (the scanners walk the file top-to-bottom).
    assert hits == sorted(hits)
    assert len(hits) == len(set(hits))


@given(text=source_text())
def test_line_detectors_never_crash_and_report_real_lines(text: str) -> None:
    for name, detect in LINE_DETECTORS.items():
        hits = detect(text)
        assert isinstance(hits, list), name
        _assert_valid_linenos(text, hits)


@given(text=source_text())
def test_line_detectors_are_deterministic(text: str) -> None:
    # A pure text -> findings function must be stable across calls; the mutation
    # and CI runs both rely on this, and a hidden global-state leak (e.g. a regex
    # accumulating state, a shared mutable default) would surface here.
    for detect in LINE_DETECTORS.values():
        assert detect(text) == detect(text)


@given(text=source_text())
def test_line_detectors_idempotent_under_repeat(text: str) -> None:
    # Doubling the file (with a separating newline) can only re-flag lines, never
    # crash or invent out-of-range numbers.
    doubled = text + "\n" + text
    for detect in LINE_DETECTORS.values():
        _assert_valid_linenos(doubled, detect(doubled))


# --- Doc-level analyzers -----------------------------------------------------


def _is_line_message(item: object) -> bool:
    """analyze() yields (line, message): line is the step's source line (int) or
    None when the doc carried no line tags; message is the human-readable string."""
    if not (isinstance(item, tuple) and len(item) == 2):
        return False
    line, msg = item
    return (line is None or isinstance(line, int)) and isinstance(msg, str)


@given(text=yaml_text())
def test_workflow_pipefail_analyze_never_crashes(text: str) -> None:
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)  # check_file swallows these; analyze is only fed valid docs
    out = workflow_pipefail.analyze(doc)
    assert isinstance(out, list)
    assert all(_is_line_message(item) for item in out)


@given(text=yaml_text())
def test_inline_run_length_analyze_never_crashes(text: str) -> None:
    import yaml

    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    out = inline_run_length.analyze(doc)
    assert isinstance(out, list)
    assert all(_is_line_message(item) for item in out)


@given(
    obj=st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(max_size=20),
        lambda children: (
            st.lists(children, max_size=4)
            | st.dictionaries(st.text(max_size=8), children, max_size=4)
        ),
        max_leaves=30,
    )
)
def test_doc_analyzers_tolerate_arbitrary_parsed_objects(obj: object) -> None:
    # safe_load can return any JSON-ish shape (scalar, list, nested dict). The
    # analyzers must treat a non-workflow shape as "nothing to report", not crash.
    assert isinstance(workflow_pipefail.analyze(obj), list)
    assert isinstance(inline_run_length.analyze(obj), list)


# --- Shared _linecheck machinery ---------------------------------------------


@given(text=yaml_text())
def test_required_check_contexts_never_crashes(text: str) -> None:
    # required_check_contexts mirrors the path-based workflow lints: it does NOT
    # swallow a YAMLError (a malformed/non-printable workflow makes pre-commit and
    # sync-required-checks surface the parse error loudly, by design). On any
    # PARSEABLE input it must return a clean list of strings rather than crash on
    # the traversal -- that is the invariant this guards.
    import yaml

    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    out = linecheck.required_check_contexts(text)
    assert isinstance(out, list)
    assert all(isinstance(c, str) for c in out)


@given(text=source_text())
def test_job_blocks_linenos_in_range(text: str) -> None:
    blocks = linecheck._job_blocks(text)
    assert isinstance(blocks, dict)
    n = len(text.splitlines())
    for name, (lineno, block) in blocks.items():
        assert isinstance(name, str)
        assert 1 <= lineno <= n
        assert isinstance(block, str)


@given(
    matrix=st.dictionaries(
        st.text(alphabet=string.ascii_letters + "_-", min_size=1, max_size=6),
        st.lists(st.integers() | st.text(max_size=6), max_size=4)
        | st.lists(
            st.dictionaries(st.text(max_size=6), st.integers(), max_size=3), max_size=3
        ),
        max_size=4,
    )
)
def test_matrix_combinations_never_crashes(matrix: dict) -> None:
    combos = linecheck.matrix_combinations(matrix)
    assert isinstance(combos, list)
    assert all(isinstance(c, dict) for c in combos)


@given(
    name=st.text(max_size=40),
    matrix=st.dictionaries(
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=4),
        st.lists(st.text(max_size=6), max_size=3),
        max_size=3,
    ),
)
def test_expand_name_never_crashes(name: str, matrix: dict) -> None:
    out = linecheck.expand_name(name, matrix)
    assert isinstance(out, list)
    assert all(isinstance(c, str) for c in out)


# --- check_pinned_base_images: the fix/resolver helpers (offline only) --------


@given(text=source_text())
def test_fix_text_offline_never_crashes(text: str) -> None:
    # Resolve is injected so no network is touched; a stub that always fails
    # exercises the unfixed path. With every resolution failing, nothing is
    # rewritten: the text is unchanged and every violation is reported unfixed.
    def fail(_image: str) -> str:
        raise pinned_base_images.DigestResolutionError("offline")

    new_text, fixed, unfixed = pinned_base_images.fix_text(text, resolve=fail)
    assert isinstance(new_text, str)
    assert new_text == text
    assert fixed == []
    assert sorted(n for n, _ in unfixed) == sorted(pinned_base_images.violations(text))


@given(text=source_text())
def test_fix_text_with_stub_digest_preserves_line_count(text: str) -> None:
    stub = "sha256:" + "b" * 64

    def ok(_image: str) -> str:
        return stub

    new_text, fixed, unfixed = pinned_base_images.fix_text(text, resolve=ok)
    assert unfixed == []
    # The rewrite is line-local: it never changes how many lines the file has.
    assert len(new_text.splitlines(keepends=True)) == len(
        text.splitlines(keepends=True)
    )
    # Every line that was fixed is now digest-pinned, so a re-scan never re-flags
    # it (the set of fixed lines and the new violation set are disjoint).
    assert set(pinned_base_images.violations(new_text)).isdisjoint(fixed)


@given(image=st.text(max_size=40))
def test_split_ref_never_crashes(image: str) -> None:
    name, tag = pinned_base_images._split_ref(image)
    assert isinstance(name, str) and isinstance(tag, str)
    # A ref carrying no tag at all defaults to "latest"; a ref with an explicit
    # (even empty, e.g. "node:") tag keeps what it spelled. Either way two strings
    # come back and nothing raises -- a bad/empty tag degrades to a registry 404
    # (DigestResolutionError), leaving the FROM flagged rather than crashing --fix.
    if "@" not in image and ":" not in image.rsplit("/", 1)[-1]:
        assert tag == "latest"

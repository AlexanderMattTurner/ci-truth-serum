"""Property/fuzz tests for the reliability checks added alongside
check_token_fallback: the detectors are fed arbitrary text assembled from
tokens that hit their real branches and must never raise, every reported line
number must refer to a real line, and the same input must yield the same
result (the same contract as the sibling fuzz suites).

Covered here: check_token_fallback, check_workflow_secret_names,
check_provenance_repo_url (URL normalization), check_pin_comment_truth,
check_divergent_action_pins, check_stderr_merge_parse, check_echo_fallback,
check_case_default, check_soft_timeout, check_flock_fixed_fd,
check_bare_return_status,
check_lockstep_pins, check_cron_comment, check_toolchain_skips,
check_conclusion_coverage, and release_canary's changelog/semver parsing.
"""

from hypothesis import given
from hypothesis import strategies as st

from tests._helpers import load_hook

token_fallback = load_hook("check_token_fallback.py", "fuzz_check_token_fallback")
secret_names = load_hook(
    "check_workflow_secret_names.py", "fuzz_check_workflow_secret_names"
)
provenance = load_hook("check_provenance_repo_url.py", "fuzz_check_provenance_repo_url")
pin_comment = load_hook("check_pin_comment_truth.py", "fuzz_check_pin_comment_truth")
divergent_pins = load_hook(
    "check_divergent_action_pins.py", "fuzz_check_divergent_action_pins"
)
stderr_merge = load_hook("check_stderr_merge_parse.py", "fuzz_check_stderr_merge_parse")
echo_fallback = load_hook("check_echo_fallback.py", "fuzz_check_echo_fallback")
case_default = load_hook("check_case_default.py", "fuzz_check_case_default")
soft_timeout = load_hook("check_soft_timeout.py", "fuzz_check_soft_timeout")
flock_fd = load_hook("check_flock_fixed_fd.py", "fuzz_check_flock_fixed_fd")
bare_return = load_hook("check_bare_return_status.py", "fuzz_check_bare_return_status")
lockstep = load_hook("check_lockstep_pins.py", "fuzz_check_lockstep_pins")
cron_comment = load_hook("check_cron_comment.py", "fuzz_check_cron_comment")
toolchain = load_hook("check_toolchain_skips.py", "fuzz_check_toolchain_skips")
canary = load_hook("release_canary.py", "fuzz_release_canary")
external_clock = load_hook(
    "check_external_clock_targets.py", "fuzz_check_external_clock_targets"
)

_SHA = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"

# Tokens hitting the detectors' real branches: token keys, secret refs, SHA
# pins and version comments, merges/pipes/substitutions, fallbacks, case arms,
# cron lines and cadence claims, skipif shapes, changelog headings.
_TOKENS = [
    "token: ${{ secrets.A || secrets.B }}",
    "GH_TOKEN: ${{ secrets.A||secrets.B }}",
    "x: ${{ secrets.ONE }}",
    "# token-fallback-ok: designed",
    "v: ${{ vars.ORG }}",
    f"- uses: actions/checkout@{_SHA} # v6",
    f"- uses: actions/checkout@{_SHA} # v7.0.0",
    f"- uses: actions/checkout@{_SHA}",
    "- uses: actions/checkout@v6",
    "# pin-comment-ok",
    "v=$(cmd 2>&1 | tail -1)",
    "out=$(cmd 2>&1)",
    'echo "$out" | grep x',
    "[[ $out == 1.2.3 ]]",
    "((out > 3))",
    "# stderr-merge-ok: sentinel",
    'v=$(cmd || echo "error")',
    "cmd || echo failed",
    'cmd || { echo "x" >&2; exit 1; }',
    "# echo-fallback-ok: sentinel",
    'case "$1" in',
    "  major)",
    "  *)",
    "  *.txt)",
    "  : ;;",
    "esac",
    "# case-default-ok: two values",
    "timeout 60 cmd",
    "timeout -k 10 60 cmd",
    'timeout --kill-after=10 "$CAP" cmd',
    "timeout -s KILL 60 cmd",
    "run=(timeout 600)",
    "sudo timeout 300 apt-get update",
    "# allow-soft-timeout: dpkg holds SIGTERM",
    "flock -x 9",
    "flock -w 5 200",
    'flock -x "$lock_fd"',
    "flock -x /var/lock/deploy cmd",
    "exec {lock_fd}>/var/lock/deploy",
    "# allow-fixed-fd: this script opens fd 9 itself",
    '- cron: "0 6 * * 1"',
    "# daily",
    "# every 15 minutes",
    "# cron-comment-ok",
    'pytest.mark.skipif(shutil.which("jq") is None, reason="r")',
    'pytest.mark.skipif(shutil.which("x") is None and not os.environ.get("CI"), reason="r")',
    "# toolchain-skip-ok: local helper",
    '[[ -n "$x" ]] && return',
    '[[ -n "$x" ]] || return',
    "cmd || return",
    "(( n > 0 )) || return",
    "! is_ready || return",
    "cmd && exit",
    "return 0",
    "# allow-bare-return: the caller wants 0",
    "f() {",
    "}",
    "## [1.2.3] - 2026-01-01",
    "## Unreleased",
    "1.10.0",
    "pkgver=1.2.3",
    "pkgver=$(git describe)",
    "pkgver() {",
    "pkgrel=1",
    "$(",
    ")",
    "`",
    "\\",
    "|",
    "||",
    ";;",
    "#",
    '"',
    "'",
    "    ",
]

_text = st.lists(
    st.one_of(st.sampled_from(_TOKENS), st.text(max_size=12)), max_size=30
).map("\n".join)

# (detector, call) pairs sharing the never-crash + valid-lines + deterministic
# contract. Each returns either list[int] or list[(int, …)].
_LINE_DETECTORS = [
    ("check_token_fallback", token_fallback.violations),
    ("check_stderr_merge_parse", stderr_merge.violations),
    ("check_echo_fallback", echo_fallback.violations),
    ("check_case_default", case_default.violations),
    ("check_soft_timeout", soft_timeout.violations),
    ("check_flock_fixed_fd", flock_fd.violations),
    ("check_bare_return_status", bare_return.violations),
    ("check_cron_comment", cron_comment.violations),
    ("check_toolchain_skips", toolchain.violations),
]


def _line_numbers(result: list) -> list[int]:
    return [entry[0] if isinstance(entry, tuple) else entry for entry in result]


@given(_text)
def test_line_detectors_never_crash_and_report_real_lines(text: str) -> None:
    n_lines = len(text.splitlines())
    for name, detector in _LINE_DETECTORS:
        result = detector(text)
        assert detector(text) == result, name  # deterministic
        for lineno in _line_numbers(result):
            assert 1 <= lineno <= max(n_lines, 1), name


@given(_text)
def test_secret_names_extraction_is_total_and_deterministic(text: str) -> None:
    names = secret_names.referenced_names(text)
    assert names == secret_names.referenced_names(text)
    assert all(isinstance(n, str) and n for n in names)
    assert secret_names.check_repo(names, text) is not None
    parsed = secret_names.parse_allowlist(text)
    assert all(isinstance(n, str) for n in parsed)


@given(st.text(max_size=200))
def test_normalize_repo_url_is_total(url: str) -> None:
    result = provenance.normalize_repo_url(url)
    assert result is None or ("/" in result and result == result.lower())


@given(_text)
def test_pin_records_and_cross_file_check_are_total(text: str) -> None:
    records = pin_comment.pin_records(text)
    n_lines = len(text.splitlines())
    for lineno, pin, version, opted in records:
        assert 1 <= lineno <= max(n_lines, 1)
        assert "@" in pin
        assert version is None or version.startswith("v")
        assert isinstance(opted, bool)
    findings = pin_comment.check_files([("a.yaml", text), ("b.yaml", text)])
    assert all(isinstance(line, int) for _p, line, _m in findings)


@given(_text, _text)
def test_divergent_action_pins_check_files_is_total(t1: str, t2: str) -> None:
    # A file the YAML parser rejects is a violation (line 1), never a crash and
    # never a silent pass — check_files owns that recovery, so it is the
    # fuzzed/registered symbol rather than the lower-level pin_records.
    findings = divergent_pins.check_files([("a.yaml", t1), ("b.yaml", t2)])
    for path, line, message in findings:
        assert path in ("a.yaml", "b.yaml")
        assert line >= 1
        assert "divergent pin" in message or "could not parse as YAML" in message


@given(_text, _text)
def test_lockstep_check_pair_is_total_with_fixed_regexes(t1: str, t2: str) -> None:
    msgs = lockstep.check_pair("a", t1, r"pin=(\S+)", "b", t2, r"pin=(\S+)")
    assert isinstance(msgs, list)
    assert all(isinstance(m, str) for m in msgs)


@given(st.text(max_size=120))
def test_cron_classify_is_total(expr: str) -> None:
    result = cron_comment.classify(expr)
    assert result is None or isinstance(result, str)


@given(_text, st.text(max_size=200))
def test_external_clock_targets_is_total(manifest: str, workflow: str) -> None:
    n_lines = len(manifest.splitlines())
    # Two resolvers exercise both arms: every entry missing (404 path), and every
    # entry present with arbitrary YAML (the parse / trigger / input paths).
    for resolve in (lambda _n: None, lambda _n, wf=workflow: wf):
        result = external_clock.violations(manifest, resolve)
        assert external_clock.violations(manifest, resolve) == result  # deterministic
        for lineno, message in result:
            assert 1 <= lineno <= max(n_lines, 1)
            assert isinstance(message, str) and message


@given(_text)
def test_changelog_and_semver_parsing_are_total(text: str) -> None:
    top = canary.changelog_top_version(text)
    assert top is None or isinstance(top, str)
    best = canary.max_semver(text.split())
    assert best is None or canary.semver_key(best) is not None
    pkgver = canary.pkgbuild_version(text)
    assert pkgver is None or isinstance(pkgver, str)


# ── check_conclusion_coverage: one question, three parsers ──────────────
conclusion_coverage = load_hook(
    "check_conclusion_coverage.py", "fuzz_check_conclusion_coverage"
)

# Tokens that reach the real branches of all three surfaces: a workflow gate in
# both comparison and `contains(fromJSON(...))` spellings, a `[[ ]]` test, a
# `case` arm, a Python comparison and a membership test against a constant.
_CONCLUSION_TOKENS = [
    "    if: github.event.workflow_run.conclusion == 'failure'",
    '    if: contains(fromJSON(\'["failure","timed_out"]\'), '
    "github.event.workflow_run.conclusion)",
    "    if: contains('failure cancelled', steps.a.conclusion)",
    'if [[ "$conclusion" == failure ]]; then n; fi',
    'if [[ "$conclusion" != success ]]; then n; fi',
    'case "$conclusion" in\n  failure|timed_out) n ;;\nesac',
    "conclusion=$(gh run view --json conclusion -q .conclusion)",
    'if run["conclusion"] == "failure":\n    n()',
    'RED = frozenset({"failure"})',
    "if conclusion in RED:\n    n()",
    "# allow-conclusion-subset: judged elsewhere",
    "jobs:",
    "  notify:",
    "conclusion",
    "'",
]

_conclusion_text = st.lists(
    st.one_of(st.sampled_from(_CONCLUSION_TOKENS), st.text(max_size=12)), max_size=20
).map("\n".join)

_CONCLUSION_PATHS = [".github/workflows/a.yaml", "route.sh", "scan.py", "notes.txt"]


@given(_conclusion_text, st.sampled_from(_CONCLUSION_PATHS))
def test_conclusion_coverage_violations_is_total(text: str, path: str) -> None:
    n_lines = len(text.split("\n"))
    result = conclusion_coverage.violations(text, path)
    assert conclusion_coverage.violations(text, path) == result  # deterministic
    for lineno, detail in result:
        assert 1 <= lineno <= max(n_lines, 1)
        assert isinstance(detail, str) and detail

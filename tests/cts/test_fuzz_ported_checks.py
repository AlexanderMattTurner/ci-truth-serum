"""Property/fuzz tests for the 21 lints ported alongside `_cts_py_imports`:
`check_bare_mkdir`, `check_big_tuple_annotations`, `check_curl_retry`,
`check_cwd_scoped_git`, `check_dead_shell_functions`,
`check_duplicate_class_names`, `check_duplicate_module_constant`,
`check_env_arith`, `check_path_shadowed_interpreter`,
`check_positional_git_argv`, `check_relative_imports`, `check_retry_loop`,
`check_shell_source_declarations`, `check_sleep_as_sync`,
`check_sparse_checkout_closure`, `check_test_helper_kwargs`,
`check_truncating_pr_json`, `check_unbounded_waits`,
`check_unreset_module_state`, `check_unspecified_encoding`, and
`check_wall_clock_assertions`.

Same contract as the sibling fuzz suites: the public parsing entry point must
never crash on adversarial text, every reported line number must be a real
line of the input, and the result stays well-typed. A handful of these
entrypoints call `ast.parse`/`yaml.load` directly with no try/except of their
own (unlike the ones routed through `_cts_py_ast.trees` or a self-catching
`yaml.load`) — those properties restrict the generated text to what the
function's own contract covers (parseable Python, parseable YAML) via
``assume``, mirroring the yaml_text() pattern in test_fuzz_parsers.py.
"""

import string
from pathlib import Path

import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import commit_all, init_test_repo, load_hook

py_imports = load_hook("_cts_py_imports.py", "fuzz_py_imports")
bare_mkdir = load_hook("check_bare_mkdir.py", "fuzz_bare_mkdir")
big_tuple = load_hook("check_big_tuple_annotations.py", "fuzz_big_tuple_annotations")
curl_retry = load_hook("check_curl_retry.py", "fuzz_curl_retry")
cwd_scoped_git = load_hook("check_cwd_scoped_git.py", "fuzz_cwd_scoped_git")
dead_shell_functions = load_hook(
    "check_dead_shell_functions.py", "fuzz_dead_shell_functions"
)
duplicate_class_names = load_hook(
    "check_duplicate_class_names.py", "fuzz_duplicate_class_names"
)
duplicate_module_constant = load_hook(
    "check_duplicate_module_constant.py", "fuzz_duplicate_module_constant"
)
env_arith = load_hook("check_env_arith.py", "fuzz_env_arith")
path_shadowed_interpreter = load_hook(
    "check_path_shadowed_interpreter.py", "fuzz_path_shadowed_interpreter"
)
positional_git_argv = load_hook(
    "check_positional_git_argv.py", "fuzz_positional_git_argv"
)
relative_imports = load_hook("check_relative_imports.py", "fuzz_relative_imports")
retry_loop = load_hook("check_retry_loop.py", "fuzz_retry_loop")
shell_source_declarations = load_hook(
    "check_shell_source_declarations.py", "fuzz_shell_source_declarations"
)
sleep_as_sync = load_hook("check_sleep_as_sync.py", "fuzz_sleep_as_sync")
sparse_checkout_closure = load_hook(
    "check_sparse_checkout_closure.py", "fuzz_sparse_checkout_closure"
)
test_helper_kwargs = load_hook("check_test_helper_kwargs.py", "fuzz_test_helper_kwargs")
truncating_pr_json = load_hook("check_truncating_pr_json.py", "fuzz_truncating_pr_json")
unbounded_waits = load_hook("check_unbounded_waits.py", "fuzz_unbounded_waits")
unreset_module_state = load_hook(
    "check_unreset_module_state.py", "fuzz_unreset_module_state"
)
unspecified_encoding = load_hook(
    "check_unspecified_encoding.py", "fuzz_unspecified_encoding"
)
wall_clock_assertions = load_hook(
    "check_wall_clock_assertions.py", "fuzz_wall_clock_assertions"
)


def _line_numbers(result: list) -> list[int]:
    return [entry[0] if isinstance(entry, tuple) else entry for entry in result]


def _assert_valid_linenos(text: str, hits: list) -> None:
    n = max(len(text.splitlines()), 1)
    for lineno in _line_numbers(hits):
        assert isinstance(lineno, int)
        assert 1 <= lineno <= n, (lineno, n)


# ── shared text strategy: a grab-bag of tokens hitting real branches across
# both the shell- and Python-grammar checks, plus generic noise ────────────

_TOKENS = [
    "mkdir -p /tmp/x",
    "mkdir /tmp/x",
    "sudo mkdir -pm 700 /tmp/x",
    "# bare-mkdir-ok: reason",
    "curl -o f https://x",
    "curl --retry 3 -o f https://x",
    "curl -o - https://x",
    "wget -O f https://x",
    "# curl-retry-ok",
    "git ls-remote origin",
    "git -C dir fetch",
    "git fetch",
    "timeout 30 git fetch",
    "git push",
    "git rev-parse HEAD",
    "# allow-unbounded: reason",
    'subprocess.run(["git", "merge", "--abort"])',
    'subprocess.run(["git", "-C", repo, "status"])',
    'subprocess.run(["git", "log"], cwd=repo)',
    "# cwd-git-ok: reason",
    "$((SECONDS + TIMEOUT))",
    "$((i + 1))",
    "TIMEOUT=90",
    "# env-arith-ok: reason",
    'line == "git rev-parse HEAD"',
    'ln.startswith("git fetch")',
    '[ "$1" = ls-remote ]',
    'case "$1" in\n  rev-parse)\n    ;;\nesac',
    "# allow-positional-git-argv: reason",
    "while [ $i -lt 3 ]; do\n  sleep 5\n  i=$((i + 1))\ndone",
    "for i in 1 2 3; do sleep 1; done",
    "while ((n < max)); do sleep 1; ((n++)); done",
    "while ((SECONDS < deadline)); do sleep 1; done",
    "# retry-loop-ok: reason",
    'source "$DIR/lib.sh"',
    "# shellcheck source=lib.sh",
    "# shellcheck disable=SC1090",
    ". ./lib.sh",
    "gh pr view --json files",
    "gh pr list --json commits,comments",
    "gh api repos/x/y",
    "# truncating-pr-json-ok: reason",
    "def f():\n    time.sleep(1)\n    assert True\n",
    "while time.monotonic() < deadline:\n    pass\n",
    "# allow-sleep: reason",
    "x: tuple[str, int, bool]\n",
    "y: tuple[str, ...]\n",
    "# big-tuple-ok: reason",
    "CONST = 1\nCONST = 2\n",
    "CONST = CONST + 1\n",
    "# allow-duplicate-constant: reason",
    "class Foo:\n    pass\n",
    "# allow-duplicate-class: reason",
    "GLOBAL_X = {}\ndef f():\n    global GLOBAL_X\n    GLOBAL_X['a'] = 1\n",
    "def _reset_process_state():\n    pass\n",
    "# allow-unreset-state: reason",
    'open("f")\n',
    'open("f", encoding="utf-8")\n',
    'Path("f").read_text()\n',
    'tempfile.NamedTemporaryFile(mode="w")\n',
    "# allow-unspecified-encoding: reason",
    'import "./lib"',
    'import { x } from "./lib.mjs"',
    "start = time.monotonic()\nelapsed = time.monotonic() - start\nassert elapsed < 5\n",
    "const start = Date.now();\nassert(Date.now() - start < 5);",
    "python3 script.py",
    "python -m pytest tests/x.py",
    "on:\n  pull_request:\njobs:\n  a:\n    steps:\n      - uses: anthropics/claude-code-action\n      - run: python3 x.py\n",
    "jobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n        with:\n          sparse-checkout: |\n            .github/scripts\n",
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

_PATHS = st.sampled_from(
    ["a.sh", "a.py", "a.js", "a.ts", "a.mjs", "test.py", "a.yaml", "no_suffix"]
)


# ── entrypoints that are already crash-proof on ANY text: routed through
# `_cts_bash_ast.parse`, `_cts_py_ast.trees`, `_cts_js_ast.parse`, or a self-caught
# `yaml.load`/`ast.parse` ───────────────────────────────────────────────────

_TOTAL_LINE_DETECTORS = [
    ("check_bare_mkdir", bare_mkdir.violations),
    ("check_curl_retry", curl_retry.violations),
    ("check_cwd_scoped_git", cwd_scoped_git.violations),
    ("check_env_arith", env_arith.violations),
    ("check_positional_git_argv", positional_git_argv.violations),
    ("check_retry_loop", retry_loop.violations),
    ("check_unbounded_waits", unbounded_waits.violations),
    ("check_duplicate_module_constant", duplicate_module_constant.violations),
    ("check_unspecified_encoding", unspecified_encoding.violations),
    (
        "check_big_tuple_annotations",
        lambda text: [line for line, _count in big_tuple.violations(text)],
    ),
]


@given(_text)
def test_total_line_detectors_never_crash_and_report_real_lines(text: str) -> None:
    for name, detector in _TOTAL_LINE_DETECTORS:
        result = detector(text)
        assert detector(text) == result, name  # deterministic
        _assert_valid_linenos(text, result)


@given(_text, _PATHS)
def test_relative_imports_violations_is_total(text: str, path: str) -> None:
    result = relative_imports.violations(text, path)
    assert relative_imports.violations(text, path) == result
    _assert_valid_linenos(text, result)


@given(_text, _PATHS, st.lists(st.text(max_size=10), max_size=3))
def test_wall_clock_assertions_violations_is_total(
    text: str, path: str, scalers: list[str]
) -> None:
    result = wall_clock_assertions.violations(text, path, frozenset(scalers))
    assert wall_clock_assertions.violations(text, path, frozenset(scalers)) == result
    _assert_valid_linenos(text, result)


@given(_text, st.lists(st.sampled_from(["anthropics/claude-code-action", "x/y"])))
def test_path_shadowed_interpreter_violations_is_total(
    text: str, agent_actions: list[str]
) -> None:
    # A wrap in a minimal workflow shape sometimes reaches the real jobs/steps
    # traversal; the raw text alone exercises the "not a dict"/YAMLError arms.
    result = path_shadowed_interpreter.violations(text, tuple(agent_actions))
    assert isinstance(result, list)
    n_lines = max(len(text.splitlines()), 1)
    for line, message in result:
        assert 1 <= line <= n_lines
        assert isinstance(message, str) and message


@given(_text)
def test_truncating_pr_json_violations_is_total(text: str) -> None:
    result = truncating_pr_json.violations(text)
    assert truncating_pr_json.violations(text) == result
    _assert_valid_linenos(text, result)


@given(_text)
def test_truncating_pr_json_python_violations_is_total(text: str) -> None:
    result = truncating_pr_json.python_violations(text)
    assert truncating_pr_json.python_violations(text) == result
    _assert_valid_linenos(text, result)


@given(_text, st.lists(st.text(alphabet=string.ascii_letters, max_size=8), max_size=3))
def test_shell_source_declarations_violations_is_total(
    text: str, search_paths: list[str]
) -> None:
    root = Path("/does/not/exist")  # never touched — resolve_target only stats
    result = shell_source_declarations.violations(
        "lib/caller.sh", text, root, search_paths
    )
    assert isinstance(result, list)
    n_lines = max(len(text.splitlines()), 1)
    for line, message in result:
        assert 1 <= line <= n_lines
        assert isinstance(message, str) and message


# ── check_sparse_checkout_closure.checkouts: text-only (the `workflow: Path`
# argument is stored, never read from disk), but a `yaml.load` this module
# does not wrap itself — restrict generated text to what parses as YAML,
# exactly like test_fuzz_parsers.py's yaml_text() pattern. Whether an
# unparseable workflow crashes it is checked separately below. ────────────

_YAML_FRAGMENTS = [
    "on:\n  pull_request:\njobs:\n  a:\n    steps:\n      - uses: actions/checkout@v4\n"
    "        with:\n          sparse-checkout: |\n            .github/scripts\n",
    "jobs:\n  a:\n    steps:\n      - run: python3 .github/scripts/x.py\n",
    "jobs:\n  a:\n    steps:\n      - uses: ./actions/local\n",
    "[]",
    "null",
    "42",
    "- a\n- b\n",
    "key: value\n",
]


@st.composite
def _yaml_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_YAML_FRAGMENTS), max_size=3))
    if draw(st.booleans()):
        parts.append(draw(st.text(alphabet=string.printable, max_size=40)))
    return "\n".join(parts)


@given(_yaml_text())
def test_sparse_checkout_closure_checkouts_is_total_on_parseable_yaml(
    text: str,
) -> None:
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)
    result = sparse_checkout_closure.checkouts(text, Path("wf.yaml"))
    assert sparse_checkout_closure.checkouts(text, Path("wf.yaml")) == result
    assert isinstance(result, list)
    for checkout in result:
        assert 1 <= checkout.line


# ── entrypoints whose module calls `ast.parse` directly, with no try/except
# of its own — restrict generated text to what actually parses as Python,
# via `assume`, matching the yaml_text() convention above. ─────────────────

_PY_FRAGMENTS = [
    "class Foo:\n    pass\n",
    "class Foo(NamedTuple):\n    pass\n",
    "def f():\n    time.sleep(1)\n    assert True\n",
    "def f():\n    while True:\n        time.sleep(1)\n",
    "_SETTLE = 0.5\ndef f():\n    time.sleep(_SETTLE)\n    assert True\n",
    "GLOBAL_X = {}\ndef f():\n    global GLOBAL_X\n    GLOBAL_X['a'] = 1\n",
    "GLOBAL_Y = []\ndef f():\n    GLOBAL_Y.append(1)\n",
    "def _reset_process_state():\n    pass\n",
    "x = 1\ny = 2\n",
    "# allow-sleep: reason\n",
    "# allow-unreset-state: reason\n",
    "# allow-duplicate-class: reason\n",
    "import os\nimport sys\n",
    "",
]


@st.composite
def _python_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_PY_FRAGMENTS), max_size=4))
    return "\n".join(parts)


@given(_python_text())
def test_duplicate_class_names_top_level_classes_is_total_on_valid_python(
    source: str,
) -> None:
    try:
        compile(source, "<test>", "exec")
    except SyntaxError:
        assume(False)
    result = duplicate_class_names.top_level_classes(source)
    again = duplicate_class_names.top_level_classes(source)
    assert result == again
    n_lines = max(len(source.splitlines()), 1)
    for name, lineno in result.lines.items():
        assert isinstance(name, str)
        assert 1 <= lineno <= n_lines


_module_classes = st.builds(
    lambda defined, exempt: duplicate_class_names.ModuleClasses(
        defined=tuple(defined), exempt=frozenset(exempt) & set(defined), lines={}
    ),
    defined=st.lists(
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=4), max_size=4
    ),
    exempt=st.lists(
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=4), max_size=2
    ),
)


@given(
    st.dictionaries(
        st.text(alphabet=string.ascii_letters, min_size=1, max_size=4),
        _module_classes,
        max_size=4,
    )
)
def test_find_collisions_is_total(classes_by_file: dict) -> None:
    result = duplicate_class_names.find_collisions(classes_by_file)
    assert isinstance(result, dict)
    assert set(result) == set(classes_by_file)
    for rel, names in result.items():
        assert names == sorted(names)
        assert all(n in classes_by_file[rel].defined for n in names)


@given(_python_text())
def test_sleep_as_sync_violations_is_total_on_valid_python(source: str) -> None:
    try:
        compile(source, "<test>", "exec")
    except SyntaxError:
        assume(False)
    result = sleep_as_sync.violations(source)
    assert sleep_as_sync.violations(source) == result
    _assert_valid_linenos(source, result)


@given(_python_text(), st.text(alphabet=string.ascii_letters + "_", max_size=10))
def test_unreset_module_state_violations_is_total_on_valid_python(
    source: str, reset_name: str
) -> None:
    assume(reset_name)  # an empty reset_name would match no def, which is fine
    try:
        compile(source, "<test>", "exec")
    except SyntaxError:
        assume(False)
    result = unreset_module_state.violations(source, reset_name)
    assert unreset_module_state.violations(source, reset_name) == result
    _assert_valid_linenos(source, result)


# ── entrypoints that read a real tree: `find_dead` (its reference scan runs
# `git ls-files`) and `findings` (`build_helper_defs` walks a package
# directory). Built as a small tmp_path git repo so the property never
# depends on the real ci-truth-serum tree. ─────────────────────────────────


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(shell_text=_text)
def test_find_dead_is_total(tmp_path, shell_text: str) -> None:
    init_test_repo(tmp_path)
    target = tmp_path / "lib.sh"
    target.write_text(shell_text, encoding="utf-8")
    commit_all(tmp_path, "fixture")
    result = dead_shell_functions.find_dead([str(target)], tmp_path)
    assert isinstance(result, list)
    n_lines = max(len(shell_text.splitlines()), 1)
    for dead in result:
        assert dead.rel == str(target)
        assert 1 <= dead.lineno <= n_lines
        assert isinstance(dead.name, str) and dead.name


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(helper_text=_python_text(), caller_text=_python_text())
def test_findings_is_total(tmp_path, helper_text: str, caller_text: str) -> None:
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "_h.py").write_text(helper_text, encoding="utf-8")
    caller = tmp_path / "tests" / "test_x.py"
    caller.write_text(caller_text, encoding="utf-8")
    result = test_helper_kwargs.findings([str(caller)], tmp_path, ("tests",))
    assert isinstance(result, list)
    n_lines = max(len(caller_text.splitlines()), 1)
    for finding in result:
        assert finding.path == str(caller)
        assert 1 <= finding.line <= n_lines
        assert isinstance(finding.callee, str) and finding.callee
        assert isinstance(finding.problem, str) and finding.problem


# ── _cts_py_imports.interpreter_scripts: a pure function over a word list ──────

_WORDS = st.lists(
    st.one_of(
        st.none(),
        st.sampled_from(
            [
                "python3",
                "python",
                "python3.12",
                "/usr/bin/python3",
                ".venv/bin/python",
                "-I",
                "-m",
                "pytest",
                "x.py",
                "tests/x.py",
                "-c",
                "not.py",
                "notpy",
            ]
        ),
        st.text(max_size=15),
    ),
    max_size=15,
)


@given(_WORDS)
def test_interpreter_scripts_is_total(words: list) -> None:
    result = py_imports.interpreter_scripts(words)
    assert py_imports.interpreter_scripts(words) == result  # deterministic
    assert isinstance(result, list)
    for candidate in result:
        assert isinstance(candidate, str) and candidate.endswith(".py")
        # Every returned candidate actually occurred in the input words.
        assert candidate in words

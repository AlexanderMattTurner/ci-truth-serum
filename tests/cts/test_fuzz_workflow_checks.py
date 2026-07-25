"""Property/fuzz tests for the workflow lints whose public entrypoint is
``check_file(path)`` -- they read a file, ``yaml.safe_load`` it, and walk the
document. The crash-resistance contract is identical to the line detectors: a
malformed, adversarial, or simply non-workflow YAML file must yield findings or
nothing, never an unhandled exception.

These checks anchor discovery at ``REPO_ROOT`` / ``WORKFLOWS_DIR`` module
constants (set to ``Path.cwd()`` at import). We monkeypatch them per example so a
generated file lives under the module's idea of the repo root, then call
``check_file`` directly -- the same surface ``main()`` drives.

The last section drops below ``check_file`` to drive ``check_untrusted_exec``'s
own shell parser and document walker directly: a finding there needs three
conjuncts to line up at once, so a file-level strategy alone leaves those layers
barely touched.
"""

import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

always_reporter = load_hook("check_always_reporter.py", "fuzz_always_reporter")
required_reporter = load_hook("check_required_reporter.py", "fuzz_required_reporter")
concurrency = load_hook("check_concurrency.py", "fuzz_concurrency")
static_concurrency = load_hook("check_static_concurrency.py", "fuzz_static_concurrency")
cancellable_required_check = load_hook(
    "check_cancellable_required_check.py", "fuzz_cancellable_required_check"
)
frozen_head_sha = load_hook("check_frozen_head_sha.py", "fuzz_frozen_head_sha")
pending_cancel = load_hook(
    "check_pending_cancel_concurrency.py", "fuzz_pending_cancel_concurrency"
)
requires_concurrency = load_hook(
    "check_requires_concurrency.py", "fuzz_requires_concurrency"
)
pr_paths = load_hook("check_pr_paths.py", "fuzz_pr_paths")
claude_model = load_hook("check_claude_model.py", "fuzz_claude_model")
externalized_markers = load_hook(
    "check_externalized_markers.py", "fuzz_externalized_markers"
)
path_gate_deps = load_hook("check_path_gate_deps.py", "fuzz_path_gate_deps")
job_timeout = load_hook("check_job_timeout.py", "fuzz_job_timeout")
trusted_base = load_hook("check_trusted_base.py", "fuzz_trusted_base")
untrusted_exec = load_hook("check_untrusted_exec.py", "fuzz_untrusted_exec")
unscoped_tool_grant = load_hook(
    "check_unscoped_tool_grant.py", "fuzz_unscoped_tool_grant"
)

# Each returns a finding shape; the contract under fuzz is only "no crash, and a
# well-typed result". `expects_list` distinguishes the list-returning checks from
# the single-optional-tuple ones.
WORKFLOW_CHECKS = [
    ("check_always_reporter", always_reporter.check_file, False),
    ("check_required_reporter", required_reporter.check_file, True),
    ("check_concurrency", concurrency.check_file, True),
    ("check_static_concurrency", static_concurrency.check_file, False),
    ("check_cancellable_required_check", cancellable_required_check.check_file, False),
    ("check_frozen_head_sha", frozen_head_sha.check_file, True),
    ("check_pending_cancel_concurrency", pending_cancel.check_file, True),
    ("check_requires_concurrency", requires_concurrency.check_file, False),
    ("check_pr_paths", pr_paths.check_file, False),
    ("check_claude_model", claude_model.check_file, True),
    ("check_externalized_markers", externalized_markers.check_file, True),
    ("check_path_gate_deps", path_gate_deps.check_file, True),
    ("check_job_timeout", job_timeout.check_file, True),
    ("check_trusted_base", trusted_base.check_file, True),
    ("check_untrusted_exec", untrusted_exec.check_file, True),
    ("check_unscoped_tool_grant", unscoped_tool_grant.check_file, True),
]


_WORKFLOW_FRAGMENTS = [
    "name: x\n",
    "on:\n  pull_request:\n    paths: ['src/**']\n",
    "on:\n  pull_request: # not-required-check\n",
    "on:\n  pull_request_target:\n    paths-ignore: ['docs/**']\n",
    "on: [push, pull_request]\n",
    "concurrency:\n  group: x\n",
    "concurrency:\n  group: ci-${{ github.ref }}\n  cancel-in-progress: true\n",
    "concurrency:\n  group: static\n  # static-concurrency-ok\n",
    "concurrency:\n  group: static\n  cancel-in-progress: true\n",
    "concurrency:\n  group: static\n  # cancellable-required-check-ok\n",
    # A frozen head-SHA in run: and in a with: value (check_frozen_head_sha).
    "jobs:\n  b:\n    steps:\n      - run: git diff ${{ github.event.pull_request.head.sha }}...HEAD\n",
    (
        "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
    ),
    "on:\n  pull_request:\n    types: [opened, labeled]\n",
    (
        "jobs:\n  scan:\n    concurrency:\n"
        "      group: ${{ github.head_ref || github.ref }}\n"
        "      cancel-in-progress: false\n"
    ),
    "permissions:\n  contents: read\n",
    "jobs:\n  decide:\n    uses: ./.github/workflows/decide-reusable.yaml\n",
    "jobs:\n  build:\n    if: needs.decide.outputs.run == 'true'\n    steps: []\n",
    "jobs:\n  report:\n    if: always()\n    needs: [decide]\n",
    "jobs:\n  report: # required-check: true\n    if: always()\n",
    "jobs:\n  report: # required-check: false\n    if: always()\n",
    (
        "jobs:\n  claude:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
        "        with:\n          claude_args: --model x\n"
    ),
    "jobs:\n  claude:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n",
    # Externalized-marker paths: a script invocation and a local composite ref.
    # The referenced files don't exist under the fuzz root, so resolution reads
    # empty text and the job stays clean — exercising the traversal, not a finding.
    "jobs:\n  fix:\n    steps:\n      - run: bash .github/scripts/autofix.sh\n",
    "jobs:\n  fix:\n    steps:\n      - uses: ./.github/actions/fixup\n",
    # Path-gate shapes: a decide job with filters, and a gated consumer.
    (
        "jobs:\n  decide:\n    uses: ./.github/workflows/decide-reusable.yaml\n"
        "    with:\n      filters: |\n        run:\n          - 'src/**'\n"
    ),
    (
        "jobs:\n  work:\n    needs: decide\n"
        "    if: needs.decide.outputs.run == 'true'\n"
        "    steps:\n      - run: bash .github/scripts/x.sh\n"
    ),
    # job-timeout shapes: a job missing timeout-minutes, one that sets it, and a
    # reusable-call job (exempt).
    "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps: []\n",
    "jobs:\n  build:\n    timeout-minutes: 10\n    steps: []\n",
    "jobs:\n  build:  # allow-no-timeout: watcher\n    steps: []\n",
    # trusted-base shapes: a privileged PR-head checkout and its opt-out.
    "permissions:\n  contents: write\n",
    (
        "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
    ),
    "on:\n  pull_request_target:\n",
    "# trusted-base-ok: base-trusted only\n",
    # untrusted-exec shapes: each of the three execution forms sitting next to a
    # PR-head checkout with a live secret, the conditionally-trusted workflow_run
    # head, the job-scoped opt-out, and step lists that are not lists of mappings.
    (
        "jobs:\n  exec:\n    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.pull_request.head.sha }}\n"
        "      - uses: ./.github/actions/build\n"
        "        env:\n          T: ${{ secrets.NPM_TOKEN }}\n"
    ),
    (
        "jobs:\n  exec:\n    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.head_ref }}\n"
        "      - run: pnpm build && npx tsc && make release\n"
        "        env:\n          T: ${{ secrets.A }}\n"
    ),
    (
        "jobs:\n  exec:\n    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ matrix.pr.head_ref }}\n"
        "      - run: bash ./scripts/x.sh\n"
        "    secrets: inherit\n"
    ),
    (
        "jobs:\n  exec:\n    if: github.event.workflow_run.event == 'push'\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.event.workflow_run.head_sha }}\n"
        "      - run: node scripts/x.mjs\n"
        "        env:\n          T: ${{ secrets.B }}\n"
    ),
    (
        "jobs:\n  exec:  # untrusted-exec-ok: credential moved to a land job\n"
        "    steps:\n      - uses: actions/checkout@v4\n"
        "        with:\n          ref: ${{ github.head_ref }}\n"
        "      - run: $RUNNER_TEMP/stage.sh\n"
        "        env:\n          T: ${{ secrets.C }}\n"
    ),
    (
        "jobs:\n  m:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
        '        with:\n          claude_args: "--allowedTools Bash Read Write"\n'
    ),
    (
        "jobs:\n  m:\n    steps:\n      - run: claude -p x\n"
        '        env:\n          AUTOFIX_ALLOWED_TOOLS: "Read,Edit,Write(//tmp/o.json)"\n'
    ),
    (
        "jobs:\n  m:  # allow-unscoped-write-grant: bare Bash makes scoping moot\n"
        "    steps:\n      - run: claude --allowed-tools 'Read(./**),Edit(//tmp/o)'\n"
    ),
    "jobs:\n  m:\n    steps:\n      - run: claude --disallowedTools Read\n",
    "jobs:\n  m:\n    steps:\n      - with:\n          allowed_tools: Glob(./**)\n",
    "jobs:\n  exec:\n    steps: 'not-a-list'\n",
    "jobs:\n  exec:\n    steps:\n      - a string step\n      - null\n",
    "jobs:\n  exec:\n    steps:\n      - run: ${{ github.event.pull_request.title }}\n",
    "jobs:\n  exec:\n    steps:\n      - uses: 42\n        run: 17\n",
    "jobs: null\n",
    "[]\n",
    "just a scalar\n",
    "key: : :\n",  # malformed
    "\t- bad-indent\n",
]


@st.composite
def workflow_text(draw: st.DrawFn) -> str:
    parts = draw(st.lists(st.sampled_from(_WORKFLOW_FRAGMENTS), max_size=5))
    if draw(st.booleans()):
        parts.append(draw(st.text(max_size=80)))
    return "".join(parts)


def _assert_lineno_in_range(line: object, n_lines: int) -> None:
    # A reported line number must point at a real line of the file it was read
    # from (1-based, within bounds) -- mirrors test_fuzz_parsers._assert_valid_linenos.
    if line is None:
        return
    assert isinstance(line, int)
    assert 1 <= line <= n_lines, (line, n_lines)


def _result_well_typed(result: object, expects_list: bool, n_lines: int) -> None:
    if expects_list:
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, tuple) and len(item) == 2
            line, msg = item
            assert isinstance(line, int) and isinstance(msg, str)
            _assert_lineno_in_range(line, n_lines)
        return
    assert result is None or (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], int)
        and isinstance(result[1], str)
    )
    if isinstance(result, tuple):
        _assert_lineno_in_range(result[0], n_lines)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(text=workflow_text())
def test_workflow_check_files_never_crash(
    text: str, tmp_path_factory, monkeypatch
) -> None:
    # safe_load may itself raise on malformed YAML; the checks that DON'T guard
    # that (they call path.read_text()+safe_load inline) legitimately propagate a
    # YAMLError -- mirror main()'s behavior by only feeding parseable docs to
    # those, while still hammering the traversal. A YAMLError here is the parser's,
    # not the lint's, so it is not a lint crash.
    try:
        yaml.safe_load(text)
    except yaml.YAMLError:
        assume(False)

    root = tmp_path_factory.mktemp("repo")
    wf_dir = root / ".github" / "workflows"
    wf_dir.mkdir(parents=True)
    path = wf_dir / "wf.yaml"
    path.write_text(text, encoding="utf-8")
    n_lines = len(text.splitlines())

    for _name, check, expects_list in WORKFLOW_CHECKS:
        mod = check.__globals__
        monkeypatch.setitem(mod, "REPO_ROOT", root)
        monkeypatch.setitem(mod, "WORKFLOWS_DIR", wf_dir)
        if "ACTIONS_DIR" in mod:
            monkeypatch.setitem(mod, "ACTIONS_DIR", root / ".github" / "actions")
        result = check(path)
        _result_well_typed(result, expects_list, n_lines)


# --- check_untrusted_exec's own analyzers ------------------------------------
#
# check_file above proves the file-level surface never crashes and points at a
# real line. These pin the two layers underneath it directly: the shell parser
# that decides whether a `run:` body executes workspace-resolved code, and the
# document walker that decides which JOB that makes a finding. Both are reached
# by check_file only when a fragment happens to line up all three conjuncts
# (untrusted checkout + live secret + execution form), so driving them straight
# is what actually hammers them.

_EXEC_TOKENS = [
    "pnpm build",
    "pnpm run build --filter x",
    "npm run test -- --ci",
    "npm install",
    "yarn lint",
    "bun run x",
    "npx tsc",
    "pnpx cowsay",
    "make -j2 release",
    "make",
    "bash ./scripts/x.sh",
    "sh build.sh",
    "node scripts/x.mjs",
    "python3 tools/gen.py",
    "./bin/tool",
    "FOO=1 bash build.sh >out 2>&1",
    "$RUNNER_TEMP/stage.sh",
    "${{ steps.s.outputs.dir }}/go.sh",
    "/usr/bin/env true",
    "~/x.sh",
    "'quoted arg'",
    'bash "./a b.sh"',
    "-",
    "--",
    "cmd | tee log",
    "a && b || c",
    "$(x)",
    "`y`",
    "<<EOF",
    "EOF",
    ";;",
    "|",
    "'",
    '"',
    "\\",
]
# ${{ }} interpolation, an RLO/ZWSP pair and an astral char, so no invisible byte
# hides behind a "looks fine" example.
_WEIRD_RUN = "\u202e\u200b\U0001f600${{ github.event.pull_request.title }}"
_RUN_LINE = st.one_of(
    st.sampled_from(_EXEC_TOKENS),
    st.just(_WEIRD_RUN),
    st.text(max_size=40),
)


@st.composite
def run_bodies(draw: st.DrawFn) -> str:
    """A `run:` script assembled from real execution forms plus garbage."""
    sep = draw(st.sampled_from(["\n", " && ", "; ", " | ", " || ", "\r\n"]))
    return sep.join(draw(st.lists(_RUN_LINE, max_size=12)))


@given(script=run_bodies())
def test_run_executions_never_crashes_and_is_a_clean_label_list(script: str) -> None:
    labels = untrusted_exec.run_executions(script)
    assert isinstance(labels, list)
    # Every label is a non-empty human string quoting the command it found; the
    # message builder joins these, so an empty or duplicate entry would produce a
    # finding that names nothing (or names the same thing twice).
    assert all(isinstance(label, str) and label.strip() for label in labels)
    assert len(labels) == len(set(labels))
    # Deterministic: a pure text -> labels function that drifts would make the
    # lint's verdict depend on parse order.
    assert untrusted_exec.run_executions(script) == labels


@given(
    step=st.dictionaries(
        st.sampled_from(["uses", "run", "with", "env", "shell", "__line__", ""]),
        st.none()
        | st.booleans()
        | st.integers()
        # Real values alongside the garbage, so the LABEL-producing branches are
        # reached and the "every label is a non-empty string" claim is not vacuous.
        | st.sampled_from(
            ["./.github/actions/x", ".", "actions/checkout@v4", "pnpm build", " "]
        )
        | st.text(max_size=40)
        | st.lists(st.text(max_size=8), max_size=3)
        | st.dictionaries(st.text(max_size=6), st.text(max_size=6), max_size=3),
        max_size=6,
    )
)
def test_step_executions_tolerates_arbitrary_step_shapes(step: dict) -> None:
    # YAML hands the lint whatever the author wrote: `uses:` can be an int, `run:`
    # a list, `with:` a scalar. A wrong-typed step is "nothing to report", never a
    # crash.
    labels = untrusted_exec.step_executions(step)
    assert isinstance(labels, list)
    assert all(isinstance(label, str) and label.strip() for label in labels)


_JOB_VALUES = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.sampled_from(
        [
            "inherit",
            "actions/checkout@v4",
            "./.github/actions/x",
            "${{ github.event.pull_request.head.sha }}",
            "${{ secrets.TOKEN }}",
            "workflow_run.event == 'push'",
            "pnpm build",
        ]
    )
    | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=3)
        | st.dictionaries(
            st.sampled_from(
                ["uses", "run", "with", "ref", "env", "secrets", "if", "permissions"]
            )
            | st.text(max_size=6),
            children,
            max_size=4,
        )
    ),
    max_leaves=20,
)


_CHECKOUT_STEPS = [
    {
        "uses": "actions/checkout@v4",
        "with": {"ref": "${{ github.event.pull_request.head.sha }}"},
    },
    {"uses": "actions/checkout@v4", "with": {"ref": "${{ github.head_ref }}"}},
    {
        "uses": "actions/checkout@v4",
        "with": {"ref": "${{ github.event.workflow_run.head_sha }}"},
    },
    {"uses": "actions/checkout@v4", "with": {"ref": "${{ matrix.pr.head_ref }}"}},
    {"uses": "actions/checkout@v4", "with": {"ref": "main"}},
    {"uses": "actions/checkout@v4", "with": "not-a-mapping"},
    {"uses": "actions/checkout@v4"},
    {"uses": 42},
]
_EXEC_STEPS = [
    {"uses": "./.github/actions/build"},
    {"uses": "."},
    {"run": "pnpm build", "env": {"T": "${{ secrets.NPM_TOKEN }}"}, "__line__": 9},
    {"run": "bash ./scripts/x.sh"},
    {"run": "$RUNNER_TEMP/stage.sh"},
    {"run": "npm install"},
    {"run": ["not", "a", "string"]},
    {"run": None},
]
_JOB_EXTRAS = [
    {},
    {"env": {"T": "${{ secrets.DEPLOY_KEY }}"}},
    {"secrets": "inherit"},
    {"permissions": {"contents": "write"}},
    {"permissions": "read-all"},
    {"env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}},
    {"if": "github.event.workflow_run.event == 'push'"},
]


@st.composite
def untrusted_exec_docs(draw: st.DrawFn) -> dict:
    """A workflow document assembled from the shapes that make the three
    conjuncts (untrusted checkout, live secret, execution form) line up.

    Without this the arbitrary-object strategy below never assembles a violating
    job, so every assertion about a REPORTED tuple would pass vacuously."""
    steps = draw(st.lists(st.sampled_from(_CHECKOUT_STEPS), max_size=2)) + draw(
        st.lists(st.sampled_from(_EXEC_STEPS), max_size=3)
    )
    cfg = {**draw(st.sampled_from(_JOB_EXTRAS)), "steps": steps}
    return {
        "permissions": draw(st.sampled_from([None, {"contents": "write"}])),
        "env": draw(st.sampled_from([None, {"T": "${{ secrets.WORKFLOW_LEVEL }}"}])),
        "jobs": {draw(st.sampled_from(["build", "exec", "a"])): cfg},
    }


@given(
    doc=st.one_of(
        _JOB_VALUES,
        st.fixed_dictionaries(
            {"jobs": st.dictionaries(st.text(max_size=6), _JOB_VALUES, max_size=3)}
        ),
        untrusted_exec_docs(),
    ),
    already=st.frozensets(
        st.sampled_from(["build", "exec", "a"]) | st.text(max_size=6), max_size=3
    ),
)
def test_analyze_tolerates_arbitrary_documents(doc: object, already: frozenset) -> None:
    # analyze() is fed whatever the YAML loader returned -- a scalar, a list, a
    # `jobs:` whose values are strings. It must degrade to "nothing to report"
    # and, when it does report, yield the exact 4-tuple check_file destructures.
    out = untrusted_exec.analyze(doc, already)
    assert isinstance(out, list)
    for item in out:
        assert isinstance(item, tuple) and len(item) == 4
        name, line, forms, secrets = item
        assert isinstance(name, str)
        assert line is None or isinstance(line, int)
        assert isinstance(forms, list) and forms
        assert all(isinstance(form, str) and form.strip() for form in forms)
        assert isinstance(secrets, list) and secrets
        assert secrets == sorted(secrets)
        # A job check_trusted_base already reports is handed off, not re-reported.
        assert name not in already

"""Property/fuzz tests for the workflow lints whose public entrypoint is
``check_file(path)`` -- they read a file, ``yaml.safe_load`` it, and walk the
document. The crash-resistance contract is identical to the line detectors: a
malformed, adversarial, or simply non-workflow YAML file must yield findings or
nothing, never an unhandled exception.

These checks anchor discovery at ``REPO_ROOT`` / ``WORKFLOWS_DIR`` module
constants (set to ``Path.cwd()`` at import). We monkeypatch them per example so a
generated file lives under the module's idea of the repo root, then call
``check_file`` directly -- the same surface ``main()`` drives.
"""

import yaml
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from tests._helpers import load_hook

always_reporter = load_hook("check_always_reporter.py", "fuzz_always_reporter")
required_reporter = load_hook("check_required_reporter.py", "fuzz_required_reporter")
concurrency = load_hook("check_concurrency.py", "fuzz_concurrency")
static_concurrency = load_hook("check_static_concurrency.py", "fuzz_static_concurrency")
requires_concurrency = load_hook(
    "check_requires_concurrency.py", "fuzz_requires_concurrency"
)
pr_paths = load_hook("check_pr_paths.py", "fuzz_pr_paths")
claude_model = load_hook("check_claude_model.py", "fuzz_claude_model")
externalized_markers = load_hook(
    "check_externalized_markers.py", "fuzz_externalized_markers"
)
path_gate_deps = load_hook("check_path_gate_deps.py", "fuzz_path_gate_deps")

# Each returns a finding shape; the contract under fuzz is only "no crash, and a
# well-typed result". `expects_list` distinguishes the list-returning checks from
# the single-optional-tuple ones.
WORKFLOW_CHECKS = [
    ("check_always_reporter", always_reporter.check_file, False),
    ("check_required_reporter", required_reporter.check_file, True),
    ("check_concurrency", concurrency.check_file, True),
    ("check_static_concurrency", static_concurrency.check_file, False),
    ("check_requires_concurrency", requires_concurrency.check_file, False),
    ("check_pr_paths", pr_paths.check_file, False),
    ("check_claude_model", claude_model.check_file, True),
    ("check_externalized_markers", externalized_markers.check_file, True),
    ("check_path_gate_deps", path_gate_deps.check_file, True),
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


def _result_well_typed(result: object, expects_list: bool) -> None:
    if expects_list:
        assert isinstance(result, list)
        for item in result:
            assert isinstance(item, tuple) and len(item) == 2
            line, msg = item
            assert isinstance(line, int) and isinstance(msg, str)
        return
    assert result is None or (
        isinstance(result, tuple)
        and len(result) == 2
        and isinstance(result[0], int)
        and isinstance(result[1], str)
    )


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

    for _name, check, expects_list in WORKFLOW_CHECKS:
        mod = check.__globals__
        monkeypatch.setitem(mod, "REPO_ROOT", root)
        monkeypatch.setitem(mod, "WORKFLOWS_DIR", wf_dir)
        if "ACTIONS_DIR" in mod:
            monkeypatch.setitem(mod, "ACTIONS_DIR", root / ".github" / "actions")
        result = check(path)
        _result_well_typed(result, expects_list)

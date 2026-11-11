"""Tests for ci_truth_serum/_cts_gating.py — the reader that finds the jobs whose
check can hold a merge back, from `# required-check: true` markers and from a
Mergify config's queue rules.

The config fixture is the queue rule agent-glovebox moved to: a pull request
needs four checks plus consent, which every `or` arm proves with one check, and
Mergify's draft needs only `CI`.
"""

from pathlib import Path

import pytest
import yaml

from tests._helpers import load_hook

gating = load_hook("_cts_gating.py", "_cts_gating")
lc = load_hook("_cts_linecheck.py", "_cts_linecheck")

GLOVEBOX_RULES = """\
queue_rules:
  - name: default
    queue_conditions:
      - base=main
      - -draft
      - check-success=CI
      - check-success=A fix PR ships a test
      - or:
          - and:
              - check-success=Consent recorded
              - or:
                  - label=approved
                  - label=red-fix
          - and:
              - author=renovate[bot]
              - check-success=Consent recorded
    merge_conditions:
      - check-success=CI
"""


def _set(*names: str) -> frozenset[str]:
    return frozenset(names)


# ── queue_checks / merge_checks ───────────────────────────────────────────


def test_every_or_arm_naming_a_check_makes_it_required():
    checks = gating.queue_checks(GLOVEBOX_RULES)
    assert checks.required == _set("CI", "A fix PR ships a test", "Consent recorded")
    assert checks.conditional == frozenset()


def test_merge_conditions_are_the_queue_build_checks():
    assert gating.merge_checks(GLOVEBOX_RULES).gating() == _set("CI")


def test_a_check_only_one_arm_names_is_conditional():
    text = GLOVEBOX_RULES.replace(
        "              - author=renovate[bot]\n              - check-success=Consent recorded\n",
        "              - author=renovate[bot]\n",
    )
    checks = gating.queue_checks(text)
    assert checks.conditional == _set("Consent recorded")
    assert "Consent recorded" in checks.gating()


def test_several_queue_rules_read_as_one_or():
    text = """\
queue_rules:
  - name: fast
    queue_conditions: [check-success=lint, check-success=unit]
    merge_conditions: []
  - name: slow
    queue_conditions: [check-success=lint, check-success=e2e]
    merge_conditions: []
"""
    checks = gating.queue_checks(text)
    assert checks.required == _set("lint")
    assert checks.conditional == _set("unit", "e2e")


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("main", _set("CI", "A fix PR ships a test", "Consent recorded")),
        ("release", frozenset()),
        (None, _set("CI", "A fix PR ships a test", "Consent recorded")),
    ],
)
def test_a_base_condition_holds_only_on_its_base(base, expected):
    """None reads every base, so a gate on any branch counts."""
    assert gating.queue_checks(GLOVEBOX_RULES, base=base).gating() == expected


def test_a_negated_base_condition_excludes_its_base():
    text = "queue_rules:\n  - queue_conditions: [-base=main, check-success=x]\n"
    assert gating.queue_checks(text, base="main").gating() == frozenset()
    assert gating.queue_checks(text, base="dev").gating() == _set("x")


@pytest.mark.parametrize(
    ("condition", "name"),
    [
        ("check-success=CI", "CI"),
        ("check-success = CI", "CI"),
        ("status-success=CI", "CI"),
        ("check-success=@github-actions/CI", "CI"),
        ("check-success=@other-app/CI", "@other-app/CI"),
    ],
)
def test_a_success_condition_names_the_check_a_job_posts(condition, name):
    text = f"queue_rules:\n  - queue_conditions: ['{condition}']\n"
    assert gating.queue_checks(text).gating() == _set(name)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "pull_request_rules: []\n",
        "queue_rules: []\n",
    ],
)
def test_a_config_with_no_queue_rule_gates_nothing(text):
    assert gating.queue_checks(text).gating() == frozenset()


def test_a_rule_without_a_key_takes_the_default_rule():
    text = """\
defaults:
  queue_rule:
    queue_conditions: [check-success=lint]
queue_rules:
  - name: fast
  - name: slow
    queue_conditions: [check-success=lint, check-success=e2e]
    merge_conditions: true
"""
    assert gating.queue_checks(text).required == _set("lint")
    assert gating.queue_checks(text).conditional == _set("e2e")
    assert gating.merge_checks(text).gating() == frozenset()


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("queue_rules:\n  - queue_conditions: [-check-success=CI]\n", "-check-success"),
        (
            "queue_rules:\n  - queue_conditions: ['#check-success>=1']\n",
            "#check-success",
        ),
        ("queue_rules:\n  - queue_conditions: [check-failure=CI]\n", "check-failure"),
        ("queue_rules:\n  - queue_conditions: ['check-success~=^CI']\n", "~="),
        ("queue_rules:\n  - queue_conditions: [{not: {or: [a]}}]\n", "not"),
        ("queue_rules:\n  - queue_conditions: [{or: a}]\n", "`or:` holds no list"),
        ("queue_rules:\n  - queue_conditions: check-success=CI\n", "not a list"),
        ("defaults:\n  queue_rule: [a]\n", "`defaults.queue_rule` is not a mapping"),
        ("queue_rules: {name: q}\n", "not a list of rules"),
        ("extends: org/config\n", "extends"),
        ("- a\n", "not a mapping"),
        ("queue_rules: [\n", "not valid YAML"),
    ],
)
def test_a_config_the_reader_cannot_read_is_refused(text, fragment):
    """A guess could drop a gate, and the check would then pass on nothing."""
    with pytest.raises(gating.MergifyConfigError) as err:
        gating.queue_checks(text)
    assert fragment in str(err.value)


# ── config_path ───────────────────────────────────────────────────────────


def test_config_path_takes_the_first_place_mergify_reads(tmp_path):
    assert gating.config_path(tmp_path) is None
    for name in reversed(gating.CONFIG_PATHS):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
        assert gating.config_path(tmp_path) == tmp_path / name


# ── GatingSource ──────────────────────────────────────────────────────────

CI_WORKFLOW = """\
name: ci
on: pull_request
jobs:
  test:
    name: test (${{ matrix.os }})
    strategy:
      matrix:
        os: [linux, mac]
    runs-on: ubuntu-latest
  report:
    name: CI
    needs: [test]
    if: always()
    runs-on: ubuntu-latest
  lint: # required-check: true
    runs-on: ubuntu-latest
"""


def _repo(tmp_path: Path, config: str | None, **workflows: str) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    for name, body in workflows.items():
        (wf / f"{name}.yaml").write_text(body, encoding="utf-8")
    if config is not None:
        (tmp_path / ".mergify.yml").write_text(config, encoding="utf-8")
    return wf


def _gates(source, path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    return source.jobs(path, lc._job_blocks(text), doc["jobs"])


def test_both_sources_gate_together(tmp_path):
    wf = _repo(tmp_path, GLOVEBOX_RULES, ci=CI_WORKFLOW)
    source = gating.GatingSource.load(tmp_path)
    gates = _gates(source, wf / "ci.yaml")
    assert sorted(gates) == ["lint", "report"]
    assert gates["lint"] == gating.Gate(marker=True)
    assert gates["report"] == gating.Gate(
        pull_request=_set("CI"), queue_build=_set("CI")
    )
    assert gates["report"].describe(source.config) == ".mergify.yml gates on 'CI'"
    assert (
        gates["lint"].describe(source.config) == "the '# required-check: true' marker"
    )


@pytest.mark.parametrize(
    ("flag", "expected"),
    [("markers", ["lint"]), ("mergify", ["report"]), ("both", ["lint", "report"])],
)
def test_the_flag_picks_the_places_to_read(tmp_path, flag, expected):
    wf = _repo(tmp_path, GLOVEBOX_RULES, ci=CI_WORKFLOW)
    source = gating.GatingSource.load(tmp_path, flag)
    assert sorted(_gates(source, wf / "ci.yaml")) == expected


def test_a_matrix_name_matches_each_expanded_check(tmp_path):
    rules = "queue_rules:\n  - queue_conditions: ['check-success=test (mac)']\n    merge_conditions: []\n"
    wf = _repo(tmp_path, rules, ci=CI_WORKFLOW)
    source = gating.GatingSource.load(tmp_path, "mergify")
    assert sorted(_gates(source, wf / "ci.yaml")) == ["test"]


def test_both_without_a_config_reads_the_markers_alone(tmp_path):
    wf = _repo(tmp_path, None, ci=CI_WORKFLOW)
    source = gating.GatingSource.load(tmp_path)
    assert source.config is None
    assert sorted(_gates(source, wf / "ci.yaml")) == ["lint"]


@pytest.mark.parametrize(
    ("config", "flag", "fragment"),
    [
        (None, "mergify", "no Mergify config exists"),
        ("pull_request_rules: []\n", "mergify", "no queue rule gates on a check"),
        ("extends: org/x\n", "both", ".mergify.yml: `extends`"),
    ],
)
def test_load_refuses_a_source_that_gates_nothing(tmp_path, config, flag, fragment):
    _repo(tmp_path, config, ci=CI_WORKFLOW)
    with pytest.raises(gating.MergifyConfigError) as err:
        gating.GatingSource.load(tmp_path, flag)
    assert fragment in str(err.value)


CALLER = """\
name: caller
on: pull_request
jobs:
  lint:
    name: Lint
    uses: ./.github/workflows/lint.yaml
  other:
    uses: ./.github/workflows/lint.yaml
"""

CALLEE = """\
name: lint
on: workflow_call
jobs:
  ruff:
    runs-on: ubuntu-latest
  mypy:
    runs-on: ubuntu-latest
"""


def test_a_called_workflow_gates_through_its_caller_name(tmp_path):
    """GitHub posts a called job as `<caller> / <callee>`, so a gate on that name
    makes the callee job gate, and the caller job that decides whether it runs."""
    rules = "queue_rules:\n  - queue_conditions: ['check-success=Lint / ruff']\n    merge_conditions: []\n"
    wf = _repo(tmp_path, rules, caller=CALLER, lint=CALLEE)
    source = gating.GatingSource.load(tmp_path, "mergify")
    assert _gates(source, wf / "caller.yaml") == {
        "lint": gating.Gate(pull_request=_set("Lint / ruff"))
    }
    assert _gates(source, wf / "lint.yaml") == {
        "ruff": gating.Gate(pull_request=_set("Lint / ruff"))
    }


def test_a_call_cycle_ends_the_walk(tmp_path):
    loop = "on: [pull_request, workflow_call]\njobs:\n  again:\n    uses: ./.github/workflows/loop.yaml\n"
    rules = "queue_rules:\n  - queue_conditions: [check-success=again]\n    merge_conditions: []\n"
    _repo(tmp_path, rules, loop=loop)
    assert gating.GatingSource.load(tmp_path, "mergify").called == {}

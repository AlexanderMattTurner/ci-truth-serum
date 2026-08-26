"""Behavior tests for the release-canary wiring: .github/scripts/release-canary.sh
and the tag-release job step that runs it.

`release-canary` shipped as a console script nothing in this repo invoked — no
workflow step, no hook, no suite run against the real tree — so when it started
dying with an unhandled `CalledProcessError` on this very repo, nobody saw it.
These tests pin the two properties that made it invisible:

  * the canary runs on the live release path (a step in tag-release.yaml's job),
    and its failure is neither swallowed nor gated off the failure path; and
  * that workflow's failures reach a human, judged by the repo's OWN routing
    SSOT (`ci_truth_serum/_failure_routing.py`) over the real workflow tree
    rather than by a string match on the notifier's list.

The wrapper itself is driven for real in a sandbox repo with a stubbed `uv`, so
the exit-status propagation and the fail-loud fetch guard are observed, not
asserted about the source text.
"""

import os
import shutil
import subprocess
from pathlib import Path

import tomllib
import yaml

from tests._helpers import REPO_ROOT, commit_all, git_env, init_test_repo, load_hook

SCRIPT_REL = ".github/scripts/release-canary.sh"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
TAG_RELEASE = WORKFLOWS / "tag-release.yaml"

routing_mod = load_hook("_failure_routing.py", "release_canary_wiring_routing")
linecheck = load_hook("_linecheck.py", "release_canary_wiring_linecheck")


def _steps_running(doc: dict, needle: str) -> list[tuple[str, dict]]:
    """Every `(job_id, step)` in DOC whose `run:` body invokes NEEDLE."""
    found = []
    for job_id, job in (doc.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and needle in str(step.get("run", "")):
                found.append((job_id, step))
    return found


def _tag_release_doc() -> dict:
    return yaml.safe_load(TAG_RELEASE.read_text(encoding="utf-8"))


def test_canary_runs_on_the_tag_release_path() -> None:
    """The canary is invoked from the job that cuts the release, after it."""
    doc = _tag_release_doc()
    canary = _steps_running(doc, SCRIPT_REL)
    tagger = _steps_running(doc, ".github/scripts/tag-release.sh")
    assert len(canary) == 1, f"expected exactly one canary step, got {canary}"
    assert len(tagger) == 1, f"expected exactly one tag step, got {tagger}"

    canary_job, canary_step = canary[0]
    tag_job, _ = tagger[0]
    assert canary_job == tag_job, (
        "the canary must share the tag job so its failure fails the workflow the "
        f"notifier watches (canary in {canary_job!r}, tagger in {tag_job!r})"
    )

    steps = doc["jobs"][canary_job]["steps"]
    assert steps.index(canary_step) > steps.index(tagger[0][1]), (
        "the canary compares the markers a cut release leaves behind, so it must "
        "run after the tagging step, not before it"
    )


def test_canary_step_failure_is_not_swallowed() -> None:
    """No `continue-on-error`, and no `if:` that takes the step (or its job) off
    the path a real failure travels — the two ways a wired guard reports green
    while doing nothing."""
    doc = _tag_release_doc()
    canary = _steps_running(doc, SCRIPT_REL)
    assert canary, f"no step in {TAG_RELEASE.name} runs {SCRIPT_REL}"
    job_id, step = canary[0]
    job = doc["jobs"][job_id]

    assert not step.get("continue-on-error"), "canary step must fail the job"
    assert not job.get("continue-on-error"), "canary job must fail the workflow"
    for gate in (step.get("if", ""), job.get("if", "")):
        assert routing_mod.gate_direction(gate) != routing_mod.BLOCKED, (
            f"gate {gate!r} holds only on success, so the canary would never "
            "run on the release it is meant to verify"
        )


def test_tag_release_failure_reaches_a_human() -> None:
    """The workflow now carrying the canary is routed to a human, computed with
    the repo's own routing SSOT over the real workflow tree.

    This is the property whose absence made the canary invisible: a guard on an
    unwatched surface is a guard nobody runs.
    """
    matcher = linecheck.notifier_matcher()
    docs = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
    ]
    docs = [doc for doc in docs if isinstance(doc, dict)]
    watched = routing_mod.watched_names(docs, matcher)

    doc = _tag_release_doc()
    route = routing_mod.routing(
        doc, TAG_RELEASE.read_text(encoding="utf-8"), matcher, watched
    )
    assert not route.opted_out, (
        "tag-release must not carry a cron-alert opt-out: the canary's whole "
        "point is that this failure is seen"
    )
    assert route.self_notify or route.watched, (
        "no notifier watches "
        f"{doc['name']!r} and it carries no failure-reachable notify step, so a "
        "canary failure here would land in an Actions tab nobody opens"
    )


def test_script_invokes_a_real_console_script_entry_point() -> None:
    """The name the wrapper runs is a declared entry point, so a rename can't
    leave the step invoking a command that no longer exists."""
    pyproject = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    scripts = pyproject["project"]["scripts"]
    assert "release-canary" in scripts, scripts
    assert scripts["release-canary"].startswith("ci_truth_serum.release_canary:")


# ── the wrapper, driven for real ────────────────────────────────────────


def _install_uv_stub(bindir: Path, log: Path, exit_code: int) -> None:
    """A `uv` that records its argv and exits with EXIT_CODE, standing in for
    the canary run so the wrapper's own behavior is what's observed."""
    bindir.mkdir(parents=True, exist_ok=True)
    uv = bindir / "uv"
    uv.write_text(
        f'#!/usr/bin/env bash\necho "$@" >>"{log}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)


def _make_repo(tmp_path: Path, *, uv_exit: int, with_origin: bool = True) -> tuple:
    """A sandbox repo carrying the real wrapper + retry.bash, a bare origin (or a
    dangling one), and a stubbed `uv`. Returns (repo, uv_log, bindir)."""
    repo = tmp_path / "repo"
    init_test_repo(repo)

    scripts = repo / ".github" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / SCRIPT_REL, scripts)
    (scripts / "release-canary.sh").chmod(0o755)

    libdir = repo / "bin" / "lib"
    libdir.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "bin" / "lib" / "retry.bash", libdir)

    (repo / "seed").write_text("x\n", encoding="utf-8")
    commit_all(repo, "seed")

    origin = tmp_path / "origin.git"
    if with_origin:
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(origin)],
        cwd=repo,
        env=git_env(),
        check=True,
    )

    bindir = tmp_path / "bin"
    uv_log = tmp_path / "uv.log"
    _install_uv_stub(bindir, uv_log, uv_exit)
    return repo, uv_log, bindir


def _run(repo: Path, bindir: Path, *args: str) -> subprocess.CompletedProcess:
    env = git_env()
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["bash", SCRIPT_REL, *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )


def test_wrapper_runs_the_packaged_canary_and_passes_on_agreement(
    tmp_path: Path,
) -> None:
    repo, uv_log, bindir = _make_repo(tmp_path, uv_exit=0)
    result = _run(repo, bindir)
    assert result.returncode == 0, result.stdout + result.stderr
    assert uv_log.read_text(encoding="utf-8").split() == [
        "run",
        "--frozen",
        "release-canary",
    ]


def test_wrapper_propagates_a_canary_failure(tmp_path: Path) -> None:
    """A disagreeing marker set exits non-zero all the way out of the wrapper —
    the property that turns a canary finding into a failed job, and so into an
    alert."""
    repo, uv_log, bindir = _make_repo(tmp_path, uv_exit=1)
    result = _run(repo, bindir)
    assert result.returncode == 1, result.stdout + result.stderr
    assert uv_log.exists(), "the canary was never reached"


def test_wrapper_fails_loud_when_tags_cannot_be_fetched(tmp_path: Path) -> None:
    """No tag set means the git marker would read as absent. That must be a red
    naming the fetch, never a canary run that blames the release — and never a
    skip."""
    repo, uv_log, bindir = _make_repo(tmp_path, uv_exit=0, with_origin=False)
    result = _run(repo, bindir)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "failed to fetch tags" in result.stderr, result.stderr
    assert not uv_log.exists(), "the canary ran on an incomplete tag set"


def test_wrapper_forwards_its_arguments_to_the_canary(tmp_path: Path) -> None:
    """The wrapper is a pass-through, so a caller can point the canary at a
    non-default changelog or PKGBUILD without a second copy of the invocation."""
    repo, uv_log, bindir = _make_repo(tmp_path, uv_exit=0)
    assert _run(repo, bindir, "--pkgbuild", "aur/PKGBUILD").returncode == 0
    assert uv_log.read_text(encoding="utf-8").split() == [
        "run",
        "--frozen",
        "release-canary",
        "--pkgbuild",
        "aur/PKGBUILD",
    ]

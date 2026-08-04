"""Tests for ci_truth_serum/check_uncached_download.py — the (opinionated) lint that
requires a cache in any job that downloads a version-pinned tool.

Drives check_file(path) directly so each rule is asserted in isolation. The
composite-action cases point the module's REPO_ROOT at the tmp tree, because that
is what `uses: ./…` resolves against."""

from pathlib import Path

import pytest

from tests._helpers import load_hook

uc = load_hook("check_uncached_download.py", "check_uncached_download")

CACHE_STEP = "      - uses: actions/cache@0057852bfaa89a56745cba8c7296529d2fc39830\n"


def _write(tmp_path: Path, body: str, name: str = "wf.yaml") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _job(steps: str, name: str = "build") -> str:
    return f"name: x\non:\n  push:\njobs:\n  {name}:\n    runs-on: ubuntu-latest\n    steps:\n{steps}"


def _run(script: str) -> str:
    """A `run:` step whose body is SCRIPT, indented into a job's step list."""
    body = "\n".join(f"          {line}" for line in script.splitlines())
    return f"      - name: install\n        run: |\n{body}\n"


def _messages(path: Path) -> list[str]:
    return [message for _, message in uc.check_file(path)]


# ── flagged ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("script", "named"),
    [
        ("pip install ruff==0.14.0", "ruff==0.14.0"),
        ('pip install "ruff==${RUFF_VERSION}"', "ruff==${RUFF_VERSION}"),
        ("uv tool install pre-commit==4.6.1", "pre-commit==4.6.1"),
        ("npm install -g @anthropic-ai/claude-code@2.1.201", "claude-code@2.1.201"),
        ("npm install --prefix /tmp/j '@jazzer.js/core@4.0.0'", "core@4.0.0"),
        (
            "curl -fsSL -o t https://github.com/o/r/releases/download/v2.12.0/t",
            "releases/download/v2.12.0/t",
        ),
        (
            'curl -O "https://example.com/dl/${TOOL_VERSION}/t.tgz"',
            "${TOOL_VERSION}",
        ),
        ("sudo curl -O https://example.com/x/v1.2.3/t.tgz", "v1.2.3"),
    ],
    ids=[
        "pip",
        "pip-var",
        "uv-tool",
        "npm-global",
        "npm-prefix",
        "curl",
        "curl-var",
        "sudo-curl",
    ],
)
def test_a_pinned_download_in_an_uncached_job_is_flagged(tmp_path, script, named):
    """Each install family this lint knows, alone in a job with no cache."""
    messages = _messages(_write(tmp_path, _job(_run(script))))
    assert len(messages) == 1, messages
    assert named in messages[0]
    assert "build" in messages[0]


def test_the_finding_points_at_the_step_line(tmp_path):
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0")))
    line, _ = uc.check_file(path)[0]
    assert (
        path.read_text(encoding="utf-8")
        .splitlines()[line - 1]
        .strip()
        .startswith("- name: install")
    )


def test_each_uncached_job_is_flagged_separately(tmp_path):
    path = _write(
        tmp_path,
        _job(_run("pip install ruff==0.14.0"))
        + f"  other:\n    runs-on: ubuntu-latest\n    steps:\n{_run('pip install black==24.1.0')}",
    )
    assert len(uc.check_file(path)) == 2


# ── clean ────────────────────────────────────────────────────────────────────


def test_a_cache_step_anywhere_in_the_job_exempts_it(tmp_path):
    """The lenient rule: one cache in the job is enough. A better-targeted second
    cache is a judgement this lint does not make."""
    path = _write(tmp_path, _job(CACHE_STEP + _run("pip install ruff==0.14.0")))
    assert uc.check_file(path) == []


def test_a_cache_below_the_install_still_exempts_the_job(tmp_path):
    """Step order does not matter: the question is whether the job caches at all."""
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0") + CACHE_STEP))
    assert uc.check_file(path) == []


@pytest.mark.parametrize(
    "step",
    [
        "      - uses: actions/setup-node@v4\n        with:\n          cache: npm\n",
        "      - uses: astral-sh/setup-uv@v5\n        with:\n          enable-cache: true\n",
        "      - uses: Swatinem/rust-cache@v2\n",
        "      - uses: actions/cache/restore@v4\n",
    ],
    ids=["setup-node", "setup-uv", "rust-cache", "cache-restore"],
)
def test_each_caching_action_exempts_the_job(tmp_path, step):
    assert (
        uc.check_file(_write(tmp_path, _job(step + _run("pip install ruff==0.14.0"))))
        == []
    )


def test_a_setup_action_with_caching_off_does_not_exempt_the_job(tmp_path):
    """The input is what turns the cache on, so its absence must not read as a
    cache. Without this the lint would go silent on every job that sets up a
    language runtime, which is most of them."""
    step = "      - uses: actions/setup-node@v4\n        with:\n          node-version: 20\n"
    assert (
        len(
            uc.check_file(
                _write(tmp_path, _job(step + _run("pip install ruff==0.14.0")))
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "script",
    [
        "pip install ruff",
        "apt-get install -y curl",
        "npm install -g prettier",
        "npm install -g prettier@latest",
        "docker pull ubuntu:24.04",
        "curl -fsSL -o t https://example.com/latest/t",
        "npm install",
        "pip install -r requirements.txt",
        "echo 'pip install ruff==0.14.0'",
        "# pip install ruff==0.14.0",
    ],
    ids=[
        "unpinned-pip",
        "unpinned-apt",
        "unpinned-npm",
        "npm-latest",
        "docker-pull",
        "unpinned-url",
        "local-npm-install",
        "requirements-file",
        "inside-a-message",
        "inside-a-comment",
    ],
)
def test_what_this_lint_leaves_alone(tmp_path, script):
    """An unpinned download has no stable cache key, so flagging it would be an
    unfixable finding — check-versionless-install owns that class. A local `npm
    install` records its pins in package.json. A string a command prints, and a
    comment, are not commands at all."""
    assert uc.check_file(_write(tmp_path, _job(_run(script)))) == []


def test_a_pinned_install_beside_an_unpinned_one_is_not_flagged(tmp_path):
    """One command, two specs: the unpinned spec makes the whole command
    un-cacheable, so this lint stays out of it."""
    path = _write(tmp_path, _job(_run("pip install ruff==0.14.0 black")))
    assert uc.check_file(path) == []


def test_a_uses_job_is_skipped(tmp_path):
    """A reusable-workflow call runs another file's steps, which are judged there."""
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  gate:\n    uses: ./.github/workflows/o.yaml\n",
    )
    assert uc.check_file(path) == []


# ── opt-out ──────────────────────────────────────────────────────────────────


def test_an_annotation_in_the_run_body_suppresses(tmp_path):
    path = _write(
        tmp_path,
        _job(_run("# cache-exempt: runs once a month\npip install ruff==0.14.0")),
    )
    assert uc.check_file(path) == []


def test_an_annotation_anywhere_in_the_job_block_suppresses(tmp_path):
    path = _write(
        tmp_path,
        "name: x\non:\n  push:\njobs:\n  build: # cache-exempt: the download is the test\n"
        f"    runs-on: ubuntu-latest\n    steps:\n{_run('pip install ruff==0.14.0')}",
    )
    assert uc.check_file(path) == []


def test_an_annotation_without_a_reason_does_not_suppress(tmp_path):
    """A bare token is a claim with no argument behind it, so it does not count."""
    path = _write(tmp_path, _job(_run("# cache-exempt\npip install ruff==0.14.0")))
    assert len(uc.check_file(path)) == 1


# ── composite actions ────────────────────────────────────────────────────────


def _action(tmp_path: Path, name: str, steps: str) -> None:
    directory = tmp_path / ".github" / "actions" / name
    directory.mkdir(parents=True)
    (directory / "action.yaml").write_text(
        f"name: {name}\nruns:\n  using: composite\n  steps:\n{steps}", encoding="utf-8"
    )


@pytest.fixture(name="rooted")
def _rooted(tmp_path, monkeypatch):
    """Resolve `uses: ./…` against the tmp tree, as a real run resolves it against
    the repository root."""
    monkeypatch.setattr(uc, "REPO_ROOT", tmp_path)
    return tmp_path


def test_a_cache_inside_a_used_composite_action_exempts_the_job(rooted):
    """The measured false-positive source. A job whose caching lives one level down
    in a shared setup action IS cached; a lint that did not follow `uses: ./…`
    would report almost every job in a real tree."""
    _action(rooted, "setup", CACHE_STEP)
    path = _write(
        rooted,
        _job(
            "      - uses: ./.github/actions/setup\n" + _run("pip install ruff==0.14.0")
        ),
    )
    assert uc.check_file(path) == []


def test_an_install_inside_a_used_composite_action_is_reported_at_the_uses_step(rooted):
    """The cache a finding asks for belongs to the JOB, so the annotation points at
    the workflow step that pulled the action in. The message still names the action,
    because that is where the install itself is written."""
    _action(rooted, "install-tool", _run("pip install ruff==0.14.0"))
    path = _write(rooted, _job("      - uses: ./.github/actions/install-tool\n"))
    findings = uc.check_file(path)
    assert len(findings) == 1
    line, message = findings[0]
    assert path.read_text(encoding="utf-8").splitlines()[line - 1].strip() == (
        "- uses: ./.github/actions/install-tool"
    )
    assert "./.github/actions/install-tool" in message


def test_two_actions_that_use_each_other_terminate(rooted):
    """A cycle must not recurse forever. Neither action caches, so the install in
    one of them is still reported."""
    _action(rooted, "a", "      - uses: ./.github/actions/b\n")
    _action(
        rooted,
        "b",
        "      - uses: ./.github/actions/a\n" + _run("pip install ruff==0.14.0"),
    )
    path = _write(rooted, _job("      - uses: ./.github/actions/a\n"))
    assert len(uc.check_file(path)) == 1


def test_a_missing_composite_action_is_not_a_crash(rooted):
    """A `uses: ./…` pointing at nothing on disk is actionlint's finding, not this
    lint's — it must not stop the rest of the job being judged."""
    path = _write(
        rooted,
        _job(
            "      - uses: ./.github/actions/gone\n" + _run("pip install ruff==0.14.0")
        ),
    )
    assert len(uc.check_file(path)) == 1


# ── unparseable input ────────────────────────────────────────────────────────


def test_unparseable_yaml_is_reported_not_passed(tmp_path):
    """A file this lint cannot read must never read as 'no uncached downloads
    here' — the silent pass is the failure mode the report exists to prevent."""
    path = _write(tmp_path, "name: x\njobs:\n  a: [unclosed\n")
    findings = uc.check_file(path)
    assert len(findings) == 1
    assert "could not parse" in findings[0][1]


def test_an_unparseable_composite_action_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(uc, "REPO_ROOT", tmp_path)
    _action(tmp_path, "broken", "      - uses: [unclosed\n")
    path = _write(tmp_path, _job("      - uses: ./.github/actions/broken\n"))
    findings = uc.check_file(path)
    assert len(findings) == 1
    assert "could not parse" in findings[0][1]
